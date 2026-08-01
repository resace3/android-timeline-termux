"""Redaction, pseudonymisation and location generalisation."""

from __future__ import annotations

import pytest

from android_timeline.redaction import (
    PSEUDONYM_PREFIX,
    coarsen_coordinate,
    geohash_encode,
    pseudonymize,
    redact_headers,
    redact_token,
    transform_location,
)

TOKEN = "super-secret-device-token-abcdef123456"


class TestTokenRedaction:
    def test_token_value_never_appears(self) -> None:
        rendered = redact_token(TOKEN)
        assert TOKEN not in rendered
        assert "abcdef" not in rendered

    def test_absent_token_is_labelled(self) -> None:
        assert redact_token(None) == "<absent>"
        assert redact_token("") == "<absent>"

    def test_fingerprint_is_stable(self) -> None:
        assert redact_token(TOKEN) == redact_token(TOKEN)

    def test_different_tokens_differ(self) -> None:
        assert redact_token(TOKEN) != redact_token(TOKEN + "x")


class TestHeaderRedaction:
    def test_authorization_is_replaced(self) -> None:
        headers = {"Authorization": f"Bearer {TOKEN}", "X-Device-ID": "device-test-001"}
        redacted = redact_headers(headers)
        assert redacted["Authorization"] == "<redacted>"
        assert redacted["X-Device-ID"] == "device-test-001"

    @pytest.mark.parametrize(
        "name", ["authorization", "AUTHORIZATION", "Cookie", "X-Api-Key"]
    )
    def test_sensitive_headers_are_case_insensitive(self, name: str) -> None:
        assert redact_headers({name: "value"})[name] == "<redacted>"

    def test_original_mapping_is_not_mutated(self) -> None:
        headers = {"Authorization": "Bearer x"}
        redact_headers(headers)
        assert headers["Authorization"] == "Bearer x"


class TestPseudonymize:
    def test_output_is_prefixed_and_short(self) -> None:
        value = pseudonymize("+1-555-0100", "salt")
        assert value is not None
        assert value.startswith(PSEUDONYM_PREFIX)
        assert len(value) == len(PSEUDONYM_PREFIX) + 16

    def test_original_value_is_not_recoverable_from_the_output(self) -> None:
        value = pseudonymize("+1-555-0100", "salt")
        assert value is not None
        assert "555" not in value
        assert "0100" not in value

    def test_stable_for_the_same_salt(self) -> None:
        assert pseudonymize("+1-555-0100", "salt") == pseudonymize("+1-555-0100", "salt")

    def test_different_salts_are_unlinkable(self) -> None:
        assert pseudonymize("+1-555-0100", "a") != pseudonymize("+1-555-0100", "b")

    def test_formatting_differences_normalise_to_the_same_pseudonym(self) -> None:
        assert pseudonymize("+1-555-0100", "s") == pseudonymize("+1 (555) 0100", "s")

    def test_none_passes_through(self) -> None:
        assert pseudonymize(None, "salt") is None

    def test_empty_string_yields_none(self) -> None:
        assert pseudonymize("   ", "salt") is None


class TestGeohash:
    def test_known_vector(self) -> None:
        # Reference value for the geohash algorithm.
        assert geohash_encode(57.64911, 10.40744, 11) == "u4pruydqqvj"

    def test_precision_is_a_prefix_relationship(self) -> None:
        long = geohash_encode(10.123456, 20.567891, 9)
        short = geohash_encode(10.123456, 20.567891, 5)
        assert long.startswith(short)

    def test_lower_precision_generalises_nearby_points(self) -> None:
        a = geohash_encode(10.1234, 20.5678, 4)
        b = geohash_encode(10.1240, 20.5680, 4)
        assert a == b

    @pytest.mark.parametrize(
        ("lat", "lon", "precision"),
        [(91.0, 0.0, 6), (0.0, 181.0, 6), (0.0, 0.0, 0), (0.0, 0.0, 13)],
    )
    def test_invalid_inputs_raise(self, lat: float, lon: float, precision: int) -> None:
        with pytest.raises(ValueError, match="must be"):
            geohash_encode(lat, lon, precision)


class TestCoarsen:
    def test_two_decimal_places(self) -> None:
        assert coarsen_coordinate(10.123456, 2) == 10.12

    def test_zero_decimal_places(self) -> None:
        assert coarsen_coordinate(10.6, 0) == 11.0


class TestTransformLocation:
    LAT, LON = 10.123456, 20.567891

    def test_disabled_emits_no_coordinates(self) -> None:
        payload = transform_location(self.LAT, self.LON, mode="disabled")
        assert payload["location_available"] is False
        assert "latitude" not in payload
        assert "longitude" not in payload

    def test_coarse_drops_precision(self) -> None:
        payload = transform_location(
            self.LAT, self.LON, mode="coarse", coarse_decimal_places=2
        )
        assert payload["latitude"] == 10.12
        assert payload["longitude"] == 20.57

    def test_geohash_emits_no_raw_coordinates(self) -> None:
        payload = transform_location(self.LAT, self.LON, mode="geohash")
        assert "geohash" in payload
        assert "latitude" not in payload
        assert "longitude" not in payload

    def test_precise_still_generalises(self) -> None:
        payload = transform_location(self.LAT, self.LON, mode="precise")
        assert payload["latitude"] == 10.1235
        assert payload["precision_decimal_places"] == 4

    def test_raw_is_the_only_mode_with_full_precision(self) -> None:
        payload = transform_location(self.LAT, self.LON, mode="raw")
        assert payload["latitude"] == self.LAT
        assert payload["longitude"] == self.LON

    def test_missing_fix_is_reported_not_faked(self) -> None:
        payload = transform_location(None, None, mode="coarse")
        assert payload["location_available"] is False

    def test_extra_fields_cannot_smuggle_coordinates(self) -> None:
        payload = transform_location(
            self.LAT,
            self.LON,
            mode="geohash",
            extra={"latitude": self.LAT, "longitude": self.LON, "provider": "gps"},
        )
        assert "latitude" not in payload
        assert payload["provider"] == "gps"

    def test_unknown_mode_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown location mode"):
            transform_location(self.LAT, self.LON, mode="telepathy")
