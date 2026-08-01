"""Current Wi-Fi connection metadata via ``termux-wifi-connectioninfo``.

SSID and BSSID are location-revealing identifiers, so both are pseudonymised
with the local salt before they ever reach the outbox. What survives is
enough to say "same network as yesterday", not which network it was.
"""

from __future__ import annotations

from typing import Any

from ..redaction import pseudonymize
from .base import Collector, CollectorError, Observation

__all__ = ["WifiCollector"]

_DISCONNECTED = {"", "<unknown ssid>", "0x", "null", "none", "unknown ssid"}


class WifiCollector(Collector):
    source = "wifi"
    required_commands = ("termux-wifi-connectioninfo",)

    def observe(self) -> list[Observation]:
        result = self.runner.run("termux-wifi-connectioninfo")
        if not result.ok:
            raise CollectorError(
                f"termux-wifi-connectioninfo exited {result.returncode}: "
                f"{result.stderr.strip()[:200]}"
            )
        data = result.json()
        if not isinstance(data, dict):
            raise CollectorError("expected a JSON object from termux-wifi-connectioninfo")

        salt = self.config.privacy.salt() if self.config.privacy.has_salt() else None
        raw_ssid = str(data.get("ssid") or "").strip().strip('"')
        connected = raw_ssid.lower() not in _DISCONNECTED

        payload: dict[str, Any] = {
            "connected": connected,
            "ssid_pseudonym": pseudonymize(raw_ssid, salt) if connected else None,
            "bssid_pseudonym": pseudonymize(str(data.get("bssid") or "") or None, salt)
            if connected
            else None,
            "supplicant_state": str(data.get("supplicant_state") or "").upper() or None,
        }

        for source_key, target_key, caster in (
            ("frequency_mhz", "frequency_mhz", _as_int),
            ("link_speed_mbps", "link_speed_mbps", _as_int),
            ("rssi", "rssi_dbm", _as_int),
        ):
            value = caster(data.get(source_key))
            if value is not None:
                payload[target_key] = value

        # Never emitted: ip, mac_address, network_id -- device-identifying and
        # not useful for behavioural features.
        return [Observation(event_type="wifi_sample", payload=payload)]


def _as_int(value: Any) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
