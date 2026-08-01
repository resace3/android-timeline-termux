"""Summaries of Termux sensor readings via ``termux-sensor``.

Experimental. Raw sensor streams are high-rate, battery-hungry and wildly
inconsistent across manufacturers, so this collector stores only aggregate
magnitudes -- never a raw sample stream.
"""

from __future__ import annotations

import math
from typing import Any

from .base import Collector, CollectorError, Observation

__all__ = ["SensorsCollector"]

#: Sensor name fragments worth summarising. Anything else is ignored so a
#: vendor-specific sensor cannot silently start recording something odd.
_INTERESTING = ("accelerometer", "gyroscope", "light", "step", "pressure")


class SensorsCollector(Collector):
    source = "sensors"
    required_commands = ("termux-sensor",)
    experimental = True

    def observe(self) -> list[Observation]:
        listing = self.runner.run("termux-sensor", ["-l"])
        if not listing.ok:
            raise CollectorError(
                f"termux-sensor -l exited {listing.returncode}: "
                f"{listing.stderr.strip()[:200]}"
            )
        catalogue = listing.json()
        sensors = _sensor_names(catalogue)
        if not sensors:
            return [
                Observation(
                    event_type="sensor_catalogue",
                    payload={"available_sensors": [], "sensor_count": 0},
                )
            ]

        options = self.settings.options if self.settings else {}
        selected = [
            name
            for name in sensors
            if any(fragment in name.lower() for fragment in _INTERESTING)
        ][: int(options.get("max_sensors", 4))]

        observations: list[Observation] = [
            Observation(
                event_type="sensor_catalogue",
                payload={
                    "available_sensors": sorted(sensors)[:64],
                    "sensor_count": len(sensors),
                    "summarised_sensors": selected,
                },
            )
        ]

        samples = int(options.get("samples", 5))
        for name in selected:
            reading = self.runner.run(
                "termux-sensor", ["-s", name, "-n", str(samples)], timeout=30.0
            )
            if not reading.ok:
                observations.append(
                    Observation(
                        event_type="sensor_summary",
                        payload={
                            "sensor": name,
                            "available": False,
                            "error": reading.stderr.strip()[:200] or "non-zero exit",
                        },
                    )
                )
                continue
            try:
                payload = _summarise(name, reading.json(), samples)
            except CollectorError as exc:
                payload = {"sensor": name, "available": False, "error": str(exc)[:200]}
            observations.append(Observation(event_type="sensor_summary", payload=payload))

        return observations

    # ``dedupe_key`` is unnecessary here: each event_type/sensor pair already
    # differs by payload, which feeds the deterministic event id.


def _sensor_names(catalogue: Any) -> list[str]:
    if isinstance(catalogue, dict):
        names = catalogue.get("sensors")
        if isinstance(names, list):
            return [str(n) for n in names]
        return [str(k) for k in catalogue]
    if isinstance(catalogue, list):
        return [str(n) for n in catalogue]
    raise CollectorError("unrecognised termux-sensor -l output")


def _summarise(name: str, reading: Any, requested: int) -> dict[str, Any]:
    """Reduce a burst of vector samples to magnitude statistics."""
    if not isinstance(reading, dict):
        raise CollectorError("expected a JSON object from termux-sensor -s")

    entry = reading.get(name)
    if entry is None and len(reading) == 1:
        entry = next(iter(reading.values()))
    if not isinstance(entry, dict):
        raise CollectorError(f"no readings for sensor {name}")

    values = entry.get("values")
    if not isinstance(values, list) or not values:
        raise CollectorError(f"sensor {name} returned no values")

    vectors: list[list[float]] = []
    if all(isinstance(v, (int, float)) for v in values):
        vectors = [[float(v) for v in values]]
    else:
        for row in values:
            if isinstance(row, list) and all(isinstance(v, (int, float)) for v in row):
                vectors.append([float(v) for v in row])
    if not vectors:
        raise CollectorError(f"sensor {name} returned no numeric values")

    magnitudes = [math.sqrt(sum(component**2 for component in v)) for v in vectors]
    mean = sum(magnitudes) / len(magnitudes)
    variance = sum((m - mean) ** 2 for m in magnitudes) / len(magnitudes)

    return {
        "sensor": name,
        "available": True,
        "samples_requested": requested,
        "samples_returned": len(magnitudes),
        "magnitude_mean": round(mean, 4),
        "magnitude_min": round(min(magnitudes), 4),
        "magnitude_max": round(max(magnitudes), 4),
        "magnitude_stddev": round(math.sqrt(variance), 4),
    }
