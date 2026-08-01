"""Configuration parsing, validation and the insecure-endpoint gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from android_timeline.config import (
    INSECURE_TEST_MODE_ENV,
    INSECURE_TEST_MODE_PHRASE,
    ConfigError,
    load_config,
)

MINIMAL = """
[device]
device_id = "device-test-001"

[server]
base_url = "https://timeline.example.invalid:8099"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


class TestLoading:
    def test_minimal_config_loads(self, tmp_path: Path) -> None:
        config = load_config(write(tmp_path, MINIMAL))
        assert config.device.device_id == "device-test-001"
        assert config.server.verify_tls is True

    def test_missing_file_is_reported(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="configuration not found"):
            load_config(tmp_path / "absent.toml")

    def test_invalid_toml_is_reported(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not valid TOML"):
            load_config(write(tmp_path, "this is not = = toml"))

    def test_unknown_section_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="unknown config section"):
            load_config(write(tmp_path, MINIMAL + "\n[surprise]\nx = 1\n"))

    def test_unknown_key_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="unknown key"):
            load_config(write(tmp_path, MINIMAL + "\nsurprise = 1\n"))

    def test_wrong_type_is_rejected(self, tmp_path: Path) -> None:
        text = MINIMAL.replace(
            'base_url = "https://timeline.example.invalid:8099"', "base_url = 42"
        )
        with pytest.raises(ConfigError, match="must be str"):
            load_config(write(tmp_path, text))

    def test_unknown_collector_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="unknown collector"):
            load_config(
                write(tmp_path, MINIMAL + "\n[collectors.mystery]\nenabled = true\n")
            )


class TestDefaults:
    def test_sensitive_collectors_are_off_by_default(self, tmp_path: Path) -> None:
        config = load_config(write(tmp_path, MINIMAL))
        assert config.collectors["calls"].enabled is False
        assert config.collectors["sms"].enabled is False
        assert config.collectors["location"].enabled is False

    def test_battery_and_wifi_are_on_by_default(self, tmp_path: Path) -> None:
        config = load_config(write(tmp_path, MINIMAL))
        assert config.collectors["battery"].enabled is True
        assert config.collectors["wifi"].enabled is True

    def test_privacy_defaults_are_conservative(self, tmp_path: Path) -> None:
        config = load_config(write(tmp_path, MINIMAL))
        assert config.privacy.location_mode == "disabled"
        assert config.privacy.store_message_bodies is False
        assert config.privacy.store_contact_names is False

    def test_retention_is_disabled_by_default(self, tmp_path: Path) -> None:
        config = load_config(write(tmp_path, MINIMAL))
        assert config.retention.enabled is False


class TestValidation:
    def test_empty_base_url_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="base_url is required"):
            load_config(write(tmp_path, '[device]\ndevice_id = "d1"\n'))

    def test_non_http_scheme_is_rejected(self, tmp_path: Path) -> None:
        text = MINIMAL.replace(
            "https://timeline.example.invalid:8099", "ftp://example.invalid"
        )
        with pytest.raises(ConfigError, match="http\\(s\\) URL"):
            load_config(write(tmp_path, text))

    def test_bad_device_id_is_rejected(self, tmp_path: Path) -> None:
        text = MINIMAL.replace("device-test-001", "device/../evil")
        with pytest.raises(ConfigError, match="device_id may only contain"):
            load_config(write(tmp_path, text))

    def test_unknown_location_mode_is_rejected(self, tmp_path: Path) -> None:
        text = MINIMAL + '\n[privacy]\nlocation_mode = "everything"\n'
        with pytest.raises(ConfigError, match="location_mode must be one of"):
            load_config(write(tmp_path, text))

    def test_short_interval_is_rejected(self, tmp_path: Path) -> None:
        text = MINIMAL + "\n[collectors.battery]\ninterval_seconds = 1\n"
        with pytest.raises(ConfigError, match="interval_seconds must be >= 10"):
            load_config(write(tmp_path, text))

    def test_oversized_batch_limit_is_rejected(self, tmp_path: Path) -> None:
        text = MINIMAL + "\nmax_batch_events = 100000\n"
        with pytest.raises(ConfigError, match="max_batch_events"):
            load_config(write(tmp_path, text))

    def test_retention_needs_a_positive_window(self, tmp_path: Path) -> None:
        text = (
            MINIMAL
            + "\n[retention]\nenabled = true\ndelete_acknowledged_after_days = 0\n"
        )
        with pytest.raises(ConfigError, match="delete_acknowledged_after_days"):
            load_config(write(tmp_path, text))


class TestInsecureEndpointGate:
    """Plaintext HTTP needs three independent locks, not one."""

    HTTP = """
[device]
device_id = "device-test-001"

[server]
base_url = "http://127.0.0.1:8099"
allow_insecure_test_endpoint = true
"""

    def test_http_refused_without_the_environment_variable(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=INSECURE_TEST_MODE_ENV):
            load_config(write(tmp_path, self.HTTP))

    def test_http_refused_without_the_config_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(INSECURE_TEST_MODE_ENV, INSECURE_TEST_MODE_PHRASE)
        text = self.HTTP.replace("allow_insecure_test_endpoint = true", "")
        with pytest.raises(ConfigError, match="allow_insecure_test_endpoint"):
            load_config(write(tmp_path, text))

    def test_http_refused_for_a_non_loopback_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(INSECURE_TEST_MODE_ENV, INSECURE_TEST_MODE_PHRASE)
        text = self.HTTP.replace("127.0.0.1", "timeline.example.invalid")
        with pytest.raises(ConfigError, match="not a loopback or emulator address"):
            load_config(write(tmp_path, text))

    def test_http_allowed_when_all_three_locks_are_open(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(INSECURE_TEST_MODE_ENV, INSECURE_TEST_MODE_PHRASE)
        config = load_config(write(tmp_path, self.HTTP))
        assert config.server.base_url.startswith("http://127.0.0.1")

    def test_emulator_host_alias_is_allowed_in_test_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(INSECURE_TEST_MODE_ENV, INSECURE_TEST_MODE_PHRASE)
        text = self.HTTP.replace("127.0.0.1", "10.0.2.2")
        assert load_config(write(tmp_path, text)).server.base_url

    def test_disabling_tls_verification_needs_test_mode(self, tmp_path: Path) -> None:
        text = MINIMAL + "\nverify_tls = false\n"
        with pytest.raises(ConfigError, match="insecure endpoint"):
            load_config(write(tmp_path, text))


class TestSecrets:
    def test_token_comes_from_environment(self, tmp_path: Path) -> None:
        config = load_config(write(tmp_path, MINIMAL))
        assert config.server.token() == "synthetic-test-token-not-a-real-secret"

    def test_token_file_is_read_when_env_is_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANDROID_TIMELINE_TOKEN", raising=False)
        token_file = tmp_path / "token"
        token_file.write_text("file-token\n", encoding="utf-8")
        text = MINIMAL + f'\ntoken_file = "{token_file.as_posix()}"\n'
        assert load_config(write(tmp_path, text)).server.token() == "file-token"

    def test_missing_token_is_reported_clearly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANDROID_TIMELINE_TOKEN", raising=False)
        config = load_config(write(tmp_path, MINIMAL))
        assert config.server.has_token() is False
        with pytest.raises(ConfigError, match="device token not found"):
            config.server.token()

    def test_no_secret_appears_in_the_example_config(self) -> None:
        from android_timeline.cli import EXAMPLE_CONFIG

        lowered = EXAMPLE_CONFIG.lower()
        assert "token =" not in lowered
        assert "salt =" not in lowered
        assert "example.invalid" in lowered
