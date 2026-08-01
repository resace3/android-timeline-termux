"""Event envelope semantics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from android_timeline.models import (
    Acknowledgement,
    Batch,
    Event,
    EventValidationError,
    QualityFlag,
    deterministic_event_id,
    iso_utc,
    local_offset_minutes,
    parse_iso_utc,
    validate_batch_dict,
    validate_event_dict,
)


def make_event(**overrides: object) -> Event:
    defaults = {
        "device_id": "device-test-001",
        "source": "battery",
        "event_type": "battery_sample",
        "payload": {"percentage": 50},
    }
    defaults.update(overrides)  # type: ignore[arg-type]
    return Event.create(**defaults)  # type: ignore[arg-type]


class TestTimestamps:
    def test_iso_utc_always_ends_in_z(self) -> None:
        moment = datetime(2026, 7, 31, 23, 45, tzinfo=UTC)
        assert iso_utc(moment) == "2026-07-31T23:45:00Z"

    def test_iso_utc_converts_from_other_offsets(self) -> None:
        eastern = timezone(timedelta(hours=-4))
        moment = datetime(2026, 7, 31, 19, 45, tzinfo=eastern)
        assert iso_utc(moment) == "2026-07-31T23:45:00Z"

    def test_iso_utc_keeps_milliseconds_when_present(self) -> None:
        moment = datetime(2026, 7, 31, 23, 45, 0, 123456, tzinfo=UTC)
        assert iso_utc(moment) == "2026-07-31T23:45:00.123Z"

    def test_round_trip(self) -> None:
        moment = datetime(2026, 7, 31, 23, 45, tzinfo=UTC)
        assert parse_iso_utc(iso_utc(moment)) == moment

    def test_parse_accepts_explicit_offset(self) -> None:
        assert parse_iso_utc("2026-07-31T19:45:00-04:00") == datetime(
            2026, 7, 31, 23, 45, tzinfo=UTC
        )

    def test_local_offset_is_within_valid_range(self) -> None:
        assert -1080 <= local_offset_minutes() <= 1080


class TestDeterministicIds:
    def test_same_observation_yields_same_id(self) -> None:
        args = ("device-test-001", "battery", "battery_sample", "2026-03-15T00:00:00Z")
        assert deterministic_event_id(*args, {"p": 1}) == deterministic_event_id(
            *args, {"p": 1}
        )

    def test_payload_change_yields_new_id(self) -> None:
        args = ("device-test-001", "battery", "battery_sample", "2026-03-15T00:00:00Z")
        assert deterministic_event_id(*args, {"p": 1}) != deterministic_event_id(
            *args, {"p": 2}
        )

    def test_key_order_does_not_affect_id(self) -> None:
        args = ("device-test-001", "battery", "battery_sample", "2026-03-15T00:00:00Z")
        assert deterministic_event_id(*args, {"a": 1, "b": 2}) == (
            deterministic_event_id(*args, {"b": 2, "a": 1})
        )

    def test_device_is_part_of_the_id(self) -> None:
        rest = ("battery", "battery_sample", "2026-03-15T00:00:00Z")
        assert deterministic_event_id("device-a", *rest) != deterministic_event_id(
            "device-b", *rest
        )


class TestEvent:
    def test_create_fills_envelope(self) -> None:
        event = make_event()
        assert event.event_id
        assert event.schema_version == 1
        assert event.event_time_utc.endswith("Z")
        assert event.collected_time_utc.endswith("Z")

    def test_round_trips_through_dict(self) -> None:
        event = make_event(quality_flags=[QualityFlag.MOCKED])
        assert Event.from_dict(event.to_dict()).to_dict() == event.to_dict()

    def test_utc_and_offset_are_both_retained(self) -> None:
        event = make_event()
        payload = event.to_dict()
        assert payload["event_time_utc"].endswith("Z")
        assert isinstance(payload["timezone_offset_minutes"], int)

    def test_from_dict_rejects_invalid(self) -> None:
        broken = make_event().to_dict()
        broken["event_time_utc"] = "not a timestamp"
        with pytest.raises(EventValidationError):
            Event.from_dict(broken)


class TestEventValidation:
    def test_valid_event_has_no_errors(self) -> None:
        assert validate_event_dict(make_event().to_dict()) == []

    @pytest.mark.parametrize(
        "field",
        [
            "event_id",
            "device_id",
            "source",
            "event_type",
            "event_time_utc",
            "collected_time_utc",
            "timezone_offset_minutes",
            "schema_version",
            "payload",
        ],
    )
    def test_missing_required_field_is_reported(self, field: str) -> None:
        payload = make_event().to_dict()
        del payload[field]
        errors = validate_event_dict(payload)
        assert any(field in error for error in errors)

    def test_unknown_field_is_rejected(self) -> None:
        payload = make_event().to_dict()
        payload["surprise"] = 1
        assert any("surprise" in error for error in validate_event_dict(payload))

    def test_naive_timestamp_is_rejected(self) -> None:
        payload = make_event().to_dict()
        payload["event_time_utc"] = "2026-03-15T00:00:00"
        assert validate_event_dict(payload)

    def test_offset_out_of_range_is_rejected(self) -> None:
        payload = make_event().to_dict()
        payload["timezone_offset_minutes"] = 5000
        assert validate_event_dict(payload)

    def test_boolean_is_not_accepted_as_integer(self) -> None:
        payload = make_event().to_dict()
        payload["timezone_offset_minutes"] = True
        assert validate_event_dict(payload)

    def test_sql_injection_string_is_ordinary_input(self) -> None:
        event = make_event(payload={"note": "'; DROP TABLE events; --"})
        assert validate_event_dict(event.to_dict()) == []

    def test_path_traversal_in_source_is_rejected(self) -> None:
        payload = make_event().to_dict()
        payload["source"] = "../../etc/passwd"
        assert validate_event_dict(payload)

    def test_non_object_is_rejected(self) -> None:
        assert validate_event_dict(["nope"]) == ["event must be a JSON object"]


class TestBatchValidation:
    def _batch(self) -> dict[str, object]:
        return Batch(
            batch_id="batch-test-0001",
            device_id="device-test-001",
            events=[make_event()],
            created_time_utc="2026-03-15T00:00:00Z",
            collector_version="0.1.0",
        ).to_dict()

    def test_valid_batch(self) -> None:
        assert validate_batch_dict(self._batch()) == []

    def test_empty_events_rejected(self) -> None:
        batch = self._batch()
        batch["events"] = []
        assert validate_batch_dict(batch)

    def test_too_many_events_rejected(self) -> None:
        batch = self._batch()
        batch["events"] = [make_event().to_dict()] * 3
        assert validate_batch_dict(batch, max_events=2)

    def test_wrong_protocol_version_rejected(self) -> None:
        batch = self._batch()
        batch["protocol_version"] = 99
        assert any("protocol_version" in e for e in validate_batch_dict(batch))

    def test_nested_event_errors_are_located(self) -> None:
        batch = self._batch()
        events = batch["events"]
        assert isinstance(events, list)
        del events[0]["event_id"]
        assert any(error.startswith("events[0]:") for error in validate_batch_dict(batch))


class TestAcknowledgement:
    def test_duplicate_counts_as_stored(self) -> None:
        ack = Acknowledgement.from_dict(
            {
                "batch_id": "batch-test-0001",
                "accepted": [
                    {"event_id": "a", "status": "stored"},
                    {"event_id": "b", "status": "duplicate"},
                ],
                "rejected": [],
            }
        )
        assert ack.stored_ids == {"a", "b"}

    def test_rejected_reasons_are_captured(self) -> None:
        ack = Acknowledgement.from_dict(
            {
                "batch_id": "batch-test-0001",
                "accepted": [],
                "rejected": [{"event_id": "c", "reason": "schema"}],
            }
        )
        assert ack.rejected == {"c": "schema"}
        assert ack.stored_ids == set()

    def test_malformed_entry_raises(self) -> None:
        with pytest.raises(ValueError, match="event_id"):
            Acknowledgement.from_dict({"accepted": [{"status": "stored"}]})
