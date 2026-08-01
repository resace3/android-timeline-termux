"""Command line interface.

``android-timeline <command>``. Every command is safe to run repeatedly;
none of them print a bearer token.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import COLLECTOR_VERSION, PROTOCOL_VERSION, SCHEMA_VERSION
from .collectors import build_collectors
from .config import (
    Config,
    ConfigError,
    default_config_path,
    load_config,
)
from .database import Outbox
from .heartbeat import build_heartbeat, is_termux
from .models import Event, QualityFlag, parse_iso_utc
from .redaction import redact_token
from .scheduler import Scheduler
from .uploader import Uploader, UploadError

__all__ = ["EXAMPLE_CONFIG", "main"]

logger = logging.getLogger("android_timeline")

EXAMPLE_CONFIG = """\
# android-timeline collector configuration.
#
# SECRETS DO NOT BELONG IN THIS FILE.
#   * the device bearer token lives in server.token_file (chmod 600)
#   * the pseudonymisation salt lives in privacy.salt_file (chmod 600)
# Both may instead be supplied via $ANDROID_TIMELINE_TOKEN / $ANDROID_TIMELINE_SALT.

[device]
# Pseudonymous. Never a serial number, IMEI, phone number or your name.
device_id = "device-example-001"

[server]
# Must be https:// outside of automated tests.
base_url = "https://homeassistant.example.invalid:8099"
token_file = "~/.config/android-timeline/token"
timeout_seconds = 30.0
verify_tls = true
# Enabling this alone does nothing: plaintext HTTP additionally requires
# $ANDROID_TIMELINE_INSECURE_TEST_MODE and a loopback host.
allow_insecure_test_endpoint = false
max_batch_events = 500
max_batch_bytes = 1048576
compression = true
compression_min_bytes = 4096

[upload]
max_attempts = 6
initial_backoff_seconds = 2.0
max_backoff_seconds = 900.0
jitter_ratio = 0.25
dead_letter_after_attempts = 25
interval_seconds = 300

[privacy]
salt_file = "~/.config/android-timeline/salt"
store_message_bodies = false
store_contact_names = false
# disabled | coarse | geohash | precise | raw   (raw is full coordinates)
location_mode = "disabled"
geohash_precision = 6
coarse_decimal_places = 2

[retention]
# Off by default: acknowledged raw events are kept forever.
enabled = false
delete_acknowledged_after_days = 0

[runtime]
heartbeat_interval_seconds = 900

[collectors.battery]
enabled = true
interval_seconds = 300

[collectors.wifi]
enabled = true
interval_seconds = 300

[collectors.sensors]
enabled = false
interval_seconds = 900
samples = 5
max_sensors = 4

[collectors.location]
enabled = false
interval_seconds = 900
provider = "network"
request = "last"

# Sensitive sources. Metadata only, and off by default.
[collectors.calls]
enabled = false
interval_seconds = 3600
limit = 50

[collectors.sms]
enabled = false
interval_seconds = 3600
limit = 50
"""


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _emit(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    elif isinstance(payload, dict):
        for key, value in payload.items():
            print(f"{key}: {value}")
    else:
        print(payload)


def _load(args: argparse.Namespace) -> Config:
    return load_config(getattr(args, "config", None))


def _open_outbox(config: Config) -> Outbox:
    return Outbox(config.database_path)


def _secure(path: Path) -> None:
    """Best-effort 0600. Silently ignored on filesystems without POSIX modes."""
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - e.g. Android shared storage
        logger.debug("could not chmod %s", path)


# ----------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    if args.print_example:
        sys.stdout.write(EXAMPLE_CONFIG)
        return 0

    target = Path(args.config).expanduser() if args.config else default_config_path()
    if target.exists() and not args.force:
        print(
            f"{target} already exists; refusing to overwrite. "
            "Re-run with --force to replace it.",
            file=sys.stderr,
        )
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    _secure(target)
    print(f"wrote {target}")
    print("Next steps:")
    print("  1. set device.device_id and server.base_url")
    print(f"  2. write the enrollment token to {target.parent / 'token'} (chmod 600)")
    print(f"  3. create a random salt in {target.parent / 'salt'} (chmod 600)")
    print("  4. run: android-timeline doctor")
    return 0


def cmd_collect_once(args: argparse.Namespace) -> int:
    config = _load(args)
    with _open_outbox(config) as outbox:
        collectors = build_collectors(config)
        scheduler = Scheduler(config, outbox, collectors=collectors)
        report = scheduler.collect_once()
        if args.heartbeat:
            heartbeat = build_heartbeat(config, outbox, collectors)
            report.events_stored += outbox.add_events([heartbeat])
            report.events_collected += 1
            report.heartbeats += 1
        _emit(report.to_dict(), args.json)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = _load(args)
    with _open_outbox(config) as outbox:
        scheduler = Scheduler(config, outbox)
        scheduler.install_signal_handlers()
        report = scheduler.run(max_ticks=args.max_ticks, tick_seconds=args.tick_seconds)
        _emit(report.to_dict(), args.json)
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    config = _load(args)
    with _open_outbox(config) as outbox:
        uploader = Uploader(config, outbox)
        try:
            result = uploader.upload_pending(max_batches=args.max_batches)
        except (UploadError, ConfigError) as exc:
            print(f"upload failed: {exc}", file=sys.stderr)
            return 2
        _emit(result.to_dict(), args.json)
        return 0 if not result.errors else 2


def cmd_status(args: argparse.Namespace) -> int:
    config = _load(args)
    with _open_outbox(config) as outbox:
        payload = {
            "collector_version": COLLECTOR_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "schema_version": SCHEMA_VERSION,
            "device_id": config.device.device_id,
            "database": str(config.database_path),
            "database_schema_version": outbox.schema_version(),
            "queue": outbox.stats().to_dict(),
            "collectors": outbox.collector_status(),
            "enabled_collectors": config.enabled_collectors(),
        }
        _emit(payload, args.json)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Diagnose the installation without ever printing a credential."""
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any = "", *, warn: bool = False) -> None:
        checks.append(
            {
                "check": name,
                "status": "ok" if ok else ("warn" if warn else "fail"),
                "detail": detail,
            }
        )

    termux = is_termux()
    check(
        "termux_environment",
        termux,
        {"prefix": os.environ.get("PREFIX", "")},
        warn=not termux,
    )

    try:
        config = _load(args)
    except ConfigError as exc:
        check("configuration", False, str(exc))
        _emit({"checks": checks, "healthy": False}, args.json)
        return 1

    check("configuration", True, {"path": str(config.source_path)})

    # Termux:API command availability
    collectors = build_collectors(config, only_enabled=False)
    command_state: dict[str, Any] = {}
    for collector in collectors:
        command_state[collector.source] = {
            "enabled": collector.enabled,
            "availability": collector.probe().value,
            "missing_commands": collector.missing_commands(),
        }
    unavailable = [
        name
        for name, state in command_state.items()
        if state["enabled"] and state["availability"] == "unavailable"
    ]
    check(
        "termux_api_commands",
        not unavailable,
        command_state,
        warn=bool(unavailable),
    )

    # Database
    try:
        with _open_outbox(config) as outbox:
            stats = outbox.stats()
            check(
                "database_writable",
                True,
                {
                    "path": str(config.database_path),
                    "schema_version": outbox.schema_version(),
                },
            )
            check("queue", True, stats.to_dict())
            check(
                "last_successful_upload",
                stats.last_successful_upload_utc is not None,
                {"at": stats.last_successful_upload_utc},
                warn=True,
            )
    except Exception as exc:
        check("database_writable", False, str(exc))

    # Credentials -- presence only, never the value.
    has_token = config.server.has_token()
    token_detail: dict[str, Any] = {"configured": has_token}
    if has_token:
        token_detail["fingerprint"] = redact_token(config.server.token())
    check("device_token", has_token, token_detail)

    check(
        "pseudonymisation_salt",
        config.privacy.has_salt(),
        {"configured": config.privacy.has_salt()},
        warn=True,
    )

    # Server reachability (unauthenticated health probe)
    if args.check_server:
        try:
            with _open_outbox(config) as outbox:
                health = Uploader(config, outbox).check_health()
            check("server_reachable", True, health)
        except Exception as exc:
            check("server_reachable", False, str(exc)[:300])
    else:
        check(
            "server_reachable",
            True,
            "skipped (pass --check-server to probe the server)",
            warn=True,
        )

    healthy = all(entry["status"] != "fail" for entry in checks)
    _emit(
        {
            "healthy": healthy,
            "collector_version": COLLECTOR_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "checks": checks,
        },
        args.json,
    )
    return 0 if healthy else 1


def cmd_export(args: argparse.Namespace) -> int:
    config = _load(args)
    with _open_outbox(config) as outbox:
        events = outbox.export_events(
            start_utc=args.start,
            end_utc=args.end,
            sources=args.source or None,
            limit=args.limit,
        )
    if args.output:
        Path(args.output).expanduser().write_text(
            json.dumps(events, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(f"wrote {len(events)} events to {args.output}")
    else:
        print(json.dumps(events, indent=2, sort_keys=True))
    return 0


def cmd_ingest_event(args: argparse.Namespace) -> int:
    """Local ingestion point for Tasker and other on-device automations."""
    config = _load(args)
    try:
        payload = json.loads(args.payload) if args.payload else {}
    except json.JSONDecodeError as exc:
        print(f"--payload is not valid JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(payload, dict):
        print("--payload must be a JSON object", file=sys.stderr)
        return 1

    event_time = None
    if args.event_time:
        try:
            event_time = parse_iso_utc(args.event_time)
        except ValueError as exc:
            print(
                f"--event-time is not an RFC 3339 UTC timestamp: {exc}", file=sys.stderr
            )
            return 1

    flags = list(args.quality_flag or [])
    unknown = sorted(set(flags) - QualityFlag.ALL)
    if unknown:
        print(f"unknown quality flag(s): {', '.join(unknown)}", file=sys.stderr)
        return 1

    event = Event.create(
        device_id=config.device.device_id,
        source=args.source,
        event_type=args.type,
        payload=payload,
        event_time=event_time,
        quality_flags=flags,
    )
    with _open_outbox(config) as outbox:
        stored = outbox.add_event(event)
    _emit(
        {
            "event_id": event.event_id,
            "stored": stored,
            "duplicate": not stored,
            "event_time_utc": event.event_time_utc,
        },
        args.json,
    )
    return 0


# ----------------------------------------------------------------------
# parser
# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="android-timeline",
        description=(
            "Offline-first Android event collector for Termux. "
            "Uploads authenticated batches to a Home Assistant app."
        ),
    )
    parser.add_argument("--version", action="version", version=COLLECTOR_VERSION)
    parser.add_argument(
        "--config", help="path to config.toml (default: ~/.config/android-timeline)"
    )
    parser.add_argument("--json", action="store_true", help="emit JSON output")
    parser.add_argument(
        "--verbose", "-v", action="count", default=0, help="increase log verbosity"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create a configuration file")
    p_init.add_argument("--force", action="store_true", help="overwrite existing config")
    p_init.add_argument(
        "--print-example",
        action="store_true",
        help="write the example config to stdout and exit",
    )
    p_init.set_defaults(func=cmd_init)

    p_collect = sub.add_parser(
        "collect-once", help="run every enabled collector a single time"
    )
    p_collect.add_argument(
        "--heartbeat", action="store_true", help="also emit a heartbeat event"
    )
    p_collect.set_defaults(func=cmd_collect_once)

    p_run = sub.add_parser("run", help="run the collection loop")
    p_run.add_argument("--max-ticks", type=int, default=None)
    p_run.add_argument("--tick-seconds", type=float, default=30.0)
    p_run.set_defaults(func=cmd_run)

    p_upload = sub.add_parser("upload", help="upload pending events now")
    p_upload.add_argument("--max-batches", type=int, default=10)
    p_upload.set_defaults(func=cmd_upload)

    p_status = sub.add_parser("status", help="show queue and collector status")
    p_status.set_defaults(func=cmd_status)

    p_doctor = sub.add_parser("doctor", help="diagnose the installation")
    p_doctor.add_argument(
        "--check-server",
        action="store_true",
        help="probe the server health endpoint (no credentials are sent)",
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_export = sub.add_parser("export", help="export raw events as JSON")
    p_export.add_argument("--start", help="inclusive RFC 3339 UTC lower bound")
    p_export.add_argument("--end", help="exclusive RFC 3339 UTC upper bound")
    p_export.add_argument("--source", action="append", help="filter by source")
    p_export.add_argument("--limit", type=int, default=10_000)
    p_export.add_argument("--output", help="write to a file instead of stdout")
    p_export.set_defaults(func=cmd_export)

    p_ingest = sub.add_parser(
        "ingest-event", help="record an externally-observed event (e.g. from Tasker)"
    )
    p_ingest.add_argument("--source", required=True, help="e.g. tasker")
    p_ingest.add_argument("--type", required=True, help="e.g. screen_on")
    p_ingest.add_argument("--payload", default="{}", help="JSON object")
    p_ingest.add_argument("--event-time", help="RFC 3339 UTC time the event occurred")
    p_ingest.add_argument(
        "--quality-flag", action="append", help="repeatable quality flag"
    )
    p_ingest.set_defaults(func=cmd_ingest_event)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    level = logging.WARNING
    if args.verbose == 1:
        level = logging.INFO
    elif args.verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
