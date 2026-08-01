"""Deterministic synthetic day used by every test layer and by CI.

Nothing here resembles real personal data: identifiers are of the form
``device-test-001``, ``+1-555-0100`` (the reserved fictional range),
``example-network`` and ``place-home-synthetic``.

The day deliberately contains the awkward cases the pipeline must handle:

* a **two-hour gap** (02:00-04:00 UTC) with no events from any source
* a **late-arriving** event, observed at 05:00 but collected at 20:00
* an exact **duplicate** event repeated in the stream
* an event flagged for **replay in a second batch** (see ``REPLAY_EVENT_ID``)

Run directly to emit the day as JSON::

    python tests/fixtures/synthetic_day.py --output day.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:  # pragma: no cover - import shim for CI
    sys.path.insert(0, str(_REPO_ROOT))

from android_timeline.models import (  # noqa: E402
    SCHEMA_VERSION,
    QualityFlag,
    deterministic_event_id,
    iso_utc,
)

__all__ = [
    "DAY_START",
    "DEVICE_ID",
    "GAP_END",
    "GAP_START",
    "REPLAY_EVENT_ID",
    "SyntheticDay",
    "build_synthetic_day",
]

DEVICE_ID = "device-test-001"
DAY_START = datetime(2026, 3, 15, 0, 0, 0, tzinfo=UTC)
DAY_END = DAY_START + timedelta(days=1)

#: The intentional outage. No source produces anything in this window.
GAP_START = DAY_START.replace(hour=2)
GAP_END = DAY_START.replace(hour=4)

#: Charging window used to assert charging-minutes features.
CHARGE_START = DAY_START.replace(hour=6)
CHARGE_END = DAY_START.replace(hour=8)

#: Wi-Fi disconnection window.
WIFI_OFF_START = DAY_START.replace(hour=12)
WIFI_OFF_END = DAY_START.replace(hour=14)

TIMEZONE_OFFSET_MINUTES = -240  # fixed so the fixture is reproducible


def _in_gap(when: datetime) -> bool:
    return GAP_START <= when < GAP_END


def _event(
    source: str,
    event_type: str,
    when: datetime,
    payload: dict[str, Any],
    *,
    collected: datetime | None = None,
    quality_flags: list[str] | None = None,
) -> dict[str, Any]:
    event_time = iso_utc(when)
    return {
        "event_id": deterministic_event_id(
            DEVICE_ID, source, event_type, event_time, payload
        ),
        "device_id": DEVICE_ID,
        "source": source,
        "event_type": event_type,
        "event_time_utc": event_time,
        "collected_time_utc": iso_utc(collected or when),
        "timezone_offset_minutes": TIMEZONE_OFFSET_MINUTES,
        "schema_version": SCHEMA_VERSION,
        "quality_flags": sorted(set(quality_flags or [])),
        "payload": payload,
    }


class SyntheticDay(dict):
    """Result of :func:`build_synthetic_day`: events plus expected values."""

    @property
    def events(self) -> list[dict[str, Any]]:
        return self["events"]

    @property
    def unique_event_ids(self) -> set[str]:
        return {e["event_id"] for e in self["events"]}


def build_synthetic_day() -> SyntheticDay:
    """Build the canonical synthetic day. Fully deterministic."""
    events: list[dict[str, Any]] = []

    # -- battery every 15 minutes ------------------------------------------
    battery_samples = 0
    charging_samples = 0
    cursor = DAY_START
    percentage = 88
    while cursor < DAY_END:
        if not _in_gap(cursor):
            charging = CHARGE_START <= cursor < CHARGE_END
            if charging:
                percentage = min(100, percentage + 3)
                charging_samples += 1
            else:
                percentage = max(5, percentage - 1)
            events.append(
                _event(
                    "battery",
                    "battery_sample",
                    cursor,
                    {
                        "percentage": percentage,
                        "status": "CHARGING" if charging else "DISCHARGING",
                        "plugged": "AC" if charging else None,
                        "charging": charging,
                        "health": "GOOD",
                        "temperature_celsius": 27.5 if not charging else 31.0,
                    },
                )
            )
            battery_samples += 1
        cursor += timedelta(minutes=15)

    # -- wifi every 30 minutes ---------------------------------------------
    wifi_connected_samples = 0
    cursor = DAY_START
    while cursor < DAY_END:
        if not _in_gap(cursor):
            connected = not (WIFI_OFF_START <= cursor < WIFI_OFF_END)
            if connected:
                wifi_connected_samples += 1
            events.append(
                _event(
                    "wifi",
                    "wifi_sample",
                    cursor,
                    {
                        "connected": connected,
                        "ssid_pseudonym": "p_synthetic0000home" if connected else None,
                        "bssid_pseudonym": "p_synthetic0000bss" if connected else None,
                        "supplicant_state": "COMPLETED" if connected else "DISCONNECTED",
                        "rssi_dbm": -52 if connected else None,
                    },
                )
            )
        cursor += timedelta(minutes=30)

    # -- coarse location categories, hourly --------------------------------
    location_samples = 0
    cursor = DAY_START
    while cursor < DAY_END:
        if not _in_gap(cursor):
            at_work = 9 <= cursor.hour < 17
            events.append(
                _event(
                    "location",
                    "location_sample",
                    cursor,
                    {
                        "location_mode": "coarse",
                        "location_available": True,
                        "place_category": (
                            "place-work-synthetic" if at_work else "place-home-synthetic"
                        ),
                        "latitude": 10.12 if at_work else 10.15,
                        "longitude": 20.57 if at_work else 20.61,
                        "precision_decimal_places": 2,
                        "accuracy_metres": 25.0,
                        "provider": "network",
                    },
                    quality_flags=[QualityFlag.COARSE, QualityFlag.REDACTED],
                )
            )
            location_samples += 1
        cursor += timedelta(hours=1)

    # -- movement summaries every 3 hours ----------------------------------
    movement_samples = 0
    cursor = DAY_START
    while cursor < DAY_END:
        if not _in_gap(cursor):
            moving = 8 <= cursor.hour < 20
            events.append(
                _event(
                    "sensors",
                    "sensor_summary",
                    cursor,
                    {
                        "sensor": "Synthetic Accelerometer",
                        "available": True,
                        "samples_requested": 5,
                        "samples_returned": 5,
                        "magnitude_mean": 10.42 if moving else 9.81,
                        "magnitude_min": 9.60 if moving else 9.79,
                        "magnitude_max": 12.30 if moving else 9.83,
                        "magnitude_stddev": 0.87 if moving else 0.01,
                    },
                    quality_flags=[QualityFlag.EXPERIMENTAL],
                )
            )
            movement_samples += 1
        cursor += timedelta(hours=3)

    # -- calls metadata ----------------------------------------------------
    call_times = [
        DAY_START.replace(hour=9, minute=15),
        DAY_START.replace(hour=18, minute=44),
    ]
    for index, when in enumerate(call_times):
        events.append(
            _event(
                "calls",
                "call_record",
                when,
                {
                    "direction": "incoming" if index == 0 else "missed",
                    "duration_seconds": 214 if index == 0 else 0,
                    "counterparty_pseudonym": f"p_synthetic00000{index}",
                },
                quality_flags=[QualityFlag.REDACTED],
            )
        )

    # -- sms metadata ------------------------------------------------------
    sms_times = [
        DAY_START.replace(hour=10, minute=5),
        DAY_START.replace(hour=10, minute=6),
    ]
    for index, when in enumerate(sms_times):
        events.append(
            _event(
                "sms",
                "sms_record",
                when,
                {
                    "direction": "incoming" if index == 0 else "outgoing",
                    "read": True,
                    "body_length": 44,
                    "counterparty_pseudonym": "p_synthetic000000",
                    "thread_pseudonym": "p_synthetic0000th",
                },
                quality_flags=[QualityFlag.REDACTED],
            )
        )

    # -- hourly heartbeats (absent during the gap) -------------------------
    heartbeats = 0
    cursor = DAY_START
    while cursor < DAY_END:
        if not _in_gap(cursor):
            events.append(
                _event(
                    "heartbeat",
                    "collector_heartbeat",
                    cursor,
                    {
                        "collector_version": "0.1.0",
                        "protocol_version": 1,
                        "termux_detected": False,
                        "queue": {"pending_events": 0, "total_events": 0},
                        "enabled_collectors": [
                            "battery",
                            "calls",
                            "location",
                            "sensors",
                            "sms",
                            "wifi",
                        ],
                        "location_mode": "coarse",
                        "salt_configured": True,
                    },
                    quality_flags=[QualityFlag.MOCKED],
                )
            )
            heartbeats += 1
        cursor += timedelta(hours=1)

    # -- a late-arriving event: happened at 05:00, collected at 20:00 -------
    late_event = _event(
        "tasker",
        "screen_on",
        DAY_START.replace(hour=5, minute=0),
        {"trigger": "synthetic-late-arrival"},
        collected=DAY_START.replace(hour=20, minute=0),
        quality_flags=[QualityFlag.LATE_ARRIVAL],
    )
    events.append(late_event)

    # -- an exact duplicate, repeated in the stream ------------------------
    duplicate_source = events[0]
    events.append(json.loads(json.dumps(duplicate_source)))

    # Sort by event time so uploads arrive in a realistic order, but keep the
    # duplicate adjacent to its original.
    events.sort(key=lambda e: (e["event_time_utc"], e["source"], e["event_id"]))

    unique_ids = {e["event_id"] for e in events}

    return SyntheticDay(
        {
            "device_id": DEVICE_ID,
            "day_start_utc": iso_utc(DAY_START),
            "day_end_utc": iso_utc(DAY_END),
            "timezone_offset_minutes": TIMEZONE_OFFSET_MINUTES,
            "events": events,
            "expected": {
                "total_event_rows": len(events),
                "unique_event_ids": len(unique_ids),
                "duplicate_rows": len(events) - len(unique_ids),
                "battery_samples": battery_samples,
                "charging_samples": charging_samples,
                "wifi_samples_connected": wifi_connected_samples,
                "location_samples": location_samples,
                "movement_samples": movement_samples,
                "calls": len(call_times),
                "sms": len(sms_times),
                "heartbeats": heartbeats,
                "gap_start_utc": iso_utc(GAP_START),
                "gap_end_utc": iso_utc(GAP_END),
                "gap_hours": [2, 3],
                "charging_window_utc": [iso_utc(CHARGE_START), iso_utc(CHARGE_END)],
                "wifi_off_window_utc": [
                    iso_utc(WIFI_OFF_START),
                    iso_utc(WIFI_OFF_END),
                ],
                "late_arrival_event_id": late_event["event_id"],
                "replay_event_id": duplicate_source["event_id"],
            },
        }
    )


#: Event id that the cross-repository test resends inside a *second* batch to
#: prove that idempotency is keyed on the event, not on the batch.
REPLAY_EVENT_ID = build_synthetic_day()["expected"]["replay_event_id"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="emit the synthetic day as JSON")
    parser.add_argument("--output", help="write to this path instead of stdout")
    parser.add_argument(
        "--events-only",
        action="store_true",
        help="emit just the events array",
    )
    args = parser.parse_args(argv)

    day = build_synthetic_day()
    payload: Any = day["events"] if args.events_only else dict(day)
    text = json.dumps(payload, indent=2, sort_keys=True)

    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {len(day['events'])} events to {args.output}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
