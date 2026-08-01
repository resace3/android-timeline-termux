"""Configuration loading and validation.

Config is TOML (parsed with stdlib :mod:`tomllib`). Secrets are *never* part
of the config file: the bearer token and the pseudonymisation salt live in
separate 0600 files, or in environment variables for CI.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

__all__ = [
    "CollectorConfig",
    "Config",
    "ConfigError",
    "DeviceConfig",
    "PrivacyConfig",
    "RetentionConfig",
    "ServerConfig",
    "UploadConfig",
    "default_config_path",
    "default_data_dir",
    "load_config",
]

#: Setting ``server.allow_insecure_test_endpoint`` is not enough on its own.
#: This environment variable must *also* carry the exact phrase below, and
#: the endpoint must be a loopback/emulator address. Three independent locks
#: so plaintext HTTP cannot be switched on by a stray config edit.
INSECURE_TEST_MODE_ENV: Final = "ANDROID_TIMELINE_INSECURE_TEST_MODE"
INSECURE_TEST_MODE_PHRASE: Final = "yes-i-am-running-an-automated-test"

#: Hosts allowed to serve plaintext HTTP in test mode. ``10.0.2.2`` is the
#: Android emulator's alias for the host machine's loopback interface.
_TEST_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1", "10.0.2.2"})

LOCATION_MODES: Final = ("disabled", "coarse", "geohash", "precise", "raw")

TOKEN_ENV: Final = "ANDROID_TIMELINE_TOKEN"  # noqa: S105 - variable name, not a value
SALT_ENV: Final = "ANDROID_TIMELINE_SALT"
CONFIG_ENV: Final = "ANDROID_TIMELINE_CONFIG"
HOME_ENV: Final = "ANDROID_TIMELINE_HOME"


class ConfigError(Exception):
    """Raised when configuration is missing, malformed or unsafe."""


def default_data_dir() -> Path:
    """Directory holding the SQLite outbox and runtime state."""
    override = os.environ.get(HOME_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "android-timeline"


def default_config_path() -> Path:
    """Location of ``config.toml``."""
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "android-timeline" / "config.toml"


@dataclass(slots=True)
class DeviceConfig:
    #: Pseudonymous identifier. Never a hardware serial, IMEI or phone number.
    device_id: str = "device-unconfigured"


@dataclass(slots=True)
class ServerConfig:
    base_url: str = ""
    token_file: str = "~/.config/android-timeline/token"  # noqa: S105 - a path
    timeout_seconds: float = 30.0
    verify_tls: bool = True
    allow_insecure_test_endpoint: bool = False
    max_batch_events: int = 500
    max_batch_bytes: int = 1_048_576
    compression: bool = True
    compression_min_bytes: int = 4096

    def token(self) -> str:
        """Read the bearer token from the environment or the token file."""
        env_token = os.environ.get(TOKEN_ENV)
        if env_token:
            return env_token.strip()
        path = Path(self.token_file).expanduser()
        if not path.is_file():
            raise ConfigError(
                f"device token not found: set ${TOKEN_ENV} or create {path} "
                "(chmod 600) with the token shown once during enrollment"
            )
        return path.read_text(encoding="utf-8").strip()

    def has_token(self) -> bool:
        try:
            return bool(self.token())
        except ConfigError:
            return False


@dataclass(slots=True)
class UploadConfig:
    max_attempts: int = 6
    initial_backoff_seconds: float = 2.0
    max_backoff_seconds: float = 900.0
    jitter_ratio: float = 0.25
    #: After this many failed attempts an event is parked in the dead-letter
    #: state. It is still stored -- never dropped -- just no longer retried.
    dead_letter_after_attempts: int = 25
    interval_seconds: int = 300


@dataclass(slots=True)
class PrivacyConfig:
    salt_file: str = "~/.config/android-timeline/salt"
    #: Message bodies and contact names are opt-in, and off by default.
    store_message_bodies: bool = False
    store_contact_names: bool = False
    location_mode: str = "disabled"
    geohash_precision: int = 6
    coarse_decimal_places: int = 2

    def salt(self) -> str:
        """Local pseudonymisation salt.

        Without a salt, phone numbers are hashed with a fixed-length
        placeholder that is *not* reversible but also not unlinkable across
        devices -- so ``doctor`` warns loudly when it is missing.
        """
        env_salt = os.environ.get(SALT_ENV)
        if env_salt:
            return env_salt.strip()
        path = Path(self.salt_file).expanduser()
        if not path.is_file():
            raise ConfigError(
                f"pseudonymisation salt not found: set ${SALT_ENV} or create "
                f"{path} (chmod 600). Never commit this value."
            )
        return path.read_text(encoding="utf-8").strip()

    def has_salt(self) -> bool:
        try:
            return bool(self.salt())
        except ConfigError:
            return False


@dataclass(slots=True)
class RetentionConfig:
    #: Disabled by default: acknowledged raw events are kept forever.
    enabled: bool = False
    delete_acknowledged_after_days: int = 0


@dataclass(slots=True)
class CollectorConfig:
    name: str
    enabled: bool = False
    interval_seconds: int = 300
    options: dict[str, Any] = field(default_factory=dict)


#: Built-in collector defaults. Sensitive sources start disabled.
DEFAULT_COLLECTORS: Final[dict[str, dict[str, Any]]] = {
    "battery": {"enabled": True, "interval_seconds": 300},
    "wifi": {"enabled": True, "interval_seconds": 300},
    "sensors": {"enabled": False, "interval_seconds": 900},
    "location": {"enabled": False, "interval_seconds": 900},
    "calls": {"enabled": False, "interval_seconds": 3600},
    "sms": {"enabled": False, "interval_seconds": 3600},
}


@dataclass(slots=True)
class Config:
    device: DeviceConfig = field(default_factory=DeviceConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    upload: UploadConfig = field(default_factory=UploadConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    collectors: dict[str, CollectorConfig] = field(default_factory=dict)
    data_dir: Path = field(default_factory=default_data_dir)
    source_path: Path | None = None
    heartbeat_interval_seconds: int = 900

    @property
    def database_path(self) -> Path:
        return self.data_dir / "outbox.sqlite3"

    def enabled_collectors(self) -> list[str]:
        return sorted(name for name, c in self.collectors.items() if c.enabled)


def _coerce(section: dict[str, Any], key: str, expected: type, where: str) -> Any:
    value = section[key]
    if expected is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, expected) or (
        expected is not bool and isinstance(value, bool)
    ):
        raise ConfigError(
            f"{where}.{key} must be {expected.__name__}, got {type(value).__name__}"
        )
    return value


def _apply(target: Any, section: dict[str, Any], where: str) -> None:
    """Copy known keys from a TOML table onto a dataclass, type-checking."""
    fields = {f: type(getattr(target, f)) for f in target.__slots__}
    unknown = set(section) - set(fields)
    if unknown:
        raise ConfigError(f"unknown key(s) in [{where}]: {', '.join(sorted(unknown))}")
    for key, expected in fields.items():
        if key in section:
            setattr(target, key, _coerce(section, key, expected, where))


def _validate(config: Config) -> None:
    device_id = config.device.device_id
    if not device_id or len(device_id) > 128:
        raise ConfigError("device.device_id must be 1-128 characters")
    if not all(c.isalnum() or c in "._:-" for c in device_id):
        raise ConfigError(
            "device.device_id may only contain letters, digits, '.', '_', ':' and '-'"
        )

    url = config.server.base_url
    if not url:
        raise ConfigError("server.base_url is required (run 'android-timeline init')")
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise ConfigError("server.base_url must be an http(s) URL")
    if not parsed.hostname:
        raise ConfigError("server.base_url must include a hostname")

    if parsed.scheme == "http":
        _require_test_mode(config, parsed.hostname)
    if not config.server.verify_tls:
        _require_test_mode(config, parsed.hostname)

    if config.privacy.location_mode not in LOCATION_MODES:
        raise ConfigError(
            f"privacy.location_mode must be one of {', '.join(LOCATION_MODES)}"
        )
    if not 1 <= config.privacy.geohash_precision <= 12:
        raise ConfigError("privacy.geohash_precision must be between 1 and 12")
    if not 0 <= config.privacy.coarse_decimal_places <= 6:
        raise ConfigError("privacy.coarse_decimal_places must be between 0 and 6")

    if not 1 <= config.server.max_batch_events <= 1000:
        raise ConfigError("server.max_batch_events must be between 1 and 1000")
    if not 1024 <= config.server.max_batch_bytes <= 16 * 1_048_576:
        raise ConfigError("server.max_batch_bytes must be between 1 KiB and 16 MiB")
    if config.server.timeout_seconds <= 0:
        raise ConfigError("server.timeout_seconds must be positive")
    if config.upload.max_attempts < 1:
        raise ConfigError("upload.max_attempts must be at least 1")
    if not 0.0 <= config.upload.jitter_ratio <= 1.0:
        raise ConfigError("upload.jitter_ratio must be between 0.0 and 1.0")

    if config.retention.enabled and config.retention.delete_acknowledged_after_days < 1:
        raise ConfigError(
            "retention.delete_acknowledged_after_days must be >= 1 when retention "
            "is enabled"
        )

    for name, collector in config.collectors.items():
        if collector.interval_seconds < 10:
            raise ConfigError(f"collectors.{name}.interval_seconds must be >= 10")

    sms = config.collectors.get("sms")
    if (
        sms
        and sms.enabled
        and config.privacy.store_message_bodies
        and not config.privacy.has_salt()
    ):
        raise ConfigError("privacy.store_message_bodies requires a pseudonymisation salt")


def _require_test_mode(config: Config, hostname: str) -> None:
    """Refuse plaintext HTTP / disabled TLS verification outside test mode."""
    reasons: list[str] = []
    if not config.server.allow_insecure_test_endpoint:
        reasons.append("server.allow_insecure_test_endpoint is false")
    if os.environ.get(INSECURE_TEST_MODE_ENV) != INSECURE_TEST_MODE_PHRASE:
        reasons.append(
            f"${INSECURE_TEST_MODE_ENV} is not set to '{INSECURE_TEST_MODE_PHRASE}'"
        )
    if hostname not in _TEST_HOSTS:
        reasons.append(
            f"host '{hostname}' is not a loopback or emulator address "
            f"({', '.join(sorted(_TEST_HOSTS))})"
        )
    if reasons:
        raise ConfigError(
            "refusing to use an insecure endpoint in a non-test configuration: "
            + "; ".join(reasons)
        )


def load_config(path: Path | str | None = None) -> Config:
    """Load, merge and validate configuration from ``path``."""
    config_path = Path(path).expanduser() if path else default_config_path()
    if not config_path.is_file():
        raise ConfigError(
            f"configuration not found at {config_path}. "
            "Run 'android-timeline init' to create one from the example."
        )
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path} is not valid TOML: {exc}") from exc

    config = Config(source_path=config_path)

    known_sections = {
        "device",
        "server",
        "upload",
        "privacy",
        "retention",
        "collectors",
        "runtime",
    }
    unknown = set(raw) - known_sections
    if unknown:
        raise ConfigError(f"unknown config section(s): {', '.join(sorted(unknown))}")

    for name, target in (
        ("device", config.device),
        ("server", config.server),
        ("upload", config.upload),
        ("privacy", config.privacy),
        ("retention", config.retention),
    ):
        section = raw.get(name, {})
        if not isinstance(section, dict):
            raise ConfigError(f"[{name}] must be a table")
        _apply(target, section, name)

    runtime = raw.get("runtime", {})
    if not isinstance(runtime, dict):
        raise ConfigError("[runtime] must be a table")
    if "data_dir" in runtime:
        config.data_dir = Path(str(runtime["data_dir"])).expanduser()
    if "heartbeat_interval_seconds" in runtime:
        config.heartbeat_interval_seconds = int(runtime["heartbeat_interval_seconds"])
    if config.heartbeat_interval_seconds < 60:
        raise ConfigError("runtime.heartbeat_interval_seconds must be >= 60")

    collectors_raw = raw.get("collectors", {})
    if not isinstance(collectors_raw, dict):
        raise ConfigError("[collectors] must be a table")
    unknown_collectors = set(collectors_raw) - set(DEFAULT_COLLECTORS)
    if unknown_collectors:
        raise ConfigError(
            f"unknown collector(s): {', '.join(sorted(unknown_collectors))}. "
            f"Known: {', '.join(sorted(DEFAULT_COLLECTORS))}"
        )
    for name, defaults in DEFAULT_COLLECTORS.items():
        section = collectors_raw.get(name, {})
        if not isinstance(section, dict):
            raise ConfigError(f"[collectors.{name}] must be a table")
        options = {
            k: v for k, v in section.items() if k not in ("enabled", "interval_seconds")
        }
        config.collectors[name] = CollectorConfig(
            name=name,
            enabled=bool(section.get("enabled", defaults["enabled"])),
            interval_seconds=int(
                section.get("interval_seconds", defaults["interval_seconds"])
            ),
            options=options,
        )

    _validate(config)
    return config
