"""The emulator's schema file must not drift from the real migration.

The Android emulator job runs ``.github/scripts/outbox-schema.sql`` through
the device's own SQLite to prove the schema is portable. If that file and
``android_timeline.database`` ever disagree, the emulator would be testing a
schema the collector does not use -- which is worse than not testing at all.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from android_timeline.database import _MIGRATIONS, split_statements

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_SQL = REPO_ROOT / ".github" / "scripts" / "outbox-schema.sql"


def _statements(sql: str) -> list[str]:
    """Normalise SQL into a comparable set of whitespace-collapsed statements.

    Uses the same splitter the migration runner uses, so the comparison
    cannot pass because of a difference in how the two are parsed.
    """
    return sorted(re.sub(r"\s+", " ", statement) for statement in split_statements(sql))


def test_schema_file_exists() -> None:
    assert SCHEMA_SQL.is_file()


def test_schema_file_matches_the_migration() -> None:
    migration = _statements(_MIGRATIONS[0][1])
    on_disk = _statements(SCHEMA_SQL.read_text(encoding="utf-8"))

    # The file additionally creates schema_migrations, which the migration
    # runner creates separately in Outbox.migrate().
    extra = [s for s in on_disk if "schema_migrations" in s]
    assert len(extra) == 1, "the file should create schema_migrations exactly once"

    assert [s for s in on_disk if "schema_migrations" not in s] == migration


def test_schema_file_is_executable_by_sqlite(tmp_path: Path) -> None:
    connection = sqlite3.connect(str(tmp_path / "probe.sqlite3"))
    try:
        connection.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        connection.close()

    assert {
        "events",
        "sync_batches",
        "collector_status",
        "schema_migrations",
        "runtime_state",
    } <= tables
