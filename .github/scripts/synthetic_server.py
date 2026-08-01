"""A synthetic Android Timeline ingestion server for CI only.

Implements just enough of the protocol to exercise the collector:
authenticated batch ingestion, per-event acknowledgement and idempotent
replay. It is **not** the real server -- that lives in
``resace3/android-timeline-home-assistant``. This exists so the emulator
job has something on the runner to upload to without any network egress.

Never run this outside CI: the token is a fixed synthetic string.
"""

from __future__ import annotations

import argparse
import gzip
import json
import pathlib
import sys
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_TOKEN = "synthetic-ci-token"  # noqa: S105 - CI-only fixture
MAX_BODY_BYTES = 2 * 1024 * 1024


class State:
    def __init__(self, token: str, path: pathlib.Path | None) -> None:
        self.token = token
        self.path = path
        self.events: dict[str, dict[str, Any]] = {}
        self.batches: list[str] = []
        self.rejections: list[dict[str, str]] = []

    def persist(self) -> None:
        if not self.path:
            return
        self.path.write_text(
            json.dumps(
                {
                    "events": self.events,
                    "batches": self.batches,
                    "rejections": self.rejections,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_handler(state: State) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            # Log the request line only. Headers are never logged, so a
            # bearer token cannot end up in a CI artifact.
            sys.stderr.write(f"[synthetic-server] {fmt % args}\n")

        def _send(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path.rstrip("/") == "/api/v1/health":
                self._send(
                    200,
                    {
                        "status": "ok",
                        "protocol_version": 1,
                        "server_version": "synthetic-ci",
                        "stored_events": len(state.events),
                    },
                )
                return
            self._send(404, {"detail": "not found"})

        def do_POST(self) -> None:
            if self.path.rstrip("/") != "/api/v1/events/batch":
                self._send(404, {"detail": "not found"})
                return

            if self.headers.get("Authorization") != f"Bearer {state.token}":
                self._send(401, {"detail": "invalid or missing bearer token"})
                return

            length = int(self.headers.get("Content-Length", "0"))
            if length > MAX_BODY_BYTES:
                self._send(413, {"detail": "payload too large"})
                return

            raw = self.rfile.read(length)
            if self.headers.get("Content-Encoding") == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    self._send(400, {"detail": "malformed gzip body"})
                    return

            try:
                batch = json.loads(raw)
            except json.JSONDecodeError as exc:
                self._send(400, {"detail": f"invalid JSON: {exc}"})
                return

            if not isinstance(batch, dict) or "events" not in batch:
                self._send(422, {"detail": "missing 'events'"})
                return
            if batch.get("protocol_version") != 1:
                self._send(422, {"detail": "unsupported protocol_version"})
                return

            accepted: list[dict[str, str]] = []
            rejected: list[dict[str, str]] = []
            for event in batch["events"]:
                event_id = str(event.get("event_id", ""))
                if not event_id:
                    rejected.append({"event_id": "", "reason": "missing event_id"})
                    continue
                status = "duplicate" if event_id in state.events else "stored"
                state.events[event_id] = event
                accepted.append({"event_id": event_id, "status": status})

            batch_id = str(batch.get("batch_id", ""))
            state.batches.append(batch_id)
            state.rejections.extend(rejected)
            state.persist()

            self._send(
                200,
                {
                    "protocol_version": 1,
                    "batch_id": batch_id,
                    "server_version": "synthetic-ci",
                    "received_time_utc": _now(),
                    "accepted": accepted,
                    "rejected": rejected,
                    "counts": {
                        "received": len(batch["events"]),
                        "stored": sum(1 for a in accepted if a["status"] == "stored"),
                        "duplicate": sum(
                            1 for a in accepted if a["status"] == "duplicate"
                        ),
                        "rejected": len(rejected),
                    },
                },
            )

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument(
        "--host",
        default="0.0.0.0",  # noqa: S104 - the emulator reaches the runner via 10.0.2.2
        help="bind address (CI runner only)",
    )
    parser.add_argument("--token", default=DEFAULT_TOKEN)
    parser.add_argument("--state", help="write received events to this JSON file")
    args = parser.parse_args(argv)

    state = State(args.token, pathlib.Path(args.state) if args.state else None)
    state.persist()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    sys.stderr.write(
        f"[synthetic-server] listening on {args.host}:{args.port} (CI fixture)\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.persist()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
