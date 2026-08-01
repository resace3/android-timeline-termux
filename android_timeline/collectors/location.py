"""Location sampling via ``termux-location``.

Disabled by default. When enabled, the configured ``privacy.location_mode``
decides how much precision survives; ``raw`` coordinates require an explicit
opt-in and are never the default.
"""

from __future__ import annotations

from typing import Any

from ..models import QualityFlag
from ..redaction import transform_location
from .base import Collector, CollectorError, Observation

__all__ = ["LocationCollector"]

_PROVIDERS = ("gps", "network", "passive")


class LocationCollector(Collector):
    source = "location"
    required_commands = ("termux-location",)

    def observe(self) -> list[Observation]:
        mode = self.config.privacy.location_mode
        if mode == "disabled":
            return [
                Observation(
                    event_type="location_disabled",
                    payload={"location_mode": "disabled"},
                )
            ]

        options = self.settings.options if self.settings else {}
        provider = str(options.get("provider", "network")).lower()
        if provider not in _PROVIDERS:
            raise CollectorError(
                f"unknown location provider {provider!r}; expected one of "
                f"{', '.join(_PROVIDERS)}"
            )
        request = str(options.get("request", "last")).lower()

        result = self.runner.run(
            "termux-location",
            ["-p", provider, "-r", request],
            timeout=float(options.get("timeout_seconds", 45.0)),
        )
        if not result.ok:
            raise CollectorError(
                f"termux-location exited {result.returncode}: "
                f"{result.stderr.strip()[:200]}"
            )

        data = result.json()
        if not isinstance(data, dict):
            raise CollectorError("expected a JSON object from termux-location")

        latitude = _as_float(data.get("latitude"))
        longitude = _as_float(data.get("longitude"))

        extra: dict[str, Any] = {"provider": str(data.get("provider", provider))}
        accuracy = _as_float(data.get("accuracy"))
        if accuracy is not None:
            extra["accuracy_metres"] = round(accuracy, 1)
        speed = _as_float(data.get("speed"))
        if speed is not None:
            extra["speed_mps"] = round(speed, 2)
        altitude = _as_float(data.get("altitude"))
        if altitude is not None and mode == "raw":
            extra["altitude_metres"] = round(altitude, 1)

        payload = transform_location(
            latitude,
            longitude,
            mode=mode,
            geohash_precision=self.config.privacy.geohash_precision,
            coarse_decimal_places=self.config.privacy.coarse_decimal_places,
            extra=extra,
        )

        flags: list[str] = []
        if mode in ("coarse", "geohash"):
            flags.append(QualityFlag.COARSE)
        if mode != "raw":
            flags.append(QualityFlag.REDACTED)
        if latitude is None or longitude is None:
            flags.append(QualityFlag.PARTIAL)

        return [
            Observation(
                event_type="location_sample", payload=payload, quality_flags=flags
            )
        ]


def _as_float(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
