"""Append-only SQLite outbox.

Design rules that the tests enforce:

* An event is written **once**. ``event_id`` is the primary key, so a replay
  of the same observation is a no-op rather than a duplicate row.
* Acknowledged raw events are **never** deleted unless retention is
  explicitly enabled in config (it is off by default).
* Batch claim/commit is transactional: a crash mid-upload leaves events
  pending, never silently lost and never marked uploaded.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from .models import Event, iso_utc, utc_now

__all__ = ["SCHEMA_VERSION", "Outbox", "OutboxStats", "split_statements"]


def split_statements(script: str) -> list[str]:
    """Split a migration script into individual SQL statements.

    Safe for our schema, which contains no semicolons inside string
    literals. Comment lines are stripped so the same helper can read the
    standalone copy of the schema used by the Android emulator job.
    """
    without_comments = "\n".join(
        line for line in script.splitlines() if not line.strip().startswith("--")
    )
    return [
        statement.strip()
        for statement in without_comments.split(";")
        if statement.strip()
    ]


#: Version of the *local database* layout (distinct from the event schema).
SCHEMA_VERSION = 1

_MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS events (
            event_id                TEXT PRIMARY KEY,
            device_id               TEXT NOT NULL,
            source                  TEXT NOT NULL,
            event_type              TEXT NOT NULL,
            event_time_utc          TEXT NOT NULL,
            collected_time_utc      TEXT NOT NULL,
            timezone_offset_minutes INTEGER NOT NULL,
            schema_version          INTEGER NOT NULL,
            quality_flags           TEXT NOT NULL DEFAULT '[]',
            payload                 TEXT NOT NULL DEFAULT '{}',
            created_at_utc          TEXT NOT NULL,
            uploaded                INTEGER NOT NULL DEFAULT 0,
            upload_attempts         INTEGER NOT NULL DEFAULT 0,
            last_upload_error       TEXT,
            last_attempt_at_utc     TEXT,
            acknowledged_at_utc     TEXT,
            dead_letter             INTEGER NOT NULL DEFAULT 0,
            batch_id                TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_events_pending
            ON events (uploaded, dead_letter, created_at_utc);
        CREATE INDEX IF NOT EXISTS idx_events_event_time
            ON events (event_time_utc);
        CREATE INDEX IF NOT EXISTS idx_events_source
            ON events (source, event_time_utc);
        CREATE INDEX IF NOT EXISTS idx_events_batch
            ON events (batch_id);

        CREATE TABLE IF NOT EXISTS sync_batches (
            batch_id        TEXT PRIMARY KEY,
            device_id       TEXT NOT NULL,
            created_at_utc  TEXT NOT NULL,
            event_count     INTEGER NOT NULL DEFAULT 0,
            status          TEXT NOT NULL DEFAULT 'pending',
            attempts        INTEGER NOT NULL DEFAULT 0,
            last_error      TEXT,
            completed_at_utc TEXT,
            accepted_count  INTEGER NOT NULL DEFAULT 0,
            rejected_count  INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_batches_status
            ON sync_batches (status, created_at_utc);

        CREATE TABLE IF NOT EXISTS collector_status (
            source              TEXT PRIMARY KEY,
            availability        TEXT NOT NULL DEFAULT 'unavailable',
            enabled             INTEGER NOT NULL DEFAULT 0,
            last_run_at_utc     TEXT,
            last_success_at_utc TEXT,
            last_error          TEXT,
            error_count         INTEGER NOT NULL DEFAULT 0,
            success_count       INTEGER NOT NULL DEFAULT 0,
            event_count         INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS runtime_state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """,
    ),
]


@dataclass(slots=True)
class OutboxStats:
    total_events: int = 0
    pending_events: int = 0
    uploaded_events: int = 0
    dead_letter_events: int = 0
    oldest_pending_utc: str | None = None
    newest_event_utc: str | None = None
    last_successful_upload_utc: str | None = None
    pending_batches: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_events": self.total_events,
            "pending_events": self.pending_events,
            "uploaded_events": self.uploaded_events,
            "dead_letter_events": self.dead_letter_events,
            "oldest_pending_utc": self.oldest_pending_utc,
            "newest_event_utc": self.newest_event_utc,
            "last_successful_upload_utc": self.last_successful_upload_utc,
            "pending_batches": self.pending_batches,
        }


class Outbox:
    """SQLite-backed event queue.

    Safe to open from more than one process (WAL + busy timeout), which is
    what lets ``ingest-event`` from Tasker run while the daemon is looping.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self.migrate()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Outbox:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Explicit IMMEDIATE transaction -- writers never interleave."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # ------------------------------------------------------------------
    # migrations
    # ------------------------------------------------------------------

    def migrate(self) -> int:
        """Apply outstanding migrations; returns the resulting version."""
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version        INTEGER PRIMARY KEY,
                applied_at_utc TEXT NOT NULL
            )
            """
        )
        current = self.schema_version()
        for version, script in _MIGRATIONS:
            if version <= current:
                continue
            # Statements are executed one at a time rather than via
            # executescript(): that helper implicitly commits, which would
            # end the surrounding transaction and leave a half-applied
            # migration recoverable only by hand.
            with self.transaction() as conn:
                for statement in split_statements(script):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at_utc) "
                    "VALUES (?, ?)",
                    (version, iso_utc(utc_now())),
                )
            current = version
        return current

    def schema_version(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations"
        ).fetchone()
        return int(row["v"]) if row else 0

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------

    def add_event(self, event: Event) -> bool:
        """Insert one event. Returns ``False`` if it was already present."""
        return self.add_events([event]) == 1

    def add_events(self, events: Sequence[Event]) -> int:
        """Insert events, ignoring ids that already exist.

        Returns the number of *newly stored* events, so callers can tell a
        real observation from a replay.
        """
        if not events:
            return 0
        now = iso_utc(utc_now())
        rows = [
            (
                e.event_id,
                e.device_id,
                e.source,
                e.event_type,
                e.event_time_utc,
                e.collected_time_utc,
                e.timezone_offset_minutes,
                e.schema_version,
                json.dumps(sorted(set(e.quality_flags)), separators=(",", ":")),
                json.dumps(e.payload, sort_keys=True, separators=(",", ":")),
                now,
            )
            for e in events
        ]
        with self.transaction() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT OR IGNORE INTO events (
                    event_id, device_id, source, event_type, event_time_utc,
                    collected_time_utc, timezone_offset_minutes, schema_version,
                    quality_flags, payload, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            inserted = conn.total_changes - before
        return int(inserted)

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return _row_to_event_dict(row) if row else None

    def count_events(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"])

    def pending_events(self, limit: int = 500) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT * FROM events
            WHERE uploaded = 0 AND dead_letter = 0
            ORDER BY created_at_utc, event_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [_row_to_event_dict(row) for row in rows]

    def export_events(
        self,
        *,
        start_utc: str | None = None,
        end_utc: str | None = None,
        sources: Sequence[str] | None = None,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if start_utc:
            clauses.append("event_time_utc >= ?")
            params.append(start_utc)
        if end_utc:
            clauses.append("event_time_utc < ?")
            params.append(end_utc)
        if sources:
            clauses.append(f"source IN ({','.join('?' * len(sources))})")
            params.extend(sources)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        # Only placeholders are interpolated here: every caller-supplied value
        # travels as a bound parameter, so a source name containing SQL is
        # matched literally rather than executed.
        rows = self._conn.execute(
            f"SELECT * FROM events {where} ORDER BY event_time_utc, event_id LIMIT ?",  # noqa: S608  # nosec B608
            params,
        ).fetchall()
        return [_row_to_event_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # batches
    # ------------------------------------------------------------------

    def claim_batch(
        self,
        device_id: str,
        *,
        max_events: int,
        max_bytes: int,
        batch_id: str | None = None,
    ) -> tuple[str, list[Event]] | None:
        """Reserve up to ``max_events`` pending events for one upload.

        Claiming is transactional: the events are stamped with the batch id
        inside the same transaction that creates the batch row, so a crash
        can never produce a batch referencing events that were never claimed.
        """
        identifier = batch_id or f"batch-{uuid.uuid4()}"
        with self.transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM events
                WHERE uploaded = 0 AND dead_letter = 0
                ORDER BY created_at_utc, event_id
                LIMIT ?
                """,
                (max_events,),
            ).fetchall()
            if not rows:
                return None

            selected: list[Event] = []
            running_bytes = 0
            for row in rows:
                event = Event.from_dict(_row_to_event_dict(row))
                encoded = len(
                    json.dumps(event.to_dict(), separators=(",", ":")).encode("utf-8")
                )
                if selected and running_bytes + encoded > max_bytes:
                    break
                running_bytes += encoded
                selected.append(event)

            conn.execute(
                """
                INSERT INTO sync_batches
                    (batch_id, device_id, created_at_utc, event_count, status)
                VALUES (?, ?, ?, ?, 'pending')
                """,
                (identifier, device_id, iso_utc(utc_now()), len(selected)),
            )
            conn.executemany(
                "UPDATE events SET batch_id = ? WHERE event_id = ?",
                [(identifier, e.event_id) for e in selected],
            )
        return identifier, selected

    def record_batch_attempt(self, batch_id: str, error: str | None = None) -> None:
        """Increment attempt counters for a batch and its events."""
        now = iso_utc(utc_now())
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE sync_batches
                SET attempts = attempts + 1,
                    last_error = ?,
                    status = CASE WHEN ? IS NULL THEN status ELSE 'failed' END
                WHERE batch_id = ?
                """,
                (error, error, batch_id),
            )
            conn.execute(
                """
                UPDATE events
                SET upload_attempts = upload_attempts + 1,
                    last_attempt_at_utc = ?,
                    last_upload_error = ?
                WHERE batch_id = ? AND uploaded = 0
                """,
                (now, error, batch_id),
            )

    def complete_batch(
        self,
        batch_id: str,
        *,
        accepted_ids: Sequence[str],
        rejected: dict[str, str] | None = None,
        dead_letter_after_attempts: int = 25,
    ) -> dict[str, int]:
        """Apply a server acknowledgement to the queue.

        Events the server accepted (stored *or* duplicate) are marked
        uploaded. Everything else stays pending for the next attempt, which
        is what makes partial acknowledgement safe.
        """
        rejected = rejected or {}
        now = iso_utc(utc_now())
        with self.transaction() as conn:
            if accepted_ids:
                conn.executemany(
                    """
                    UPDATE events
                    SET uploaded = 1,
                        acknowledged_at_utc = ?,
                        last_upload_error = NULL
                    WHERE event_id = ?
                    """,
                    [(now, event_id) for event_id in accepted_ids],
                )
            for event_id, reason in rejected.items():
                conn.execute(
                    """
                    UPDATE events
                    SET last_upload_error = ?,
                        upload_attempts = upload_attempts + 1
                    WHERE event_id = ?
                    """,
                    (f"rejected: {reason}"[:500], event_id),
                )

            # Park permanently-failing events instead of retrying forever.
            conn.execute(
                """
                UPDATE events
                SET dead_letter = 1
                WHERE uploaded = 0 AND dead_letter = 0
                  AND upload_attempts >= ?
                """,
                (dead_letter_after_attempts,),
            )

            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN uploaded = 1 THEN 1 ELSE 0 END) AS accepted,
                    SUM(CASE WHEN uploaded = 0 THEN 1 ELSE 0 END) AS outstanding
                FROM events WHERE batch_id = ?
                """,
                (batch_id,),
            ).fetchone()
            accepted_count = int(row["accepted"] or 0)
            outstanding = int(row["outstanding"] or 0)

            conn.execute(
                """
                UPDATE sync_batches
                SET status = ?,
                    accepted_count = ?,
                    rejected_count = ?,
                    completed_at_utc = ?,
                    last_error = NULL
                WHERE batch_id = ?
                """,
                (
                    "acknowledged" if outstanding == 0 else "partial",
                    accepted_count,
                    len(rejected),
                    now,
                    batch_id,
                ),
            )
            # Free unacknowledged events so a later batch can pick them up.
            conn.execute(
                "UPDATE events SET batch_id = NULL WHERE batch_id = ? AND uploaded = 0",
                (batch_id,),
            )
            conn.execute(
                "INSERT INTO runtime_state (key, value) VALUES ('last_upload_utc', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (now,),
            )
        return {
            "accepted": accepted_count,
            "rejected": len(rejected),
            "outstanding": outstanding,
        }

    def release_batch(self, batch_id: str) -> None:
        """Return a failed batch's events to the pending pool."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE events SET batch_id = NULL WHERE batch_id = ? AND uploaded = 0",
                (batch_id,),
            )
            conn.execute(
                "UPDATE sync_batches SET status = 'failed' WHERE batch_id = ?",
                (batch_id,),
            )

    def batch(self, batch_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM sync_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # collector status
    # ------------------------------------------------------------------

    def update_collector_status(
        self,
        source: str,
        *,
        availability: str,
        enabled: bool,
        succeeded: bool,
        events: int = 0,
        error: str | None = None,
    ) -> None:
        now = iso_utc(utc_now())
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO collector_status (source, availability, enabled)
                VALUES (?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    availability = excluded.availability,
                    enabled = excluded.enabled
                """,
                (source, availability, int(enabled)),
            )
            if succeeded:
                conn.execute(
                    """
                    UPDATE collector_status
                    SET last_run_at_utc = ?, last_success_at_utc = ?,
                        success_count = success_count + 1,
                        event_count = event_count + ?,
                        last_error = NULL
                    WHERE source = ?
                    """,
                    (now, now, events, source),
                )
            else:
                conn.execute(
                    """
                    UPDATE collector_status
                    SET last_run_at_utc = ?, error_count = error_count + 1,
                        last_error = ?
                    WHERE source = ?
                    """,
                    (now, (error or "unknown error")[:500], source),
                )

    def collector_status(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM collector_status ORDER BY source"
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # runtime state / stats / retention
    # ------------------------------------------------------------------

    def set_state(self, key: str, value: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO runtime_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_state(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM runtime_state WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row else None

    def stats(self) -> OutboxStats:
        row = self._conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN uploaded = 0 AND dead_letter = 0 THEN 1 ELSE 0 END)
                    AS pending,
                SUM(CASE WHEN uploaded = 1 THEN 1 ELSE 0 END) AS uploaded,
                SUM(CASE WHEN dead_letter = 1 THEN 1 ELSE 0 END) AS dead,
                MIN(CASE WHEN uploaded = 0 AND dead_letter = 0
                    THEN event_time_utc END) AS oldest_pending,
                MAX(event_time_utc) AS newest
            FROM events
            """
        ).fetchone()
        batches = self._conn.execute(
            "SELECT COUNT(*) AS c FROM sync_batches WHERE status IN "
            "('pending', 'failed', 'partial')"
        ).fetchone()
        return OutboxStats(
            total_events=int(row["total"] or 0),
            pending_events=int(row["pending"] or 0),
            uploaded_events=int(row["uploaded"] or 0),
            dead_letter_events=int(row["dead"] or 0),
            oldest_pending_utc=row["oldest_pending"],
            newest_event_utc=row["newest"],
            last_successful_upload_utc=self.get_state("last_upload_utc"),
            pending_batches=int(batches["c"] or 0),
        )

    def apply_retention(self, *, enabled: bool, older_than_days: int) -> int:
        """Delete acknowledged events older than ``older_than_days``.

        A no-op unless retention is explicitly enabled. Pending, dead-letter
        and unacknowledged events are never touched.
        """
        if not enabled or older_than_days < 1:
            return 0
        cutoff = iso_utc(utc_now() - timedelta(days=older_than_days))
        with self.transaction() as conn:
            before = conn.total_changes
            conn.execute(
                """
                DELETE FROM events
                WHERE uploaded = 1
                  AND acknowledged_at_utc IS NOT NULL
                  AND acknowledged_at_utc < ?
                """,
                (cutoff,),
            )
            deleted = conn.total_changes - before
        return int(deleted)


def _row_to_event_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "device_id": row["device_id"],
        "source": row["source"],
        "event_type": row["event_type"],
        "event_time_utc": row["event_time_utc"],
        "collected_time_utc": row["collected_time_utc"],
        "timezone_offset_minutes": int(row["timezone_offset_minutes"]),
        "schema_version": int(row["schema_version"]),
        "quality_flags": json.loads(row["quality_flags"] or "[]"),
        "payload": json.loads(row["payload"] or "{}"),
    }
