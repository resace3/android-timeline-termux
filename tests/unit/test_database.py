"""Outbox behaviour: dedupe, transactional batching, retries, retention."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from android_timeline.database import Outbox
from android_timeline.models import Event, iso_utc, utc_now


def event(
    index: int = 0, *, source: str = "battery", device: str = "device-test-001"
) -> Event:
    return Event.create(
        device_id=device,
        source=source,
        event_type="battery_sample",
        payload={"percentage": 50 + index},
        event_time=utc_now() + timedelta(seconds=index),
    )


class TestMigrations:
    def test_migrations_apply(self, outbox: Outbox) -> None:
        assert outbox.schema_version() == 1

    def test_migrations_are_idempotent(self, outbox: Outbox) -> None:
        assert outbox.migrate() == 1
        assert outbox.migrate() == 1

    def test_reopening_preserves_data(self, tmp_path: Path) -> None:
        path = tmp_path / "outbox.sqlite3"
        with Outbox(path) as first:
            first.add_event(event(1))
        with Outbox(path) as second:
            assert second.count_events() == 1
            assert second.schema_version() == 1

    def test_expected_tables_exist(self, outbox: Outbox) -> None:
        rows = outbox._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {row["name"] for row in rows}
        assert {
            "events",
            "sync_batches",
            "collector_status",
            "schema_migrations",
        } <= names


class TestDeduplication:
    def test_same_event_stored_once(self, outbox: Outbox) -> None:
        item = event(1)
        assert outbox.add_event(item) is True
        assert outbox.add_event(item) is False
        assert outbox.count_events() == 1

    def test_add_events_returns_newly_stored_count(self, outbox: Outbox) -> None:
        items = [event(1), event(2), event(1)]
        assert outbox.add_events(items) == 2

    def test_replay_after_upload_does_not_resurrect(self, outbox: Outbox) -> None:
        item = event(1)
        outbox.add_event(item)
        claim = outbox.claim_batch("device-test-001", max_events=10, max_bytes=10_000)
        assert claim is not None
        outbox.complete_batch(claim[0], accepted_ids=[item.event_id])

        assert outbox.add_event(item) is False
        assert outbox.stats().pending_events == 0

    def test_different_devices_do_not_collide(self, outbox: Outbox) -> None:
        outbox.add_event(event(1, device="device-test-001"))
        outbox.add_event(event(1, device="device-test-002"))
        assert outbox.count_events() == 2


class TestBatching:
    def test_claim_marks_events_with_batch_id(self, outbox: Outbox) -> None:
        outbox.add_events([event(i) for i in range(3)])
        claim = outbox.claim_batch("device-test-001", max_events=10, max_bytes=100_000)
        assert claim is not None
        batch_id, events = claim
        assert len(events) == 3
        assert outbox.batch(batch_id)["event_count"] == 3

    def test_claim_respects_max_events(self, outbox: Outbox) -> None:
        outbox.add_events([event(i) for i in range(5)])
        claim = outbox.claim_batch("device-test-001", max_events=2, max_bytes=100_000)
        assert claim is not None
        assert len(claim[1]) == 2

    def test_claim_respects_max_bytes_but_always_sends_one(self, outbox: Outbox) -> None:
        outbox.add_events([event(i) for i in range(5)])
        claim = outbox.claim_batch("device-test-001", max_events=10, max_bytes=1)
        assert claim is not None
        assert len(claim[1]) == 1

    def test_empty_queue_returns_none(self, outbox: Outbox) -> None:
        assert (
            outbox.claim_batch("device-test-001", max_events=10, max_bytes=1000) is None
        )

    def test_complete_marks_accepted_events_uploaded(self, outbox: Outbox) -> None:
        outbox.add_events([event(i) for i in range(3)])
        batch_id, events = outbox.claim_batch(
            "device-test-001", max_events=10, max_bytes=100_000
        )
        outcome = outbox.complete_batch(
            batch_id, accepted_ids=[e.event_id for e in events]
        )
        assert outcome == {"accepted": 3, "rejected": 0, "outstanding": 0}
        assert outbox.stats().pending_events == 0
        assert outbox.batch(batch_id)["status"] == "acknowledged"

    def test_partial_acknowledgement_keeps_the_rest_pending(self, outbox: Outbox) -> None:
        outbox.add_events([event(i) for i in range(3)])
        batch_id, events = outbox.claim_batch(
            "device-test-001", max_events=10, max_bytes=100_000
        )
        outcome = outbox.complete_batch(batch_id, accepted_ids=[events[0].event_id])

        assert outcome["accepted"] == 1
        assert outcome["outstanding"] == 2
        assert outbox.stats().pending_events == 2
        assert outbox.batch(batch_id)["status"] == "partial"

    def test_unacknowledged_events_are_reclaimable(self, outbox: Outbox) -> None:
        outbox.add_events([event(i) for i in range(2)])
        batch_id, events = outbox.claim_batch(
            "device-test-001", max_events=10, max_bytes=100_000
        )
        outbox.complete_batch(batch_id, accepted_ids=[events[0].event_id])

        second = outbox.claim_batch("device-test-001", max_events=10, max_bytes=100_000)
        assert second is not None
        assert [e.event_id for e in second[1]] == [events[1].event_id]

    def test_release_returns_events_to_pending(self, outbox: Outbox) -> None:
        outbox.add_events([event(i) for i in range(2)])
        batch_id, _ = outbox.claim_batch(
            "device-test-001", max_events=10, max_bytes=100_000
        )
        outbox.release_batch(batch_id)
        assert outbox.stats().pending_events == 2
        assert outbox.batch(batch_id)["status"] == "failed"


class TestRetries:
    def test_attempt_counter_increments(self, outbox: Outbox) -> None:
        outbox.add_event(event(1))
        batch_id, events = outbox.claim_batch(
            "device-test-001", max_events=10, max_bytes=100_000
        )
        outbox.record_batch_attempt(batch_id, "connection refused")
        outbox.record_batch_attempt(batch_id, "connection refused")

        stored = outbox._conn.execute(
            "SELECT upload_attempts, last_upload_error FROM events WHERE event_id = ?",
            (events[0].event_id,),
        ).fetchone()
        assert stored["upload_attempts"] == 2
        assert stored["last_upload_error"] == "connection refused"

    def test_events_move_to_dead_letter_after_the_limit(self, outbox: Outbox) -> None:
        outbox.add_event(event(1))
        batch_id, _ = outbox.claim_batch(
            "device-test-001", max_events=10, max_bytes=100_000
        )
        for _ in range(3):
            outbox.record_batch_attempt(batch_id, "boom")

        outbox.complete_batch(batch_id, accepted_ids=[], dead_letter_after_attempts=3)
        stats = outbox.stats()
        assert stats.dead_letter_events == 1
        assert stats.pending_events == 0
        # Dead-lettered, but still stored: nothing is ever dropped.
        assert stats.total_events == 1

    def test_dead_letter_events_are_not_reclaimed(self, outbox: Outbox) -> None:
        outbox.add_event(event(1))
        batch_id, _ = outbox.claim_batch(
            "device-test-001", max_events=10, max_bytes=100_000
        )
        outbox.record_batch_attempt(batch_id, "boom")
        outbox.complete_batch(batch_id, accepted_ids=[], dead_letter_after_attempts=1)
        assert (
            outbox.claim_batch("device-test-001", max_events=10, max_bytes=1000) is None
        )


class TestCollectorStatus:
    def test_success_and_failure_are_tracked(self, outbox: Outbox) -> None:
        outbox.update_collector_status(
            "battery", availability="supported", enabled=True, succeeded=True, events=2
        )
        outbox.update_collector_status(
            "battery",
            availability="supported",
            enabled=True,
            succeeded=False,
            error="command failed",
        )
        row = outbox.collector_status()[0]
        assert row["success_count"] == 1
        assert row["error_count"] == 1
        assert row["last_error"] == "command failed"
        assert row["event_count"] == 2


class TestRetention:
    def test_disabled_by_default(self, outbox: Outbox) -> None:
        outbox.add_event(event(1))
        assert outbox.apply_retention(enabled=False, older_than_days=1) == 0
        assert outbox.count_events() == 1

    def test_does_not_touch_pending_events(self, outbox: Outbox) -> None:
        outbox.add_event(event(1))
        assert outbox.apply_retention(enabled=True, older_than_days=1) == 0
        assert outbox.count_events() == 1

    def test_removes_old_acknowledged_events_when_enabled(self, outbox: Outbox) -> None:
        item = event(1)
        outbox.add_event(item)
        batch_id, _ = outbox.claim_batch(
            "device-test-001", max_events=10, max_bytes=100_000
        )
        outbox.complete_batch(batch_id, accepted_ids=[item.event_id])
        # Backdate the acknowledgement so the cutoff applies.
        outbox._conn.execute(
            "UPDATE events SET acknowledged_at_utc = ?",
            (iso_utc(utc_now() - timedelta(days=30)),),
        )
        assert outbox.apply_retention(enabled=True, older_than_days=7) == 1
        assert outbox.count_events() == 0


class TestExport:
    def test_filters_by_time_and_source(self, outbox: Outbox) -> None:
        outbox.add_events([event(0), event(1, source="wifi")])
        assert len(outbox.export_events(sources=["wifi"])) == 1
        assert len(outbox.export_events(limit=1)) == 1

    def test_sql_injection_in_filter_is_inert(self, outbox: Outbox) -> None:
        outbox.add_event(event(0))
        assert outbox.export_events(sources=["battery'; DROP TABLE events; --"]) == []
        assert outbox.count_events() == 1


class TestStats:
    def test_empty_database(self, outbox: Outbox) -> None:
        stats = outbox.stats()
        assert stats.total_events == 0
        assert stats.pending_events == 0
        assert stats.last_successful_upload_utc is None

    def test_last_upload_time_is_recorded(self, outbox: Outbox) -> None:
        item = event(1)
        outbox.add_event(item)
        batch_id, _ = outbox.claim_batch(
            "device-test-001", max_events=10, max_bytes=100_000
        )
        outbox.complete_batch(batch_id, accepted_ids=[item.event_id])
        assert outbox.stats().last_successful_upload_utc is not None


def test_transaction_rolls_back_on_error(outbox: Outbox) -> None:
    outbox.add_event(event(1))
    with pytest.raises(RuntimeError), outbox.transaction() as conn:
        conn.execute("DELETE FROM events")
        raise RuntimeError("boom")
    assert outbox.count_events() == 1
