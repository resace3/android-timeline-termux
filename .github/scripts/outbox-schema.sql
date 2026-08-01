-- Outbox schema, extracted from android_timeline/database.py migration 1.
--
-- Used by the Android emulator job to prove the schema is accepted by the
-- device's own SQLite build. tests/unit/test_schema_sql.py asserts this file
-- stays byte-identical to the migration, so it cannot silently drift.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version        INTEGER PRIMARY KEY,
    applied_at_utc TEXT NOT NULL
);

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
