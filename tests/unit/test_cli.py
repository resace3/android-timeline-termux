"""CLI surface: every command runs, and none of them print a secret."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from android_timeline.cli import EXAMPLE_CONFIG, main
from android_timeline.config import Config

REPO_ROOT = Path(__file__).resolve().parents[2]


def run(args: list[str], config_path: Path | None = None) -> int:
    base = ["--json"]
    if config_path is not None:
        base = ["--config", str(config_path), *base]
    return main([*base, *args])


class TestInit:
    def test_print_example_writes_valid_toml(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["init", "--print-example"]) == 0
        import tomllib

        parsed = tomllib.loads(capsys.readouterr().out)
        assert parsed["device"]["device_id"]

    def test_creates_a_config_file(self, tmp_path: Path) -> None:
        target = tmp_path / "new" / "config.toml"
        assert main(["--config", str(target), "init"]) == 0
        assert target.is_file()

    def test_refuses_to_overwrite_without_force(self, tmp_path: Path) -> None:
        target = tmp_path / "config.toml"
        target.write_text("# existing\n", encoding="utf-8")
        assert main(["--config", str(target), "init"]) == 1
        assert target.read_text(encoding="utf-8") == "# existing\n"

    def test_force_overwrites(self, tmp_path: Path) -> None:
        target = tmp_path / "config.toml"
        target.write_text("# existing\n", encoding="utf-8")
        assert main(["--config", str(target), "init", "--force"]) == 0
        assert "device_id" in target.read_text(encoding="utf-8")

    def test_committed_example_matches_the_embedded_template(self) -> None:
        committed = (REPO_ROOT / "config.example.toml").read_text(encoding="utf-8")
        assert committed == EXAMPLE_CONFIG

    def test_the_example_config_is_loadable(self, tmp_path: Path) -> None:
        from android_timeline.config import load_config

        path = tmp_path / "config.toml"
        path.write_text(EXAMPLE_CONFIG, encoding="utf-8")
        assert load_config(path).device.device_id == "device-example-001"


class TestCollectAndStatus:
    def test_collect_once(
        self,
        config_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "ANDROID_TIMELINE_TERMUX_MOCK_DIR",
            str(REPO_ROOT / "tests" / "fixtures" / "termux"),
        )
        assert run(["collect-once", "--heartbeat"], config_path) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["events_stored"] > 0
        assert report["heartbeats"] == 1

    def test_status_reports_versions_and_queue(
        self, config_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(["status"], config_path) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["protocol_version"] == 1
        assert payload["device_id"] == "device-test-001"
        assert payload["queue"]["total_events"] == 0

    def test_export_emits_json(
        self,
        config_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv(
            "ANDROID_TIMELINE_TERMUX_MOCK_DIR",
            str(REPO_ROOT / "tests" / "fixtures" / "termux"),
        )
        run(["collect-once"], config_path)
        capsys.readouterr()

        output = tmp_path / "export.json"
        assert run(["export", "--output", str(output)], config_path) == 0
        events = json.loads(output.read_text(encoding="utf-8"))
        assert events and all("event_id" in e for e in events)


class TestIngestEvent:
    def test_records_a_tasker_event(
        self, config_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = run(
            [
                "ingest-event",
                "--source",
                "tasker",
                "--type",
                "screen_on",
                "--payload",
                '{"trigger": "test"}',
            ],
            config_path,
        )
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["stored"] is True
        assert payload["duplicate"] is False

    def test_identical_event_is_reported_as_duplicate(
        self, config_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = [
            "ingest-event",
            "--source",
            "tasker",
            "--type",
            "screen_on",
            "--payload",
            "{}",
            "--event-time",
            "2026-03-15T08:00:00Z",
        ]
        run(args, config_path)
        capsys.readouterr()
        run(args, config_path)
        payload = json.loads(capsys.readouterr().out)
        assert payload["duplicate"] is True

    def test_invalid_json_payload_is_rejected(self, config_path: Path) -> None:
        assert (
            run(
                ["ingest-event", "--source", "tasker", "--type", "x", "--payload", "{"],
                config_path,
            )
            == 1
        )

    def test_non_object_payload_is_rejected(self, config_path: Path) -> None:
        assert (
            run(
                ["ingest-event", "--source", "tasker", "--type", "x", "--payload", "[1]"],
                config_path,
            )
            == 1
        )

    def test_unknown_quality_flag_is_rejected(self, config_path: Path) -> None:
        assert (
            run(
                [
                    "ingest-event",
                    "--source",
                    "tasker",
                    "--type",
                    "x",
                    "--quality-flag",
                    "totally_made_up",
                ],
                config_path,
            )
            == 1
        )

    def test_bad_event_time_is_rejected(self, config_path: Path) -> None:
        assert (
            run(
                [
                    "ingest-event",
                    "--source",
                    "tasker",
                    "--type",
                    "x",
                    "--event-time",
                    "yesterday",
                ],
                config_path,
            )
            == 1
        )


class TestDoctor:
    def test_reports_checks_without_touching_the_network(
        self, config_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(["doctor"], config_path)
        report = json.loads(capsys.readouterr().out)
        names = {entry["check"] for entry in report["checks"]}
        assert {
            "termux_environment",
            "configuration",
            "termux_api_commands",
            "database_writable",
            "queue",
            "device_token",
            "pseudonymisation_salt",
            "server_reachable",
            "last_successful_upload",
        } <= names

    def test_never_prints_the_token(
        self, config: Config, config_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(["doctor"], config_path)
        output = capsys.readouterr().out
        assert config.server.token() not in output
        assert config.privacy.salt() not in output
        assert "<redacted" in output

    def test_fails_when_configuration_is_missing(self, tmp_path: Path) -> None:
        assert main(["--config", str(tmp_path / "absent.toml"), "--json", "doctor"]) == 1

    def test_reports_missing_termux_commands(
        self, config_path: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        # An empty mock directory means every termux-* command is absent.
        import os

        os.environ["ANDROID_TIMELINE_TERMUX_MOCK_DIR"] = str(tmp_path / "empty")
        (tmp_path / "empty").mkdir()
        try:
            run(["doctor"], config_path)
            report = json.loads(capsys.readouterr().out)
            entry = next(
                e for e in report["checks"] if e["check"] == "termux_api_commands"
            )
            assert entry["status"] == "warn"
            assert entry["detail"]["battery"]["availability"] == "unavailable"
        finally:
            os.environ.pop("ANDROID_TIMELINE_TERMUX_MOCK_DIR", None)


class TestUpload:
    def test_upload_with_no_pending_events_succeeds(
        self, config_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(["upload"], config_path) == 0
        assert json.loads(capsys.readouterr().out)["batches_sent"] == 0


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip()


def test_unknown_command_exits_with_usage_error() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["nonsense"])
    assert exit_info.value.code == 2
