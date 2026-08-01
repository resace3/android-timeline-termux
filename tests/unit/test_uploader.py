"""Upload protocol: retries, backoff, idempotency and redaction."""

from __future__ import annotations

import gzip
import json
import logging
import random
from typing import Any

import pytest

from android_timeline.config import Config
from android_timeline.database import Outbox
from android_timeline.models import Event, utc_now
from android_timeline.uploader import Uploader, UploadError


class FakeTransport:
    """Records requests and replays scripted responses."""

    def __init__(self, uploader: Uploader) -> None:
        self.requests: list[dict[str, Any]] = []
        self.responses: list[Any] = []
        self._uploader = uploader
        uploader._request = self.handle  # type: ignore[method-assign]

    def handle(
        self,
        path: str,
        *,
        method: str = "POST",
        body: bytes | None = None,
        headers: dict[str, str],
    ) -> tuple[int, dict[str, Any]]:
        decoded = body or b""
        if headers.get("Content-Encoding") == "gzip":
            decoded = gzip.decompress(decoded)
        parsed = json.loads(decoded) if decoded else {}
        self.requests.append(
            {"path": path, "method": method, "headers": headers, "body": parsed}
        )
        if not self.responses:
            return 200, _ack(parsed)
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _ack(batch: dict[str, Any], *, accept: list[str] | None = None) -> dict[str, Any]:
    ids = accept if accept is not None else [e["event_id"] for e in batch["events"]]
    return {
        "protocol_version": 1,
        "batch_id": batch.get("batch_id", ""),
        "server_version": "0.1.0",
        "received_time_utc": "2026-03-15T00:00:00Z",
        "accepted": [{"event_id": i, "status": "stored"} for i in ids],
        "rejected": [],
        "counts": {
            "received": len(batch["events"]),
            "stored": len(ids),
            "duplicate": 0,
            "rejected": 0,
        },
    }


def add_events(outbox: Outbox, count: int) -> list[Event]:
    events = [
        Event.create(
            device_id="device-test-001",
            source="battery",
            event_type="battery_sample",
            payload={"percentage": index},
            event_time=utc_now(),
        )
        for index in range(count)
    ]
    outbox.add_events(events)
    return events


@pytest.fixture
def uploader(config: Config, outbox: Outbox) -> Uploader:
    return Uploader(config, outbox, sleep=lambda _s: None, rng=random.Random(1234))


@pytest.fixture
def transport(uploader: Uploader) -> FakeTransport:
    return FakeTransport(uploader)


class TestHeaders:
    def test_required_headers_are_present(
        self, uploader: Uploader, transport: FakeTransport, outbox: Outbox
    ) -> None:
        add_events(outbox, 1)
        uploader.upload_pending()
        headers = transport.requests[0]["headers"]
        assert headers["Authorization"].startswith("Bearer ")
        assert headers["X-Device-ID"] == "device-test-001"
        assert headers["X-Batch-ID"]
        assert headers["Content-Type"] == "application/json"
        assert headers["X-Protocol-Version"] == "1"

    def test_batch_id_header_matches_the_body(
        self, uploader: Uploader, transport: FakeTransport, outbox: Outbox
    ) -> None:
        add_events(outbox, 1)
        uploader.upload_pending()
        request = transport.requests[0]
        assert request["headers"]["X-Batch-ID"] == request["body"]["batch_id"]

    def test_token_is_never_placed_in_the_url(
        self, uploader: Uploader, transport: FakeTransport, outbox: Outbox
    ) -> None:
        add_events(outbox, 1)
        uploader.upload_pending()
        assert "?" not in transport.requests[0]["path"]
        assert "token" not in transport.requests[0]["path"]


class TestCompression:
    def test_small_batches_are_not_compressed(
        self, uploader: Uploader, transport: FakeTransport, outbox: Outbox
    ) -> None:
        uploader.config.server.compression_min_bytes = 10**9
        add_events(outbox, 1)
        uploader.upload_pending()
        assert "Content-Encoding" not in transport.requests[0]["headers"]

    def test_large_batches_are_gzipped(
        self, uploader: Uploader, transport: FakeTransport, outbox: Outbox
    ) -> None:
        uploader.config.server.compression_min_bytes = 1
        add_events(outbox, 5)
        uploader.upload_pending()
        assert transport.requests[0]["headers"]["Content-Encoding"] == "gzip"

    def test_oversized_body_is_rejected_locally(
        self, uploader: Uploader, transport: FakeTransport, outbox: Outbox
    ) -> None:
        add_events(outbox, 3)
        uploader.config.server.max_batch_bytes = 10
        uploader.config.server.compression = False
        result = uploader.upload_pending()
        assert any("exceeds the configured limit" in e for e in result.errors)


class TestIdempotency:
    def test_accepted_events_are_marked_uploaded(
        self, uploader: Uploader, transport: FakeTransport, outbox: Outbox
    ) -> None:
        add_events(outbox, 3)
        result = uploader.upload_pending()
        assert result.events_accepted == 3
        assert outbox.stats().pending_events == 0

    def test_second_run_sends_nothing(
        self, uploader: Uploader, transport: FakeTransport, outbox: Outbox
    ) -> None:
        add_events(outbox, 3)
        uploader.upload_pending()
        uploader.upload_pending()
        assert len(transport.requests) == 1

    def test_duplicate_status_counts_as_success(
        self, uploader: Uploader, transport: FakeTransport, outbox: Outbox
    ) -> None:
        events = add_events(outbox, 2)
        transport.responses = [
            (
                200,
                {
                    "protocol_version": 1,
                    "batch_id": "b",
                    "received_time_utc": "2026-03-15T00:00:00Z",
                    "accepted": [
                        {"event_id": events[0].event_id, "status": "duplicate"},
                        {"event_id": events[1].event_id, "status": "stored"},
                    ],
                    "rejected": [],
                    "counts": {"received": 2, "stored": 1, "duplicate": 1, "rejected": 0},
                },
            )
        ]
        uploader.upload_pending()
        assert outbox.stats().pending_events == 0

    def test_partial_acknowledgement_leaves_the_rest_queued(
        self, uploader: Uploader, transport: FakeTransport, outbox: Outbox
    ) -> None:
        events = add_events(outbox, 3)
        transport.responses = [
            (
                200,
                {
                    "protocol_version": 1,
                    "batch_id": "b",
                    "received_time_utc": "2026-03-15T00:00:00Z",
                    "accepted": [{"event_id": events[0].event_id, "status": "stored"}],
                    "rejected": [],
                    "counts": {"received": 3, "stored": 1, "duplicate": 0, "rejected": 0},
                },
            )
        ]
        uploader.upload_pending(max_batches=1)
        assert outbox.stats().pending_events == 2


class TestRetries:
    def test_retryable_error_is_retried_then_succeeds(
        self, uploader: Uploader, transport: FakeTransport, outbox: Outbox
    ) -> None:
        add_events(outbox, 1)
        transport.responses = [UploadError("503", retryable=True, status=503)]
        result = uploader.upload_pending()
        # One failed attempt, then a successful retry of the same batch.
        assert len(transport.requests) == 2
        assert (
            transport.requests[0]["body"]["batch_id"]
            == (transport.requests[1]["body"]["batch_id"])
        )
        assert result.events_accepted == 1

    def test_non_retryable_error_stops_immediately(
        self, uploader: Uploader, transport: FakeTransport, outbox: Outbox
    ) -> None:
        add_events(outbox, 1)
        transport.responses = [
            UploadError("401 invalid token", retryable=False, status=401)
        ] * 10
        result = uploader.upload_pending()
        assert result.batches_sent == 0
        assert any("invalid token" in e for e in result.errors)

    def test_offline_failure_preserves_the_queue(
        self, uploader: Uploader, transport: FakeTransport, outbox: Outbox
    ) -> None:
        add_events(outbox, 4)
        transport.responses = [UploadError("network down", retryable=True)] * 20
        uploader.upload_pending()
        # Nothing lost, nothing marked uploaded, ready for the next attempt.
        assert outbox.stats().pending_events == 4
        assert outbox.stats().uploaded_events == 0

    def test_later_retry_succeeds_after_an_outage(
        self, uploader: Uploader, transport: FakeTransport, outbox: Outbox
    ) -> None:
        add_events(outbox, 2)
        transport.responses = [UploadError("network down", retryable=True)] * 20
        uploader.upload_pending()
        transport.responses = []
        result = uploader.upload_pending()
        assert result.events_accepted == 2
        assert outbox.stats().pending_events == 0

    def test_attempts_are_capped(
        self, uploader: Uploader, transport: FakeTransport, outbox: Outbox
    ) -> None:
        uploader.config.upload.max_attempts = 3
        add_events(outbox, 1)
        calls: list[int] = []

        def failing(*_args: Any, **_kwargs: Any) -> tuple[int, dict[str, Any]]:
            calls.append(1)
            raise UploadError("503", retryable=True, status=503)

        uploader._request = failing  # type: ignore[method-assign]
        uploader.upload_pending()
        assert len(calls) == 3


class TestBackoff:
    def test_grows_exponentially(self, uploader: Uploader) -> None:
        uploader.config.upload.jitter_ratio = 0.0
        assert uploader.backoff_delay(1) == 2.0
        assert uploader.backoff_delay(2) == 4.0
        assert uploader.backoff_delay(3) == 8.0

    def test_is_capped(self, uploader: Uploader) -> None:
        uploader.config.upload.jitter_ratio = 0.0
        uploader.config.upload.max_backoff_seconds = 10.0
        assert uploader.backoff_delay(20) == 10.0

    def test_jitter_stays_within_the_configured_ratio(self, uploader: Uploader) -> None:
        uploader.config.upload.jitter_ratio = 0.25
        delays = [uploader.backoff_delay(3) for _ in range(200)]
        assert all(6.0 <= d <= 10.0 for d in delays)
        assert len(set(delays)) > 1  # actually jittered, not constant

    def test_jitter_never_returns_a_negative_delay(self, uploader: Uploader) -> None:
        uploader.config.upload.jitter_ratio = 1.0
        assert all(uploader.backoff_delay(1) >= 0.0 for _ in range(200))


class TestLoggingRedaction:
    def test_authorization_header_never_reaches_the_log(
        self,
        uploader: Uploader,
        outbox: Outbox,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        token = uploader.config.server.token()
        captured: list[dict[str, str]] = []

        original = uploader._headers

        def spy(**kwargs: Any) -> dict[str, str]:
            headers = original(**kwargs)
            captured.append(headers)
            return headers

        monkeypatch.setattr(uploader, "_headers", spy)
        FakeTransport(uploader)
        add_events(outbox, 1)

        with caplog.at_level(logging.DEBUG, logger="android_timeline.uploader"):
            uploader.upload_pending()

        assert captured and captured[0]["Authorization"] == f"Bearer {token}"
        assert token not in caplog.text
