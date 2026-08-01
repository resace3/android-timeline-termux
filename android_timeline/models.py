"""Event envelope, batch and acknowledgement models.

Everything here is plain ``dataclasses`` + stdlib so the collector stays
installable on Termux without a compiler.  The hand-written validators in
this module are kept byte-compatible with ``schemas/*.schema.json``; the
contract tests assert that the two agree.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from . import PROTOCOL_VERSION, SCHEMA_VERSION

__all__ = [
    "Acknowledgement",
    "Batch",
    "Event",
    "EventValidationError",
    "QualityFlag",
    "deterministic_event_id",
    "iso_utc",
    "local_offset_minutes",
    "parse_iso_utc",
    "utc_now",
    "validate_batch_dict",
    "validate_event_dict",
]

# UUID namespace for deterministic event ids.  Fixed forever: changing it
# would break deduplication against already-uploaded events.
_NAMESPACE: Final = uuid.UUID("6f1a7a1e-6b1e-4f7d-9c0e-3f6c9b2a5d10")

_ID_RE: Final = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ISO_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")

#: Hard ceiling on a single event's serialised size. Mirrors the server.
MAX_EVENT_BYTES: Final = 64 * 1024

#: Events further than this into the future are rejected as clock skew.
MAX_FUTURE_SKEW_SECONDS: Final = 24 * 3600

#: Events older than this are still accepted but flagged; the server keeps
#: them because late arrival is normal after a long offline period.
LATE_ARRIVAL_SECONDS: Final = 6 * 3600


class QualityFlag:
    """Well-known values for ``Event.quality_flags``.

    Quality flags never change the meaning of ``payload``; they annotate how
    much the reader should trust it.
    """

    COLLECTOR_ERROR: Final = "collector_error"
    COMMAND_UNAVAILABLE: Final = "command_unavailable"
    PERMISSION_DENIED: Final = "permission_denied"
    MOCKED: Final = "mocked"
    EXPERIMENTAL: Final = "experimental"
    LATE_ARRIVAL: Final = "late_arrival"
    REDACTED: Final = "redacted"
    COARSE: Final = "coarse"
    PARTIAL: Final = "partial"
    CLOCK_UNCERTAIN: Final = "clock_uncertain"

    ALL: Final = frozenset(
        {
            COLLECTOR_ERROR,
            COMMAND_UNAVAILABLE,
            PERMISSION_DENIED,
            MOCKED,
            EXPERIMENTAL,
            LATE_ARRIVAL,
            REDACTED,
            COARSE,
            PARTIAL,
            CLOCK_UNCERTAIN,
        }
    )


class EventValidationError(ValueError):
    """Raised when an event envelope fails validation."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def utc_now() -> datetime:
    """Timezone-aware current time in UTC."""
    return datetime.now(UTC)


def iso_utc(value: datetime) -> str:
    """Render ``value`` as a ``Z``-suffixed RFC 3339 timestamp.

    Naive datetimes are assumed to already be UTC -- the collector never
    creates naive local timestamps, but fixtures sometimes do.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    if value.microsecond:
        return value.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(value: str) -> datetime:
    """Parse a ``Z``-suffixed RFC 3339 timestamp into an aware UTC datetime."""
    if not isinstance(value, str):
        raise ValueError(f"expected string timestamp, got {type(value).__name__}")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def local_offset_minutes(when: datetime | None = None) -> int:
    """Offset of the device's local timezone from UTC, in minutes.

    Retained on every event so a later reader can reconstruct wall-clock
    time without guessing which timezone the phone was in.
    """
    moment = when or datetime.now()
    if moment.tzinfo is not None:
        offset = moment.utcoffset() or timedelta(0)
    else:
        offset = moment.astimezone().utcoffset() or timedelta(0)
    return int(offset.total_seconds() // 60)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def deterministic_event_id(
    device_id: str,
    source: str,
    event_type: str,
    event_time_utc: str,
    payload: dict[str, Any] | None = None,
) -> str:
    """Derive a stable UUIDv5 for a naturally idempotent observation.

    Two collectors sampling the same instant produce the same id, so a
    duplicate can never be stored twice -- on the phone or on the server.
    Collectors that genuinely emit distinct events at the same timestamp
    should pass a discriminator inside ``payload``.
    """
    seed = "|".join(
        [
            device_id,
            source,
            event_type,
            event_time_utc,
            _canonical(payload or {}),
        ]
    )
    return str(uuid.uuid5(_NAMESPACE, seed))


@dataclass(slots=True)
class Event:
    """A single raw, timestamped observation.

    Raw events are the ground truth of the whole system: features are
    derived from them and can be recomputed, but an event is never
    rewritten once stored.
    """

    device_id: str
    source: str
    event_type: str
    event_time_utc: str
    collected_time_utc: str
    timezone_offset_minutes: int
    payload: dict[str, Any] = field(default_factory=dict)
    quality_flags: list[str] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION
    event_id: str = ""

    def __post_init__(self) -> None:
        if not self.event_id:
            self.event_id = deterministic_event_id(
                self.device_id,
                self.source,
                self.event_type,
                self.event_time_utc,
                self.payload,
            )

    @classmethod
    def create(
        cls,
        *,
        device_id: str,
        source: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        event_time: datetime | None = None,
        collected_time: datetime | None = None,
        quality_flags: list[str] | None = None,
        event_id: str | None = None,
    ) -> Event:
        """Build an event, filling in timestamps from the system clock."""
        collected = collected_time or utc_now()
        occurred = event_time or collected
        return cls(
            device_id=device_id,
            source=source,
            event_type=event_type,
            event_time_utc=iso_utc(occurred),
            collected_time_utc=iso_utc(collected),
            timezone_offset_minutes=local_offset_minutes(),
            payload=dict(payload or {}),
            quality_flags=list(quality_flags or []),
            event_id=event_id or "",
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the wire envelope (key order matches the schema)."""
        return {
            "event_id": self.event_id,
            "device_id": self.device_id,
            "source": self.source,
            "event_type": self.event_type,
            "event_time_utc": self.event_time_utc,
            "collected_time_utc": self.collected_time_utc,
            "timezone_offset_minutes": self.timezone_offset_minutes,
            "schema_version": self.schema_version,
            "quality_flags": list(self.quality_flags),
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Event:
        errors = validate_event_dict(raw)
        if errors:
            raise EventValidationError(errors)
        return cls(
            event_id=raw["event_id"],
            device_id=raw["device_id"],
            source=raw["source"],
            event_type=raw["event_type"],
            event_time_utc=raw["event_time_utc"],
            collected_time_utc=raw["collected_time_utc"],
            timezone_offset_minutes=int(raw["timezone_offset_minutes"]),
            schema_version=int(raw.get("schema_version", SCHEMA_VERSION)),
            quality_flags=list(raw.get("quality_flags") or []),
            payload=dict(raw.get("payload") or {}),
        )


def validate_event_dict(raw: Any) -> list[str]:
    """Validate an event envelope, returning a list of human-readable errors.

    Returns ``[]`` when the event is valid.  Kept deliberately permissive
    about *payload contents* -- payloads are source-specific and versioned
    separately -- but strict about the envelope.
    """
    errors: list[str] = []
    if not isinstance(raw, dict):
        return ["event must be a JSON object"]

    required = (
        "event_id",
        "device_id",
        "source",
        "event_type",
        "event_time_utc",
        "collected_time_utc",
        "timezone_offset_minutes",
        "schema_version",
        "payload",
    )
    for key in required:
        if key not in raw:
            errors.append(f"missing required field '{key}'")
    if errors:
        return errors

    for key in ("event_id", "device_id"):
        value = raw[key]
        if not isinstance(value, str) or not _ID_RE.match(value):
            errors.append(f"'{key}' must match {_ID_RE.pattern}")

    for key in ("source", "event_type"):
        value = raw[key]
        if not isinstance(value, str) or not _NAME_RE.match(value):
            errors.append(f"'{key}' must match {_NAME_RE.pattern}")

    for key in ("event_time_utc", "collected_time_utc"):
        value = raw[key]
        if not isinstance(value, str) or not _ISO_RE.match(value):
            errors.append(f"'{key}' must be an RFC 3339 UTC timestamp ending in 'Z'")

    offset = raw["timezone_offset_minutes"]
    if not isinstance(offset, int) or isinstance(offset, bool):
        errors.append("'timezone_offset_minutes' must be an integer")
    elif not -1080 <= offset <= 1080:
        errors.append("'timezone_offset_minutes' must be between -1080 and 1080")

    version = raw["schema_version"]
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        errors.append("'schema_version' must be a positive integer")

    if not isinstance(raw["payload"], dict):
        errors.append("'payload' must be a JSON object")

    flags = raw.get("quality_flags", [])
    if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags):
        errors.append("'quality_flags' must be an array of strings")
    elif len(flags) > 16:
        errors.append("'quality_flags' must contain at most 16 entries")

    unknown = set(raw) - set(required) - {"quality_flags"}
    if unknown:
        errors.append(f"unexpected field(s): {', '.join(sorted(unknown))}")

    return errors


@dataclass(slots=True)
class Batch:
    """An upload unit: a set of events plus provenance about the sender."""

    batch_id: str
    device_id: str
    events: list[Event]
    created_time_utc: str
    collector_version: str
    protocol_version: int = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "batch_id": self.batch_id,
            "device_id": self.device_id,
            "collector_version": self.collector_version,
            "created_time_utc": self.created_time_utc,
            "events": [event.to_dict() for event in self.events],
        }


def validate_batch_dict(raw: Any, *, max_events: int = 1000) -> list[str]:
    """Validate a batch envelope. Returns ``[]`` when valid."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return ["batch must be a JSON object"]

    for key in (
        "protocol_version",
        "batch_id",
        "device_id",
        "collector_version",
        "created_time_utc",
        "events",
    ):
        if key not in raw:
            errors.append(f"missing required field '{key}'")
    if errors:
        return errors

    if raw["protocol_version"] != PROTOCOL_VERSION:
        errors.append(
            f"unsupported protocol_version {raw['protocol_version']!r} "
            f"(server speaks {PROTOCOL_VERSION})"
        )
    for key in ("batch_id", "device_id"):
        if not isinstance(raw[key], str) or not _ID_RE.match(raw[key]):
            errors.append(f"'{key}' must match {_ID_RE.pattern}")
    if (
        not isinstance(raw["collector_version"], str)
        or len(raw["collector_version"]) > 64
    ):
        errors.append("'collector_version' must be a string of at most 64 characters")
    if not isinstance(raw["created_time_utc"], str) or not _ISO_RE.match(
        raw["created_time_utc"]
    ):
        errors.append("'created_time_utc' must be an RFC 3339 UTC timestamp")

    events = raw["events"]
    if not isinstance(events, list):
        errors.append("'events' must be an array")
        return errors
    if not events:
        errors.append("'events' must contain at least one event")
    if len(events) > max_events:
        errors.append(f"'events' must contain at most {max_events} entries")

    for index, event in enumerate(events[:max_events]):
        for problem in validate_event_dict(event):
            errors.append(f"events[{index}]: {problem}")

    return errors


@dataclass(slots=True)
class Acknowledgement:
    """Server response describing the fate of every event in a batch."""

    batch_id: str
    accepted: dict[str, str]
    rejected: dict[str, str]
    server_version: str = ""
    protocol_version: int = PROTOCOL_VERSION
    received_time_utc: str = ""

    @property
    def stored_ids(self) -> set[str]:
        """Event ids the server now holds -- newly stored *or* already known.

        Both outcomes mean the phone can stop retrying, which is what makes
        replay safe.
        """
        return set(self.accepted)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Acknowledgement:
        if not isinstance(raw, dict):
            raise ValueError("acknowledgement must be a JSON object")
        accepted_raw = raw.get("accepted") or []
        rejected_raw = raw.get("rejected") or []
        if not isinstance(accepted_raw, list) or not isinstance(rejected_raw, list):
            raise ValueError("'accepted' and 'rejected' must be arrays")

        accepted: dict[str, str] = {}
        for item in accepted_raw:
            if not isinstance(item, dict) or "event_id" not in item:
                raise ValueError("each accepted entry needs an 'event_id'")
            accepted[str(item["event_id"])] = str(item.get("status", "stored"))

        rejected: dict[str, str] = {}
        for item in rejected_raw:
            if not isinstance(item, dict) or "event_id" not in item:
                raise ValueError("each rejected entry needs an 'event_id'")
            rejected[str(item["event_id"])] = str(item.get("reason", "unknown"))

        return cls(
            batch_id=str(raw.get("batch_id", "")),
            accepted=accepted,
            rejected=rejected,
            server_version=str(raw.get("server_version", "")),
            protocol_version=int(raw.get("protocol_version", PROTOCOL_VERSION)),
            received_time_utc=str(raw.get("received_time_utc", "")),
        )
