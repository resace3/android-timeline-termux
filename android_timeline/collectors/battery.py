"""Battery level and charging state via ``termux-battery-status``."""

from __future__ import annotations

from typing import Any

from .base import Collector, CollectorError, Observation

__all__ = ["BatteryCollector"]

_PLUGGED_VALUES = {"AC", "USB", "WIRELESS", "PLUGGED_AC", "PLUGGED_USB"}


class BatteryCollector(Collector):
    """Emits a ``battery_sample`` per poll.

    Charging state is derived here rather than server-side so that a gap in
    uploads never turns into a gap in charging attribution.
    """

    source = "battery"
    required_commands = ("termux-battery-status",)

    def observe(self) -> list[Observation]:
        result = self.runner.run("termux-battery-status")
        if not result.ok:
            raise CollectorError(
                f"termux-battery-status exited {result.returncode}: "
                f"{result.stderr.strip()[:200]}"
            )
        data = result.json()
        if not isinstance(data, dict):
            raise CollectorError("expected a JSON object from termux-battery-status")

        percentage = _as_int(data.get("percentage"))
        status = str(data.get("status", "UNKNOWN")).upper()
        plugged = str(data.get("plugged", "")).upper()

        payload: dict[str, Any] = {
            "percentage": percentage,
            "status": status,
            "plugged": plugged or None,
            "charging": status == "CHARGING" or plugged in _PLUGGED_VALUES,
            "health": str(data.get("health", "UNKNOWN")).upper(),
        }

        temperature = _as_float(data.get("temperature"))
        if temperature is not None:
            payload["temperature_celsius"] = round(temperature, 2)
        current = _as_int(data.get("current"))
        if current is not None:
            payload["current_microamps"] = current

        return [Observation(event_type="battery_sample", payload=payload)]


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
