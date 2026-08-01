"""Collector behaviour against mocked Termux commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from android_timeline.collectors import (
    COLLECTOR_CLASSES,
    Availability,
    BatteryCollector,
    CallsCollector,
    LocationCollector,
    MockCommandRunner,
    SensorsCollector,
    SmsCollector,
    WifiCollector,
    build_collectors,
)
from android_timeline.config import DEFAULT_COLLECTORS, Config
from android_timeline.models import QualityFlag


class TestRegistry:
    def test_registry_matches_config_defaults(self) -> None:
        assert set(COLLECTOR_CLASSES) == set(DEFAULT_COLLECTORS)

    def test_every_collector_declares_a_source(self) -> None:
        for name, cls in COLLECTOR_CLASSES.items():
            assert cls.source == name

    def test_build_collectors_filters_to_enabled(self, config: Config) -> None:
        config.collectors["sms"].enabled = False
        names = {c.source for c in build_collectors(config)}
        assert "sms" not in names

    def test_build_collectors_can_include_disabled(self, config: Config) -> None:
        config.collectors["sms"].enabled = False
        names = {c.source for c in build_collectors(config, only_enabled=False)}
        assert "sms" in names


class TestBattery:
    def test_parses_a_sample(
        self, config: Config, mock_runner: MockCommandRunner
    ) -> None:
        events = BatteryCollector(config, mock_runner).collect()
        assert len(events) == 1
        payload = events[0].payload
        assert payload["percentage"] == 72
        assert payload["charging"] is False
        assert payload["status"] == "DISCHARGING"

    def test_mocked_data_is_flagged(
        self, config: Config, mock_runner: MockCommandRunner
    ) -> None:
        events = BatteryCollector(config, mock_runner).collect()
        assert QualityFlag.MOCKED in events[0].quality_flags

    def test_charging_is_derived_from_plugged_state(
        self, config: Config, tmp_path: Path
    ) -> None:
        (tmp_path / "termux-battery-status.json").write_text(
            json.dumps({"percentage": 80, "status": "UNKNOWN", "plugged": "AC"}),
            encoding="utf-8",
        )
        events = BatteryCollector(config, MockCommandRunner(tmp_path)).collect()
        assert events[0].payload["charging"] is True

    def test_missing_command_yields_a_quality_event(
        self, config: Config, tmp_path: Path
    ) -> None:
        (tmp_path / "termux-battery-status.missing").touch()
        events = BatteryCollector(config, MockCommandRunner(tmp_path)).collect()
        assert events[0].event_type == "collection_unavailable"
        assert QualityFlag.COMMAND_UNAVAILABLE in events[0].quality_flags

    def test_malformed_output_yields_a_collection_error(
        self, config: Config, tmp_path: Path
    ) -> None:
        (tmp_path / "termux-battery-status.json").write_text("not json", encoding="utf-8")
        events = BatteryCollector(config, MockCommandRunner(tmp_path)).collect()
        assert events[0].event_type == "collection_error"
        assert QualityFlag.COLLECTOR_ERROR in events[0].quality_flags

    def test_non_zero_exit_yields_a_collection_error(
        self, config: Config, tmp_path: Path
    ) -> None:
        (tmp_path / "termux-battery-status.json").write_text("{}", encoding="utf-8")
        (tmp_path / "termux-battery-status.meta.json").write_text(
            json.dumps({"returncode": 1, "stderr": "permission denied"}), encoding="utf-8"
        )
        events = BatteryCollector(config, MockCommandRunner(tmp_path)).collect()
        assert events[0].event_type == "collection_error"
        assert "permission denied" in events[0].payload["error"]

    def test_collector_failure_never_raises(self, config: Config, tmp_path: Path) -> None:
        (tmp_path / "termux-battery-status.json").write_text("", encoding="utf-8")
        # Must not raise -- a broken source can never stop the daemon.
        assert BatteryCollector(config, MockCommandRunner(tmp_path)).collect()


class TestWifi:
    def test_ssid_is_pseudonymised(
        self, config: Config, mock_runner: MockCommandRunner
    ) -> None:
        payload = WifiCollector(config, mock_runner).collect()[0].payload
        assert payload["connected"] is True
        assert payload["ssid_pseudonym"].startswith("p_")
        assert "example-network" not in json.dumps(payload)

    def test_identifying_fields_are_never_emitted(
        self, config: Config, mock_runner: MockCommandRunner
    ) -> None:
        payload = WifiCollector(config, mock_runner).collect()[0].payload
        serialised = json.dumps(payload)
        assert "192.0.2.10" not in serialised
        assert "02:00:00:00:00:01" not in serialised
        assert "ip" not in payload
        assert "mac_address" not in payload

    def test_disconnected_state_is_recognised(
        self, config: Config, tmp_path: Path
    ) -> None:
        (tmp_path / "termux-wifi-connectioninfo.json").write_text(
            json.dumps({"ssid": "<unknown ssid>", "supplicant_state": "DISCONNECTED"}),
            encoding="utf-8",
        )
        payload = WifiCollector(config, MockCommandRunner(tmp_path)).collect()[0].payload
        assert payload["connected"] is False
        assert payload["ssid_pseudonym"] is None


class TestLocation:
    def test_disabled_mode_emits_no_coordinates(
        self, config: Config, mock_runner: MockCommandRunner
    ) -> None:
        config.privacy.location_mode = "disabled"
        events = LocationCollector(config, mock_runner).collect()
        assert events[0].event_type == "location_disabled"
        assert "latitude" not in events[0].payload

    def test_coarse_mode_reduces_precision(
        self, config: Config, mock_runner: MockCommandRunner
    ) -> None:
        config.privacy.location_mode = "coarse"
        payload = LocationCollector(config, mock_runner).collect()[0].payload
        assert payload["latitude"] == 10.12
        assert payload["longitude"] == 20.57

    def test_geohash_mode_emits_no_raw_coordinates(
        self, config: Config, mock_runner: MockCommandRunner
    ) -> None:
        config.privacy.location_mode = "geohash"
        payload = LocationCollector(config, mock_runner).collect()[0].payload
        assert "geohash" in payload
        assert "latitude" not in payload

    def test_raw_mode_is_flagged_but_full_precision(
        self, config: Config, mock_runner: MockCommandRunner
    ) -> None:
        config.privacy.location_mode = "raw"
        event = LocationCollector(config, mock_runner).collect()[0]
        assert event.payload["latitude"] == 10.123456
        assert QualityFlag.REDACTED not in event.quality_flags

    def test_unknown_provider_is_a_collection_error(
        self, config: Config, mock_runner: MockCommandRunner
    ) -> None:
        config.privacy.location_mode = "coarse"
        config.collectors["location"].options["provider"] = "telepathy"
        events = LocationCollector(config, mock_runner).collect()
        assert events[0].event_type == "collection_error"


class TestSensors:
    def test_summary_is_aggregate_only(
        self, config: Config, mock_runner: MockCommandRunner
    ) -> None:
        events = SensorsCollector(config, mock_runner).collect()
        types = {e.event_type for e in events}
        assert "sensor_catalogue" in types
        summaries = [e for e in events if e.event_type == "sensor_summary"]
        assert summaries
        payload = summaries[0].payload
        assert "magnitude_mean" in payload
        # Never a raw sample stream.
        assert "values" not in payload

    def test_marked_experimental(
        self, config: Config, mock_runner: MockCommandRunner
    ) -> None:
        events = SensorsCollector(config, mock_runner).collect()
        assert QualityFlag.EXPERIMENTAL in events[0].quality_flags


class TestSensitiveCollectors:
    def test_calls_require_a_salt(
        self,
        config: Config,
        mock_runner: MockCommandRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("ANDROID_TIMELINE_SALT", raising=False)
        config.privacy.salt_file = "/nonexistent/salt"
        events = CallsCollector(config, mock_runner).collect()
        assert events[0].event_type == "collection_error"
        assert "salt" in events[0].payload["error"]

    def test_call_numbers_are_pseudonymised(
        self, config: Config, mock_runner: MockCommandRunner
    ) -> None:
        events = CallsCollector(config, mock_runner).collect()
        serialised = json.dumps([e.payload for e in events])
        for number in ("+1-555-0100", "+1-555-0101", "+1-555-0102", "15550100"):
            assert number not in serialised
        assert all(e.payload["counterparty_pseudonym"].startswith("p_") for e in events)

    def test_contact_names_are_dropped_by_default(
        self, config: Config, mock_runner: MockCommandRunner
    ) -> None:
        events = CallsCollector(config, mock_runner).collect()
        serialised = json.dumps([e.payload for e in events])
        assert "Synthetic Contact" not in serialised
        assert all("contact_name" not in e.payload for e in events)

    def test_contact_names_appear_only_when_opted_in(
        self, config: Config, mock_runner: MockCommandRunner
    ) -> None:
        config.privacy.store_contact_names = True
        events = CallsCollector(config, mock_runner).collect()
        assert any(e.payload.get("contact_name") for e in events)

    def test_sms_bodies_are_dropped_by_default(
        self, config: Config, mock_runner: MockCommandRunner
    ) -> None:
        events = SmsCollector(config, mock_runner).collect()
        serialised = json.dumps([e.payload for e in events])
        assert "SYNTHETIC TEST MESSAGE" not in serialised
        assert all("body" not in e.payload for e in events)
        assert events[0].payload["body_length"] > 0

    def test_sms_bodies_appear_only_when_opted_in(
        self, config: Config, mock_runner: MockCommandRunner
    ) -> None:
        config.privacy.store_message_bodies = True
        events = SmsCollector(config, mock_runner).collect()
        assert "SYNTHETIC TEST MESSAGE" in json.dumps([e.payload for e in events])

    def test_sms_entries_do_not_collapse_into_one_event(
        self, config: Config, mock_runner: MockCommandRunner
    ) -> None:
        events = SmsCollector(config, mock_runner).collect()
        assert len({e.event_id for e in events}) == len(events) == 2


class TestAvailability:
    def test_mock_runner_reports_mocked(
        self, config: Config, mock_runner: MockCommandRunner
    ) -> None:
        assert BatteryCollector(config, mock_runner).probe() is Availability.MOCKED

    def test_missing_command_reports_unavailable(
        self, config: Config, tmp_path: Path
    ) -> None:
        collector = BatteryCollector(config, MockCommandRunner(tmp_path))
        assert collector.probe() is Availability.UNAVAILABLE
        assert collector.missing_commands() == ["termux-battery-status"]

    def test_argument_specific_fixtures_win(self, tmp_path: Path) -> None:
        (tmp_path / "cmd.json").write_text('{"a": 1}', encoding="utf-8")
        (tmp_path / "cmd__-l.json").write_text('{"a": 2}', encoding="utf-8")
        runner = MockCommandRunner(tmp_path)
        assert runner.run("cmd").json() == {"a": 1}
        assert runner.run("cmd", ["-l"]).json() == {"a": 2}
