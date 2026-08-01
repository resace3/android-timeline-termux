"""Redaction, pseudonymisation and location generalisation.

Nothing in this module is reversible without the local salt, and the salt
never leaves the phone. Applied at *collection* time so sensitive values are
never written to the outbox in the first place -- not merely hidden at upload.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable, Mapping
from typing import Any, Final

__all__ = [
    "PSEUDONYM_PREFIX",
    "coarsen_coordinate",
    "geohash_encode",
    "pseudonymize",
    "redact_headers",
    "redact_token",
    "transform_location",
]

PSEUDONYM_PREFIX: Final = "p_"

_SENSITIVE_HEADERS: Final = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
)

_GEOHASH_ALPHABET: Final = "0123456789bcdefghjkmnpqrstuvwxyz"

_DIGITS_RE: Final = re.compile(r"[^0-9+]")


def redact_token(token: str | None) -> str:
    """Render a bearer token safe for logs and diagnostics.

    Shows only a short salt-free digest prefix, never any part of the token
    itself, so ``doctor`` output can be pasted into a bug report.
    """
    if not token:
        return "<absent>"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
    return f"<redacted len={len(token)} sha256:{digest}>"


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Copy ``headers`` with credential-bearing values replaced."""
    return {
        key: ("<redacted>" if key.lower() in _SENSITIVE_HEADERS else value)
        for key, value in headers.items()
    }


def pseudonymize(value: str | None, salt: str | None, *, length: int = 16) -> str | None:
    """Map an identifier to a stable, unlinkable pseudonym.

    ``None`` in, ``None`` out. Uses HMAC-SHA256 so that knowing the output
    and the input space (e.g. all possible phone numbers) still does not
    recover the mapping without the salt.
    """
    if value is None:
        return None
    normalised = _DIGITS_RE.sub("", str(value)) or str(value).strip().lower()
    if not normalised:
        return None
    key = (salt or "").encode("utf-8")
    digest = hmac.new(key, normalised.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{PSEUDONYM_PREFIX}{digest[:length]}"


def coarsen_coordinate(value: float, decimal_places: int) -> float:
    """Round a coordinate down to a fixed precision.

    Two decimal places is roughly a 1 km grid cell at the equator -- enough to
    tell "at home" from "in town", not enough to identify an address.
    """
    return round(float(value), decimal_places)


def geohash_encode(latitude: float, longitude: float, precision: int = 6) -> str:
    """Encode a coordinate as a geohash (pure Python, no dependencies)."""
    if not -90.0 <= latitude <= 90.0:
        raise ValueError("latitude must be between -90 and 90")
    if not -180.0 <= longitude <= 180.0:
        raise ValueError("longitude must be between -180 and 180")
    if not 1 <= precision <= 12:
        raise ValueError("precision must be between 1 and 12")

    lat_range = [-90.0, 90.0]
    lon_range = [-180.0, 180.0]
    result: list[str] = []
    bits = 0
    bit_count = 0
    even = True

    while len(result) < precision:
        if even:
            mid = (lon_range[0] + lon_range[1]) / 2
            if longitude > mid:
                bits = (bits << 1) | 1
                lon_range[0] = mid
            else:
                bits <<= 1
                lon_range[1] = mid
        else:
            mid = (lat_range[0] + lat_range[1]) / 2
            if latitude > mid:
                bits = (bits << 1) | 1
                lat_range[0] = mid
            else:
                bits <<= 1
                lat_range[1] = mid
        even = not even
        bit_count += 1
        if bit_count == 5:
            result.append(_GEOHASH_ALPHABET[bits])
            bits = 0
            bit_count = 0

    return "".join(result)


def transform_location(
    latitude: float | None,
    longitude: float | None,
    *,
    mode: str,
    geohash_precision: int = 6,
    coarse_decimal_places: int = 2,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Reduce a fix to the least precise representation the config allows.

    ``raw`` is the only mode that emits full coordinates, and it is never the
    default. Every other mode drops the original numbers before they reach
    the outbox.
    """
    payload: dict[str, Any] = {"location_mode": mode}
    if extra:
        payload.update(
            {
                k: v
                for k, v in extra.items()
                if k not in ("latitude", "longitude", "location_mode")
            }
        )

    if mode == "disabled" or latitude is None or longitude is None:
        payload["location_available"] = False
        return payload

    payload["location_available"] = True

    if mode == "coarse":
        payload["latitude"] = coarsen_coordinate(latitude, coarse_decimal_places)
        payload["longitude"] = coarsen_coordinate(longitude, coarse_decimal_places)
        payload["precision_decimal_places"] = coarse_decimal_places
    elif mode == "geohash":
        payload["geohash"] = geohash_encode(latitude, longitude, geohash_precision)
        payload["geohash_precision"] = geohash_precision
    elif mode == "precise":
        # "precise" still generalises: 4 dp is ~11 m, enough for dwell
        # detection without recording a doorstep.
        payload["latitude"] = coarsen_coordinate(latitude, 4)
        payload["longitude"] = coarsen_coordinate(longitude, 4)
        payload["precision_decimal_places"] = 4
    elif mode == "raw":
        payload["latitude"] = float(latitude)
        payload["longitude"] = float(longitude)
        payload["precision_decimal_places"] = None
    else:  # pragma: no cover - guarded by config validation
        raise ValueError(f"unknown location mode: {mode}")

    return payload


def strip_keys(payload: Mapping[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    """Return ``payload`` without ``keys`` (used to drop opt-in fields)."""
    removed = set(keys)
    return {k: v for k, v in payload.items() if k not in removed}
