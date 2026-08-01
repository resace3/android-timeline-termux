"""The collection loop.

Deliberately simple: no threads, no async, no external scheduler. Termux
processes get killed unpredictably, so the daemon must be cheap to restart
and must never hold state that is not already in SQLite.
"""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from types import FrameType
from typing import Any

from .collectors import Collector, build_collectors
from .config import Config
from .database import Outbox
from .heartbeat import build_heartbeat
from .models import utc_now
from .uploader import Uploader, UploadError

__all__ = ["Scheduler", "SchedulerReport"]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SchedulerReport:
    """What one pass of the loop did. Returned so tests can assert on it."""

    ticks: int = 0
    events_collected: int = 0
    events_stored: int = 0
    heartbeats: int = 0
    uploads_attempted: int = 0
    events_uploaded: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticks": self.ticks,
            "events_collected": self.events_collected,
            "events_stored": self.events_stored,
            "heartbeats": self.heartbeats,
            "uploads_attempted": self.uploads_attempted,
            "events_uploaded": self.events_uploaded,
            "errors": list(self.errors),
        }


class Scheduler:
    """Runs collectors on their configured intervals and uploads batches."""

    def __init__(
        self,
        config: Config,
        outbox: Outbox,
        *,
        uploader: Uploader | None = None,
        collectors: list[Collector] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.outbox = outbox
        self.collectors = (
            collectors if collectors is not None else build_collectors(config)
        )
        self.uploader = uploader or Uploader(config, outbox)
        self._clock = clock
        self._sleep = sleep
        self._due: dict[str, float] = {}
        self._next_heartbeat = 0.0
        self._next_upload = 0.0
        self._stopping = False

    # ------------------------------------------------------------------
    # signals
    # ------------------------------------------------------------------

    def install_signal_handlers(self) -> None:
        """Stop cleanly on SIGTERM/SIGINT so no batch is left half-claimed."""

        def _handler(signum: int, _frame: FrameType | None) -> None:
            logger.info("received signal %s; finishing current tick", signum)
            self._stopping = True

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                logger.debug("could not install handler for signal %s", sig)

    def stop(self) -> None:
        self._stopping = True

    # ------------------------------------------------------------------
    # one tick
    # ------------------------------------------------------------------

    def collect_once(self, report: SchedulerReport | None = None) -> SchedulerReport:
        """Run every enabled collector once, regardless of interval."""
        result = report or SchedulerReport()
        for collector in self.collectors:
            events = collector.collect()
            stored = self.outbox.add_events(events)
            availability = collector.probe()
            failed = any(
                e.event_type in ("collection_error", "collection_unavailable")
                for e in events
            )
            self.outbox.update_collector_status(
                collector.source,
                availability=availability.value,
                enabled=collector.enabled,
                succeeded=not failed,
                events=len(events),
                error=(
                    str(events[0].payload.get("error", "unavailable"))
                    if failed and events
                    else None
                ),
            )
            result.events_collected += len(events)
            result.events_stored += stored
        return result

    def tick(self, report: SchedulerReport | None = None) -> SchedulerReport:
        """Run whatever is due right now."""
        result = report or SchedulerReport()
        result.ticks += 1
        now = self._clock()

        for collector in self.collectors:
            due_at = self._due.get(collector.source, 0.0)
            if now < due_at:
                continue
            self._due[collector.source] = now + collector.interval_seconds
            events = collector.collect()
            failed = any(
                e.event_type in ("collection_error", "collection_unavailable")
                for e in events
            )
            stored = self.outbox.add_events(events)
            self.outbox.update_collector_status(
                collector.source,
                availability=collector.probe().value,
                enabled=collector.enabled,
                succeeded=not failed,
                events=len(events),
                error=(
                    str(events[0].payload.get("error", "unavailable"))
                    if failed and events
                    else None
                ),
            )
            result.events_collected += len(events)
            result.events_stored += stored

        if now >= self._next_heartbeat:
            self._next_heartbeat = now + self.config.heartbeat_interval_seconds
            heartbeat = build_heartbeat(self.config, self.outbox, self.collectors)
            result.events_stored += self.outbox.add_events([heartbeat])
            result.events_collected += 1
            result.heartbeats += 1

        if now >= self._next_upload:
            self._next_upload = now + self.config.upload.interval_seconds
            result.uploads_attempted += 1
            try:
                upload = self.uploader.upload_pending()
                result.events_uploaded += upload.events_accepted
                result.errors.extend(upload.errors)
            except UploadError as exc:
                # Never fatal: the queue is durable, the network is not.
                logger.warning("upload failed: %s", exc)
                result.errors.append(str(exc))
            except Exception as exc:
                logger.exception("unexpected upload failure")
                result.errors.append(f"{type(exc).__name__}: {exc}")

        self.outbox.set_state("last_tick_utc", utc_now().isoformat())
        return result

    # ------------------------------------------------------------------
    # loop
    # ------------------------------------------------------------------

    def run(
        self, *, max_ticks: int | None = None, tick_seconds: float = 30.0
    ) -> SchedulerReport:
        """Loop until stopped (or until ``max_ticks``, used by tests)."""
        report = SchedulerReport()
        self._stopping = False
        while not self._stopping:
            self.tick(report)
            if max_ticks is not None and report.ticks >= max_ticks:
                break
            self._sleep(tick_seconds)

            retention = self.config.retention
            if retention.enabled:
                removed = self.outbox.apply_retention(
                    enabled=True,
                    older_than_days=retention.delete_acknowledged_after_days,
                )
                if removed:
                    logger.info("retention removed %d acknowledged events", removed)
        return report
