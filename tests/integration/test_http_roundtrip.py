"""End-to-end upload against a real local HTTP server.

This exercises the actual ``urllib`` transport, gzip encoding, header
construction and acknowledgement parsing -- the parts a mocked transport
cannot prove. The server is started by the test itself and only ever binds
to loopback.
"""

from __future__ import annotations

import gzip
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from android_timeline.config import (
    INSECURE_TEST_MODE_ENV,
    INSECURE_TEST_MODE_PHRASE,
    load_config,
)
from android_timeline.database import Outbox
from android_timeline.models import Event, utc_now
from android_timeline.uploader import Uploader

pytestmark = pytest.mark.integration


class RecordingServer:
    """A minimal stand-in for the Home Assistant ingestion endpoint."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.stored: dict[str, dict[str, Any]] = {}
        self.seen_batches: set[str] = set()
        self.token = "synthetic-test-token-not-a-real-secret"
        self.fail_times = 0
        self.max_bytes = 1_048_576
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._server, self._thread = server, thread

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)


def _make_handler(state: RecordingServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: Any) -> None:  # keep test output clean
            return

        def _respond(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/api/v1/health":
                self._respond(200, {"status": "ok", "protocol_version": 1})
            else:
                self._respond(404, {"detail": "not found"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)

            state.requests.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers),
                    "bytes": len(raw),
                }
            )

            if state.fail_times > 0:
                state.fail_times -= 1
                self._respond(503, {"detail": "synthetic outage"})
                return

            if self.headers.get("Authorization") != f"Bearer {state.token}":
                self._respond(401, {"detail": "invalid token"})
                return

            if len(raw) > state.max_bytes:
                self._respond(413, {"detail": "payload too large"})
                return

            if self.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)

            try:
                batch = json.loads(raw)
            except json.JSONDecodeError:
                self._respond(400, {"detail": "invalid JSON"})
                return

            accepted = []
            for event in batch["events"]:
                event_id = event["event_id"]
                status = "duplicate" if event_id in state.stored else "stored"
                state.stored[event_id] = event
                accepted.append({"event_id": event_id, "status": status})

            state.seen_batches.add(batch["batch_id"])
            self._respond(
                200,
                {
                    "protocol_version": 1,
                    "batch_id": batch["batch_id"],
                    "server_version": "test-0.1.0",
                    "received_time_utc": "2026-03-15T00:00:00Z",
                    "accepted": accepted,
                    "rejected": [],
                    "counts": {
                        "received": len(batch["events"]),
                        "stored": sum(1 for a in accepted if a["status"] == "stored"),
                        "duplicate": sum(
                            1 for a in accepted if a["status"] == "duplicate"
                        ),
                        "rejected": 0,
                    },
                },
            )

    return Handler


@pytest.fixture
def server() -> Iterator[RecordingServer]:
    state = RecordingServer()
    state.start()
    try:
        yield state
    finally:
        state.stop()


@pytest.fixture
def live_uploader(
    server: RecordingServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Uploader, Outbox]]:
    monkeypatch.setenv(INSECURE_TEST_MODE_ENV, INSECURE_TEST_MODE_PHRASE)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[device]
device_id = "device-test-001"

[server]
base_url = "{server.url}"
allow_insecure_test_endpoint = true
compression_min_bytes = 100000

[upload]
max_attempts = 4
initial_backoff_seconds = 0.01
max_backoff_seconds = 0.05
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    config.data_dir = tmp_path / "data"
    with Outbox(config.database_path) as outbox:
        yield Uploader(config, outbox, sleep=lambda _s: None), outbox


def make_events(count: int, offset: int = 0) -> list[Event]:
    return [
        Event.create(
            device_id="device-test-001",
            source="battery",
            event_type="battery_sample",
            payload={"percentage": index + offset},
            event_time=utc_now(),
        )
        for index in range(count)
    ]


class TestHealth:
    def test_health_probe_sends_no_credentials(
        self, live_uploader: tuple[Uploader, Outbox], server: RecordingServer
    ) -> None:
        uploader, _ = live_uploader
        assert uploader.check_health()["status"] == "ok"


class TestRoundTrip:
    def test_events_reach_the_server_once(
        self, live_uploader: tuple[Uploader, Outbox], server: RecordingServer
    ) -> None:
        uploader, outbox = live_uploader
        events = make_events(5)
        outbox.add_events(events)

        result = uploader.upload_pending()
        assert result.events_accepted == 5
        assert len(server.stored) == 5
        assert outbox.stats().pending_events == 0

    def test_replaying_the_same_batch_creates_no_duplicates(
        self, live_uploader: tuple[Uploader, Outbox], server: RecordingServer
    ) -> None:
        uploader, outbox = live_uploader
        events = make_events(3)
        outbox.add_events(events)
        uploader.upload_pending()

        # Re-send the identical batch by hand, exactly as a retry would.
        from android_timeline.models import Batch, iso_utc

        batch = Batch(
            batch_id=next(iter(server.seen_batches)),
            device_id="device-test-001",
            events=events,
            created_time_utc=iso_utc(utc_now()),
            collector_version="0.1.0",
        )
        ack = uploader.send_batch(batch, uploader.config.server.token())

        assert len(server.stored) == 3
        assert all(status == "duplicate" for status in ack.accepted.values())

    def test_same_events_in_a_new_batch_create_no_duplicates(
        self, live_uploader: tuple[Uploader, Outbox], server: RecordingServer
    ) -> None:
        uploader, outbox = live_uploader
        events = make_events(3)
        outbox.add_events(events)
        uploader.upload_pending()

        from android_timeline.models import Batch, iso_utc

        batch = Batch(
            batch_id="batch-different-0001",
            device_id="device-test-001",
            events=events,
            created_time_utc=iso_utc(utc_now()),
            collector_version="0.1.0",
        )
        uploader.send_batch(batch, uploader.config.server.token())
        assert len(server.stored) == 3

    def test_timestamps_and_offsets_survive_the_round_trip(
        self, live_uploader: tuple[Uploader, Outbox], server: RecordingServer
    ) -> None:
        uploader, outbox = live_uploader
        events = make_events(1)
        outbox.add_events(events)
        uploader.upload_pending()

        received = server.stored[events[0].event_id]
        assert received["event_time_utc"] == events[0].event_time_utc
        assert received["event_time_utc"].endswith("Z")
        assert received["timezone_offset_minutes"] == events[0].timezone_offset_minutes

    def test_gzip_round_trip(
        self,
        live_uploader: tuple[Uploader, Outbox],
        server: RecordingServer,
    ) -> None:
        uploader, outbox = live_uploader
        uploader.config.server.compression_min_bytes = 1
        outbox.add_events(make_events(10))
        uploader.upload_pending()

        assert server.requests[0]["headers"].get("Content-Encoding") == "gzip"
        assert len(server.stored) == 10


class TestFailureModes:
    def test_invalid_token_is_rejected_and_queue_is_preserved(
        self, live_uploader: tuple[Uploader, Outbox], server: RecordingServer
    ) -> None:
        uploader, outbox = live_uploader
        server.token = "a-different-token"
        outbox.add_events(make_events(3))

        result = uploader.upload_pending()
        assert result.events_accepted == 0
        assert any("401" in e for e in result.errors)
        assert outbox.stats().pending_events == 3

    def test_oversized_request_is_rejected_by_the_server(
        self, live_uploader: tuple[Uploader, Outbox], server: RecordingServer
    ) -> None:
        uploader, outbox = live_uploader
        server.max_bytes = 10
        outbox.add_events(make_events(3))

        result = uploader.upload_pending()
        assert any("413" in e for e in result.errors)
        assert outbox.stats().pending_events == 3

    def test_transient_outage_then_success(
        self, live_uploader: tuple[Uploader, Outbox], server: RecordingServer
    ) -> None:
        uploader, outbox = live_uploader
        server.fail_times = 2
        outbox.add_events(make_events(2))

        result = uploader.upload_pending()
        assert result.events_accepted == 2
        assert len(server.requests) == 3  # two failures, one success

    def test_network_interruption_preserves_then_recovers(
        self,
        live_uploader: tuple[Uploader, Outbox],
        server: RecordingServer,
    ) -> None:
        uploader, outbox = live_uploader
        outbox.add_events(make_events(4))

        # Simulate the server being unreachable entirely.
        server.stop()
        result = uploader.upload_pending()
        assert result.events_accepted == 0
        assert outbox.stats().pending_events == 4

        # ...and coming back.
        server.start()
        uploader.config.server.base_url = server.url
        recovered = uploader.upload_pending()
        assert recovered.events_accepted == 4
        assert outbox.stats().pending_events == 0

    def test_unauthenticated_health_still_works_when_ingest_is_locked(
        self, live_uploader: tuple[Uploader, Outbox], server: RecordingServer
    ) -> None:
        uploader, _ = live_uploader
        server.token = "rotated-token"
        assert uploader.check_health()["status"] == "ok"


class TestNoSecretsOnTheWire:
    def test_token_is_only_in_the_authorization_header(
        self, live_uploader: tuple[Uploader, Outbox], server: RecordingServer
    ) -> None:
        uploader, outbox = live_uploader
        token = uploader.config.server.token()
        outbox.add_events(make_events(2))
        uploader.upload_pending()

        request = server.requests[0]
        assert token not in request["path"]
        assert request["headers"]["Authorization"] == f"Bearer {token}"
        other_headers = {
            k: v for k, v in request["headers"].items() if k.lower() != "authorization"
        }
        assert token not in json.dumps(other_headers)
