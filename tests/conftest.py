"""Shared pytest fixtures.

Every fixture is hermetic: nothing reads the developer's real home
directory, and no test ever contacts a network host it did not itself start.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
TERMUX_FIXTURES = FIXTURES / "termux"
SCHEMA_DIR = REPO_ROOT / "schemas"

if str(REPO_ROOT) not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(REPO_ROOT))

from android_timeline.collectors import MockCommandRunner  # noqa: E402
from android_timeline.config import Config, load_config  # noqa: E402
from android_timeline.database import Outbox  # noqa: E402

TEST_SALT = "synthetic-test-salt-not-a-real-secret"
TEST_TOKEN = "synthetic-test-token-not-a-real-secret"


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point every path-derived default at a temporary directory."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ANDROID_TIMELINE_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("ANDROID_TIMELINE_CONFIG", raising=False)
    monkeypatch.delenv("ANDROID_TIMELINE_TERMUX_MOCK_DIR", raising=False)
    monkeypatch.delenv("ANDROID_TIMELINE_INSECURE_TEST_MODE", raising=False)
    monkeypatch.setenv("ANDROID_TIMELINE_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("ANDROID_TIMELINE_SALT", TEST_SALT)
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)


@pytest.fixture
def config_text() -> str:
    """A minimal valid configuration with every collector enabled."""
    return """
[device]
device_id = "device-test-001"

[server]
base_url = "https://timeline.example.invalid:8099"

[privacy]
location_mode = "coarse"

[collectors.battery]
enabled = true
interval_seconds = 60

[collectors.wifi]
enabled = true
interval_seconds = 60

[collectors.sensors]
enabled = true
interval_seconds = 60

[collectors.location]
enabled = true
interval_seconds = 60

[collectors.calls]
enabled = true
interval_seconds = 60

[collectors.sms]
enabled = true
interval_seconds = 60
"""


@pytest.fixture
def config_path(tmp_path: Path, config_text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(config_text, encoding="utf-8")
    return path


@pytest.fixture
def config(config_path: Path, tmp_path: Path) -> Config:
    loaded = load_config(config_path)
    loaded.data_dir = tmp_path / "data"
    return loaded


@pytest.fixture
def outbox(config: Config) -> Iterator[Outbox]:
    with Outbox(config.database_path) as box:
        yield box


@pytest.fixture
def mock_runner() -> MockCommandRunner:
    return MockCommandRunner(TERMUX_FIXTURES)


@pytest.fixture
def synthetic_day() -> dict[str, Any]:
    from tests.fixtures.synthetic_day import build_synthetic_day

    return dict(build_synthetic_day())


@pytest.fixture
def schemas() -> dict[str, dict[str, Any]]:
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(SCHEMA_DIR.glob("*.schema.json"))
    }


@pytest.fixture
def schema_validator(schemas: dict[str, dict[str, Any]]):  # type: ignore[no-untyped-def]
    """A jsonschema validator factory with local ``$ref`` resolution.

    The batch schema references the event schema by ``$id``; wiring a local
    registry keeps the published schemas honest without needing network
    access during tests.
    """
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    registry: Registry = Registry()  # type: ignore[type-arg]
    for document in schemas.values():
        registry = registry.with_resource(
            document["$id"], Resource.from_contents(document)
        )

    def make(name: str) -> Draft202012Validator:
        return Draft202012Validator(schemas[name], registry=registry)

    return make
