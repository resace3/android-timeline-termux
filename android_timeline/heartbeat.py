"""Heartbeat events.

A heartbeat is how the server distinguishes "nothing happened" from "the
collector was dead". Without it, a flat battery curve and a crashed daemon
look identical downstream -- which is exactly the failure mode this project
exists to detect.
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Any

from . import COLLECTOR_VERSION, PROTOCOL_VERSION
from .collectors import Collector
from .config import Config
from .database import Outbox
from .models import Event

__all__ = ["HEARTBEAT_EVENT_TYPE", "HEARTBEAT_SOURCE", "build_heartbeat", "is_termux"]

HEARTBEAT_SOURCE = "heartbeat"
HEARTBEAT_EVENT_TYPE = "collector_heartbeat"


def is_termux() -> bool:
    """Detect a real Termux environment.

    Checks the Termux prefix rather than merely ``$PREFIX`` so that setting
    an environment variable is not enough to fool ``doctor``.
    """
    prefix = os.environ.get("PREFIX", "")
    if "com.termux" in prefix and Path(prefix).is_dir():
        return True
    return Path("/data/data/com.termux/files/usr").is_dir()


def build_heartbeat(config: Config, outbox: Outbox, collectors: list[Collector]) -> Event:
    """Assemble a heartbeat event describing collector and queue health.

    Contains no credentials, no hostnames and no payload data -- only counts,
    versions and capability flags.
    """
    stats = outbox.stats()
    statuses = {row["source"]: row for row in outbox.collector_status()}

    collector_state: dict[str, Any] = {}
    for collector in collectors:
        row = statuses.get(collector.source, {})
        collector_state[collector.source] = {
            "enabled": collector.enabled,
            "availability": collector.probe().value,
            "interval_seconds": collector.interval_seconds,
            "missing_commands": collector.missing_commands(),
            "error_count": int(row.get("error_count", 0) or 0),
            "success_count": int(row.get("success_count", 0) or 0),
            "last_success_at_utc": row.get("last_success_at_utc"),
        }

    payload: dict[str, Any] = {
        "collector_version": COLLECTOR_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "python_version": platform.python_version(),
        "platform": sys.platform,
        "termux_detected": is_termux(),
        "queue": {
            "pending_events": stats.pending_events,
            "total_events": stats.total_events,
            "dead_letter_events": stats.dead_letter_events,
            "pending_batches": stats.pending_batches,
            "oldest_pending_utc": stats.oldest_pending_utc,
        },
        "last_successful_upload_utc": stats.last_successful_upload_utc,
        "enabled_collectors": config.enabled_collectors(),
        "collectors": collector_state,
        "location_mode": config.privacy.location_mode,
        "salt_configured": config.privacy.has_salt(),
    }

    return Event.create(
        device_id=config.device.device_id,
        source=HEARTBEAT_SOURCE,
        event_type=HEARTBEAT_EVENT_TYPE,
        payload=payload,
    )
