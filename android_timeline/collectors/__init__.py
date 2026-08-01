"""Collector registry.

Adding a collector means adding it here *and* to
:data:`android_timeline.config.DEFAULT_COLLECTORS`; the tests assert the two
stay in sync so a collector can never be shipped without a documented
default enablement state.
"""

from __future__ import annotations

from typing import Final

from ..config import Config
from .base import (
    Availability,
    Collector,
    CollectorError,
    CommandResult,
    CommandRunner,
    MockCommandRunner,
    Observation,
    TermuxCommandRunner,
    get_command_runner,
)
from .battery import BatteryCollector
from .calls import CallsCollector
from .location import LocationCollector
from .sensors import SensorsCollector
from .sms import SmsCollector
from .wifi import WifiCollector

__all__ = [
    "COLLECTOR_CLASSES",
    "Availability",
    "BatteryCollector",
    "CallsCollector",
    "Collector",
    "CollectorError",
    "CommandResult",
    "CommandRunner",
    "LocationCollector",
    "MockCommandRunner",
    "Observation",
    "SensorsCollector",
    "SmsCollector",
    "TermuxCommandRunner",
    "WifiCollector",
    "build_collectors",
    "get_command_runner",
]

COLLECTOR_CLASSES: Final[dict[str, type[Collector]]] = {
    BatteryCollector.source: BatteryCollector,
    WifiCollector.source: WifiCollector,
    SensorsCollector.source: SensorsCollector,
    LocationCollector.source: LocationCollector,
    CallsCollector.source: CallsCollector,
    SmsCollector.source: SmsCollector,
}


def build_collectors(
    config: Config,
    runner: CommandRunner | None = None,
    *,
    only_enabled: bool = True,
) -> list[Collector]:
    """Instantiate collectors for ``config``, sorted by source name."""
    shared = runner or get_command_runner()
    collectors = [
        cls(config, shared)
        for name, cls in sorted(COLLECTOR_CLASSES.items())
        if name in config.collectors
    ]
    if only_enabled:
        return [c for c in collectors if c.enabled]
    return collectors
