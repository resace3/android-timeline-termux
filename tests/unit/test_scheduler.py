"""Scheduler and heartbeat behaviour."""

from __future__ import annotations

import json

import pytest

from android_timeline.collectors import MockCommandRunner, build_collectors
from android_timeline.config import Config
from android_timeline.database import Outbox
from android_timeline.heartbeat import build_heartbeat, is_termux
from android_timeline.scheduler import Scheduler
from android_timeline.uploader import Uploader, UploadError


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class NullUploader(Uploader):
    """An uploader that never touches the network."""

    def __init__(self, config: Config, outbox: Outbox, *, fail: bool = False) -> None:
        super().__init__(config, outbox, sleep=lambda _s: None)
        self.calls = 0
        self.fail = fail

    def upload_pending(self, *, max_batches: int = 10):  # type: ignore[override]
        self.calls += 1
        if self.fail:
            raise UploadError("synthetic outage", retryable=True)
        from android_timeline.uploader import UploadResult

        return UploadResult()


@pytest.fixture
def scheduler(
    config: Config, outbox: Outbox, mock_runner: MockCommandRunner
) -> Scheduler:
    return Scheduler(
        config,
        outbox,
        uploader=NullUploader(config, outbox),
        collectors=build_collectors(config, mock_runner),
        clock=FakeClock(),
        sleep=lambda _s: None,
    )


class TestCollectOnce:
    def test_runs_every_enabled_collector(self, scheduler: Scheduler) -> None:
        report = scheduler.collect_once()
        assert report.events_collected > 0
        assert report.events_stored == report.events_collected

    def test_records_collector_status(self, scheduler: Scheduler, outbox: Outbox) -> None:
        scheduler.collect_once()
        sources = {row["source"] for row in outbox.collector_status()}
        assert {"battery", "wifi", "location", "sensors", "calls", "sms"} <= sources

    def test_repeated_collection_never_duplicates_an_id(
        self, scheduler: Scheduler, outbox: Outbox
    ) -> None:
        for _ in range(3):
            scheduler.collect_once()
        rows = outbox.export_events(limit=10_000)
        ids = [row["event_id"] for row in rows]
        assert len(ids) == len(set(ids))
        assert outbox.count_events() == len(ids)


class TestTick:
    def test_respects_collector_intervals(self, scheduler: Scheduler) -> None:
        clock = scheduler._clock
        assert isinstance(clock, FakeClock)

        first = scheduler.tick()
        assert first.events_collected > 0

        # Nothing is due yet: the tick happens but collects nothing.
        second = scheduler.tick()
        assert second.ticks == 1
        assert second.events_collected == 0

        clock.advance(120)
        third = scheduler.tick()
        assert third.events_collected > 0

    def test_heartbeat_is_emitted(self, scheduler: Scheduler, outbox: Outbox) -> None:
        report = scheduler.tick()
        assert report.heartbeats == 1
        rows = outbox.export_events(sources=["heartbeat"])
        assert len(rows) == 1

    def test_upload_failure_is_not_fatal(
        self, config: Config, outbox: Outbox, mock_runner: MockCommandRunner
    ) -> None:
        scheduler = Scheduler(
            config,
            outbox,
            uploader=NullUploader(config, outbox, fail=True),
            collectors=build_collectors(config, mock_runner),
            clock=FakeClock(),
            sleep=lambda _s: None,
        )
        report = scheduler.tick()
        assert report.errors
        assert report.events_stored > 0  # collection still happened

    def test_run_stops_after_max_ticks(self, scheduler: Scheduler) -> None:
        report = scheduler.run(max_ticks=3, tick_seconds=0.0)
        assert report.ticks == 3

    def test_stop_ends_the_loop_after_the_current_tick(
        self, scheduler: Scheduler
    ) -> None:
        # A SIGTERM arriving mid-run must not abandon work in progress.
        scheduler._sleep = lambda _s: scheduler.stop()
        report = scheduler.run(max_ticks=10, tick_seconds=0.0)
        assert report.ticks == 1


class TestHeartbeat:
    def test_contains_queue_and_collector_state(
        self, config: Config, outbox: Outbox, mock_runner: MockCommandRunner
    ) -> None:
        collectors = build_collectors(config, mock_runner)
        event = build_heartbeat(config, outbox, collectors)
        payload = event.payload

        assert payload["collector_version"]
        assert payload["protocol_version"] == 1
        assert "queue" in payload
        assert set(payload["enabled_collectors"]) == {
            "battery",
            "calls",
            "location",
            "sensors",
            "sms",
            "wifi",
        }
        assert payload["collectors"]["battery"]["availability"] == "mocked"

    def test_never_contains_credentials(
        self, config: Config, outbox: Outbox, mock_runner: MockCommandRunner
    ) -> None:
        event = build_heartbeat(config, outbox, build_collectors(config, mock_runner))
        serialised = json.dumps(event.payload).lower()
        assert config.server.token().lower() not in serialised
        assert config.privacy.salt().lower() not in serialised
        assert "authorization" not in serialised
        assert "bearer" not in serialised

    def test_reports_salt_presence_not_value(
        self, config: Config, outbox: Outbox, mock_runner: MockCommandRunner
    ) -> None:
        event = build_heartbeat(config, outbox, build_collectors(config, mock_runner))
        assert event.payload["salt_configured"] is True

    def test_queue_depth_is_reported(
        self, config: Config, outbox: Outbox, mock_runner: MockCommandRunner
    ) -> None:
        collectors = build_collectors(config, mock_runner)
        Scheduler(config, outbox, collectors=collectors).collect_once()
        event = build_heartbeat(config, outbox, collectors)
        assert event.payload["queue"]["pending_events"] > 0


def test_is_termux_is_false_on_a_plain_runner() -> None:
    # CI runs on Linux without a Termux prefix; the check must not be fooled
    # by an environment variable alone.
    assert is_termux() is False
