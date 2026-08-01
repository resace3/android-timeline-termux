"""Collector plugin API and the Termux command adapter.

Every collector shells out to a ``termux-*`` binary from Termux:API. That
single choke point is abstracted behind :class:`CommandRunner` so the whole
collection layer can be exercised on an ordinary Linux runner -- and inside
an Android emulator -- without any Termux:API present.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from ..config import Config
from ..models import Event, QualityFlag, utc_now

__all__ = [
    "Availability",
    "Collector",
    "CollectorError",
    "CommandResult",
    "CommandRunner",
    "MockCommandRunner",
    "Observation",
    "TermuxCommandRunner",
    "get_command_runner",
]

#: Point this at a directory of fixture files to run collectors without
#: Termux:API. Used by unit tests, the container job and the emulator job.
MOCK_DIR_ENV: Final = "ANDROID_TIMELINE_TERMUX_MOCK_DIR"

_DEFAULT_TIMEOUT: Final = 20.0


class Availability(StrEnum):
    """How much a collector's output can be trusted on this device."""

    SUPPORTED = "supported"
    EXPERIMENTAL = "experimental"
    MOCKED = "mocked"
    UNAVAILABLE = "unavailable"


class CollectorError(Exception):
    """A collector failed. Recorded as a quality event, never fatal."""


@dataclass(slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def json(self) -> Any:
        text = self.stdout.strip()
        if not text:
            raise CollectorError("command produced no output")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise CollectorError(f"command output was not JSON: {exc}") from exc


class CommandRunner(ABC):
    """Executes ``termux-*`` helper commands."""

    #: What availability collectors should report when using this runner.
    availability: Availability = Availability.UNAVAILABLE

    @abstractmethod
    def available(self, command: str) -> bool:
        """Whether ``command`` can be executed on this device."""

    @abstractmethod
    def run(
        self,
        command: str,
        args: list[str] | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> CommandResult:
        """Run ``command``; never raises for a non-zero exit status."""


class TermuxCommandRunner(CommandRunner):
    """Real adapter: invokes Termux:API binaries on the device."""

    availability = Availability.SUPPORTED

    def available(self, command: str) -> bool:
        return shutil.which(command) is not None

    def run(
        self,
        command: str,
        args: list[str] | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> CommandResult:
        if not self.available(command):
            return CommandResult(127, "", f"{command}: command not found")
        argv = [command, *(args or [])]
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(124, "", f"{command}: timed out after {timeout}s")
        except OSError as exc:
            return CommandResult(126, "", f"{command}: {exc}")
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class MockCommandRunner(CommandRunner):
    """Fixture-backed adapter used by CI and the emulator smoke test.

    Layout of ``directory``:

    * ``<command>.json``      -- the stdout the command should produce
    * ``<command>__<args>.json`` -- argument-specific override, tried first
    * ``<command>.meta.json`` -- optional ``{"returncode": N, "stderr": "..."}``
    * ``<command>.missing``   -- simulate the command not being installed

    Events produced through this runner always carry the ``mocked`` quality
    flag, so synthetic data can never be mistaken for real observations.
    """

    availability = Availability.MOCKED

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    @staticmethod
    def slug(args: list[str] | None) -> str:
        """Filename-safe rendering of an argument list."""
        if not args:
            return ""
        joined = "_".join(args)
        return re.sub(r"[^A-Za-z0-9._-]+", "_", joined)

    def _fixture(self, command: str, suffix: str) -> Path:
        return self.directory / f"{command}{suffix}"

    def _resolve(self, command: str, args: list[str] | None) -> Path | None:
        """Prefer an argument-specific fixture, fall back to the plain one."""
        slug = self.slug(args)
        if slug:
            specific = self._fixture(command, f"__{slug}.json")
            if specific.exists():
                return specific
        base = self._fixture(command, ".json")
        return base if base.exists() else None

    def available(self, command: str) -> bool:
        if self._fixture(command, ".missing").exists():
            return False
        return self._fixture(command, ".json").exists()

    def run(
        self,
        command: str,
        args: list[str] | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> CommandResult:
        if self._fixture(command, ".missing").exists():
            return CommandResult(127, "", f"{command}: command not found (mocked)")

        payload = self._resolve(command, args)
        if payload is None:
            return CommandResult(127, "", f"{command}: no fixture in {self.directory}")

        meta_path = self._fixture(command, ".meta.json")
        returncode = 0
        stderr = ""
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            returncode = int(meta.get("returncode", 0))
            stderr = str(meta.get("stderr", ""))

        return CommandResult(returncode, payload.read_text(encoding="utf-8"), stderr)


def get_command_runner() -> CommandRunner:
    """Pick the mock runner when ``$ANDROID_TIMELINE_TERMUX_MOCK_DIR`` is set."""
    mock_dir = os.environ.get(MOCK_DIR_ENV)
    if mock_dir:
        return MockCommandRunner(mock_dir)
    return TermuxCommandRunner()


@dataclass(slots=True)
class Observation:
    """A collector's structured output, before it becomes an :class:`Event`."""

    event_type: str
    payload: dict[str, Any]
    event_time: Any = None
    quality_flags: list[str] = field(default_factory=list)
    #: Optional discriminator that makes an event id unique when several
    #: observations share an event_time (e.g. multiple call log entries).
    dedupe_key: str | None = None


class Collector(ABC):
    """Base class for all collectors.

    Subclasses declare a stable :attr:`source` name -- it is stored on every
    event forever, so renaming one is a breaking schema change.
    """

    #: Stable identifier written to ``Event.source``.
    source: str = ""
    #: Termux:API commands this collector needs.
    required_commands: tuple[str, ...] = ()
    #: Marks collectors whose output is not yet trustworthy on real devices.
    experimental: bool = False

    def __init__(self, config: Config, runner: CommandRunner | None = None) -> None:
        if not self.source:  # pragma: no cover - programming error
            raise ValueError(f"{type(self).__name__} must define a source name")
        self.config = config
        self.runner = runner or get_command_runner()
        self.settings = config.collectors.get(self.source)

    # -- capability -----------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.settings and self.settings.enabled)

    @property
    def interval_seconds(self) -> int:
        return self.settings.interval_seconds if self.settings else 300

    def probe(self) -> Availability:
        """Report whether this collector can produce trustworthy data now."""
        missing = [c for c in self.required_commands if not self.runner.available(c)]
        if missing:
            return Availability.UNAVAILABLE
        if self.runner.availability is Availability.MOCKED:
            return Availability.MOCKED
        if self.experimental:
            return Availability.EXPERIMENTAL
        return Availability.SUPPORTED

    def missing_commands(self) -> list[str]:
        return [c for c in self.required_commands if not self.runner.available(c)]

    # -- collection -----------------------------------------------------

    @abstractmethod
    def observe(self) -> list[Observation]:
        """Produce observations. May raise :class:`CollectorError`."""

    def collect(self) -> list[Event]:
        """Run the collector and convert observations into events.

        A failing collector yields a ``collection_error`` quality event
        rather than propagating: one broken source must never stop the
        daemon or hide the fact that data is missing.
        """
        availability = self.probe()
        base_flags: list[str] = []
        if availability is Availability.MOCKED:
            base_flags.append(QualityFlag.MOCKED)
        if availability is Availability.EXPERIMENTAL or self.experimental:
            base_flags.append(QualityFlag.EXPERIMENTAL)

        if availability is Availability.UNAVAILABLE:
            return [
                self._quality_event(
                    "collection_unavailable",
                    {
                        "reason": "required command(s) unavailable",
                        "missing_commands": self.missing_commands(),
                    },
                    [QualityFlag.COMMAND_UNAVAILABLE, *base_flags],
                )
            ]

        try:
            observations = self.observe()
        except CollectorError as exc:
            return [
                self._quality_event(
                    "collection_error",
                    {"error": str(exc)[:500], "error_type": type(exc).__name__},
                    [QualityFlag.COLLECTOR_ERROR, *base_flags],
                )
            ]
        except Exception as exc:
            return [
                self._quality_event(
                    "collection_error",
                    {"error": str(exc)[:500], "error_type": type(exc).__name__},
                    [QualityFlag.COLLECTOR_ERROR, *base_flags],
                )
            ]

        events: list[Event] = []
        for observation in observations:
            payload = dict(observation.payload)
            if observation.dedupe_key:
                payload["_dedupe_key"] = observation.dedupe_key
            events.append(
                Event.create(
                    device_id=self.config.device.device_id,
                    source=self.source,
                    event_type=observation.event_type,
                    payload=payload,
                    event_time=observation.event_time,
                    quality_flags=sorted({*base_flags, *observation.quality_flags}),
                )
            )
        return events

    def _quality_event(
        self, event_type: str, payload: dict[str, Any], flags: list[str]
    ) -> Event:
        return Event.create(
            device_id=self.config.device.device_id,
            source=self.source,
            event_type=event_type,
            payload=payload,
            event_time=utc_now(),
            quality_flags=sorted(set(flags)),
        )
