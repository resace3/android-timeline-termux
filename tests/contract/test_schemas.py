"""Protocol contract tests.

Two things are asserted here:

1. The published JSON Schemas and the hand-written validators in
   ``android_timeline.models`` agree. The collector cannot depend on
   ``jsonschema`` at runtime (it pulls a Rust extension, which is exactly
   what Termux users must not need), so the two implementations are kept
   honest against each other in CI instead.
2. The vendored schema files match their recorded checksums, so an
   accidental edit on this side of the protocol is caught before it can
   drift away from ``android-timeline-home-assistant``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import pytest

from android_timeline import PROTOCOL_VERSION, SCHEMA_VERSION
from android_timeline.models import (
    Batch,
    Event,
    utc_now,
    validate_batch_dict,
    validate_event_dict,
)

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "schemas"
CHECKSUM_FILE = SCHEMA_DIR / "PROTOCOL_SHA256SUMS"


def valid_event(**overrides: Any) -> dict[str, Any]:
    event = Event.create(
        device_id="device-test-001",
        source="battery",
        event_type="battery_sample",
        payload={"percentage": 55},
        event_time=utc_now(),
    ).to_dict()
    event.update(overrides)
    return event


class TestSchemaFiles:
    def test_expected_schemas_exist(self) -> None:
        names = {p.name for p in SCHEMA_DIR.glob("*.schema.json")}
        assert names == {
            "event.schema.json",
            "batch.schema.json",
            "acknowledgement.schema.json",
        }

    def test_every_schema_is_itself_valid(
        self, schemas: dict[str, dict[str, Any]]
    ) -> None:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError

        for name, document in schemas.items():
            try:
                Draft202012Validator.check_schema(document)
            except SchemaError as exc:  # pragma: no cover - fails the build
                pytest.fail(f"{name} is not a valid JSON Schema: {exc}")

    def test_every_schema_declares_an_id(
        self, schemas: dict[str, dict[str, Any]]
    ) -> None:
        for document in schemas.values():
            assert document["$id"].startswith("https://")

    def test_protocol_version_matches_the_schema_bound(
        self, schemas: dict[str, dict[str, Any]]
    ) -> None:
        batch = schemas["batch.schema.json"]["properties"]["protocol_version"]
        assert batch["minimum"] <= PROTOCOL_VERSION <= batch["maximum"]

    def test_schema_version_is_accepted_by_the_event_schema(
        self, schema_validator: Callable[[str], Any]
    ) -> None:
        validator = schema_validator("event.schema.json")
        assert validator.is_valid(valid_event(schema_version=SCHEMA_VERSION))


class TestChecksums:
    """Guards the vendored copy of the protocol against silent edits."""

    def test_checksum_file_exists(self) -> None:
        assert CHECKSUM_FILE.is_file(), (
            "schemas/PROTOCOL_SHA256SUMS is missing; regenerate it with "
            "`sha256sum schemas/*.schema.json > schemas/PROTOCOL_SHA256SUMS`"
        )

    def test_recorded_checksums_match_the_files(self) -> None:
        recorded: dict[str, str] = {}
        for line in CHECKSUM_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            digest, _, name = line.partition("  ")
            recorded[name.strip().lstrip("*")] = digest.strip()

        assert recorded, "no checksums recorded"

        for name, expected in recorded.items():
            path = SCHEMA_DIR / Path(name).name
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            assert actual == expected, (
                f"{name} has changed. If this is an intentional protocol "
                "change, update android-timeline-home-assistant in the same "
                "release and regenerate PROTOCOL_SHA256SUMS."
            )

    def test_every_schema_is_covered_by_a_checksum(self) -> None:
        recorded = {
            line.partition("  ")[2].strip().lstrip("*")
            for line in CHECKSUM_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }
        recorded_names = {Path(name).name for name in recorded}
        actual = {p.name for p in SCHEMA_DIR.glob("*.schema.json")}
        assert actual <= recorded_names


class TestValidatorsAgree:
    """The stdlib validator and the JSON Schema must reach the same verdict."""

    VALID_CASES: ClassVar[list[dict[str, Any]]] = [
        {},
        {"quality_flags": []},
        {"quality_flags": ["mocked", "coarse"]},
        {"timezone_offset_minutes": 0},
        {"timezone_offset_minutes": -720},
        {"payload": {}},
        {"payload": {"nested": {"deep": [1, 2, 3]}}},
        {"payload": {"note": "'; DROP TABLE events; --"}},
        {"event_time_utc": "2026-03-15T00:00:00.123Z"},
    ]

    INVALID_CASES: ClassVar[list[dict[str, Any]]] = [
        {"event_id": ""},
        {"event_id": "has spaces"},
        {"device_id": "../../etc/passwd"},
        {"source": "Battery"},
        {"source": "1battery"},
        {"event_type": "has-dashes"},
        {"event_time_utc": "2026-03-15T00:00:00"},
        {"event_time_utc": "2026-03-15 00:00:00Z"},
        {"event_time_utc": "not-a-time"},
        {"collected_time_utc": "2026-03-15T00:00:00+01:00"},
        {"timezone_offset_minutes": 2000},
        {"timezone_offset_minutes": -2000},
        {"schema_version": 0},
        {"payload": []},
        {"payload": "text"},
        {"quality_flags": "mocked"},
        {"quality_flags": [1, 2]},
    ]

    @pytest.mark.parametrize("overrides", VALID_CASES)
    def test_valid_cases_pass_both(
        self, overrides: dict[str, Any], schema_validator: Callable[[str], Any]
    ) -> None:
        event = valid_event(**overrides)
        assert validate_event_dict(event) == []
        assert schema_validator("event.schema.json").is_valid(event)

    @pytest.mark.parametrize("overrides", INVALID_CASES)
    def test_invalid_cases_fail_both(
        self, overrides: dict[str, Any], schema_validator: Callable[[str], Any]
    ) -> None:
        event = valid_event(**overrides)
        assert validate_event_dict(event) != []
        assert not schema_validator("event.schema.json").is_valid(event)

    def test_unknown_top_level_field_fails_both(
        self, schema_validator: Callable[[str], Any]
    ) -> None:
        event = valid_event(surprise=1)
        assert validate_event_dict(event) != []
        assert not schema_validator("event.schema.json").is_valid(event)

    def test_unknown_quality_flag_is_rejected_by_the_schema(
        self, schema_validator: Callable[[str], Any]
    ) -> None:
        event = valid_event(quality_flags=["totally_made_up"])
        assert not schema_validator("event.schema.json").is_valid(event)


class TestBatchContract:
    def test_a_collector_built_batch_validates(
        self, schema_validator: Callable[[str], Any]
    ) -> None:
        batch = Batch(
            batch_id="batch-test-0001",
            device_id="device-test-001",
            events=[Event.from_dict(valid_event())],
            created_time_utc="2026-03-15T00:00:00Z",
            collector_version="0.1.0",
        ).to_dict()
        assert validate_batch_dict(batch) == []
        schema_validator("batch.schema.json").validate(batch)

    def test_empty_batch_fails_both(self, schema_validator: Callable[[str], Any]) -> None:
        batch = Batch(
            batch_id="batch-test-0001",
            device_id="device-test-001",
            events=[],
            created_time_utc="2026-03-15T00:00:00Z",
            collector_version="0.1.0",
        ).to_dict()
        assert validate_batch_dict(batch) != []
        assert not schema_validator("batch.schema.json").is_valid(batch)


class TestAcknowledgementContract:
    def test_a_well_formed_acknowledgement_validates(
        self, schema_validator: Callable[[str], Any]
    ) -> None:
        payload = {
            "protocol_version": 1,
            "batch_id": "batch-test-0001",
            "server_version": "0.1.0",
            "received_time_utc": "2026-03-15T00:00:00Z",
            "accepted": [{"event_id": "abc", "status": "stored"}],
            "rejected": [{"event_id": "def", "reason": "schema"}],
            "counts": {"received": 2, "stored": 1, "duplicate": 0, "rejected": 1},
        }
        schema_validator("acknowledgement.schema.json").validate(payload)

    def test_unknown_status_is_rejected(
        self, schema_validator: Callable[[str], Any]
    ) -> None:
        payload = {
            "protocol_version": 1,
            "batch_id": "batch-test-0001",
            "received_time_utc": "2026-03-15T00:00:00Z",
            "accepted": [{"event_id": "abc", "status": "maybe"}],
            "rejected": [],
            "counts": {"received": 1, "stored": 0, "duplicate": 0, "rejected": 0},
        }
        assert not schema_validator("acknowledgement.schema.json").is_valid(payload)


class TestSyntheticDayContract:
    def test_every_synthetic_event_validates(
        self, synthetic_day: dict[str, Any], schema_validator: Callable[[str], Any]
    ) -> None:
        validator = schema_validator("event.schema.json")
        for event in synthetic_day["events"]:
            validator.validate(event)
            assert validate_event_dict(event) == []

    def test_the_day_contains_the_documented_edge_cases(
        self, synthetic_day: dict[str, Any]
    ) -> None:
        expected = synthetic_day["expected"]
        assert expected["duplicate_rows"] == 1
        assert expected["gap_hours"] == [2, 3]
        assert expected["late_arrival_event_id"]
        assert expected["replay_event_id"]

    def test_the_gap_really_is_empty(self, synthetic_day: dict[str, Any]) -> None:
        gap_start = synthetic_day["expected"]["gap_start_utc"]
        gap_end = synthetic_day["expected"]["gap_end_utc"]
        inside = [
            e
            for e in synthetic_day["events"]
            if gap_start <= e["event_time_utc"] < gap_end
        ]
        assert inside == []

    def test_the_late_event_was_collected_after_it_happened(
        self, synthetic_day: dict[str, Any]
    ) -> None:
        late_id = synthetic_day["expected"]["late_arrival_event_id"]
        event = next(e for e in synthetic_day["events"] if e["event_id"] == late_id)
        assert event["collected_time_utc"] > event["event_time_utc"]

    def test_no_fixture_looks_like_real_personal_data(
        self, synthetic_day: dict[str, Any]
    ) -> None:
        serialised = json.dumps(synthetic_day)
        assert "device-test-001" in serialised
        # The reserved fictional ranges and synthetic markers only.
        for forbidden in ("@gmail.com", "homeassistant.local", "nabu.casa"):
            assert forbidden not in serialised
