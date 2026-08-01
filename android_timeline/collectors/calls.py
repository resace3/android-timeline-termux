"""Call log **metadata** via ``termux-call-log``.

Disabled by default. Even when enabled this collector stores no phone
numbers and no contact names: numbers become salted pseudonyms and names are
dropped unless ``privacy.store_contact_names`` is explicitly turned on.
"""

from __future__ import annotations

from typing import Any

from ..models import QualityFlag, parse_iso_utc
from ..redaction import pseudonymize
from .base import Collector, CollectorError, Observation

__all__ = ["CallsCollector"]


class CallsCollector(Collector):
    source = "calls"
    required_commands = ("termux-call-log",)

    def observe(self) -> list[Observation]:
        if not self.config.privacy.has_salt():
            raise CollectorError(
                "call collection requires a pseudonymisation salt; refusing to "
                "store weakly-hashed phone numbers"
            )
        salt = self.config.privacy.salt()

        options = self.settings.options if self.settings else {}
        limit = max(1, min(int(options.get("limit", 50)), 500))

        result = self.runner.run("termux-call-log", ["-l", str(limit)])
        if not result.ok:
            raise CollectorError(
                f"termux-call-log exited {result.returncode}: "
                f"{result.stderr.strip()[:200]}"
            )
        entries = result.json()
        if not isinstance(entries, list):
            raise CollectorError("expected a JSON array from termux-call-log")

        observations: list[Observation] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            payload: dict[str, Any] = {
                "direction": _direction(str(entry.get("type", ""))),
                "duration_seconds": _as_int(entry.get("duration")) or 0,
                "counterparty_pseudonym": pseudonymize(entry.get("phone_number"), salt),
            }
            if self.config.privacy.store_contact_names:
                name = entry.get("name")
                payload["contact_name"] = str(name) if name else None

            event_time = _parse_time(entry.get("date"))
            # Termux reports a local, human-formatted date with no offset. We
            # read it as UTC and say so, rather than silently inventing a
            # timezone the reader cannot audit later.
            flags = [QualityFlag.REDACTED]
            if event_time is not None:
                flags.append(QualityFlag.CLOCK_UNCERTAIN)

            observations.append(
                Observation(
                    event_type="call_record",
                    payload=payload,
                    event_time=event_time,
                    quality_flags=flags,
                    dedupe_key=None if event_time else str(entry.get("date", "")),
                )
            )
        return observations


def _direction(value: str) -> str:
    normalised = value.strip().upper()
    if normalised in ("INCOMING", "OUTGOING", "MISSED", "REJECTED", "BLOCKED"):
        return normalised.lower()
    return "unknown"


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any) -> Any:
    """Best-effort parse of Termux's call-log timestamp.

    Termux emits a local, human-formatted date. When it cannot be parsed the
    observation still lands, timestamped at collection time and marked with a
    dedupe key so entries do not collapse into one another.
    """
    if not value:
        return None
    text = str(value).strip()
    for candidate in (text, text.replace(" ", "T")):
        try:
            return parse_iso_utc(
                candidate if candidate.endswith("Z") else f"{candidate}Z"
            )
        except ValueError:
            continue
    return None
