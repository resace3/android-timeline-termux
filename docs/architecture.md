# Architecture

## Design constraints

The shape of this collector follows from four facts about Android:

1. **The process will be killed.** Doze, App Standby and manufacturer task
   killers stop background work without warning. Therefore: no in-memory
   state that matters, cheap restarts, and everything durable in SQLite
   before it is acknowledged.
2. **The network will be absent.** Sometimes for days. Therefore: an
   append-only outbox, idempotent uploads, and a queue that is never drained
   optimistically.
3. **Termux:API is a set of shell commands.** They can hang, be missing,
   return malformed JSON, or be denied a permission. Therefore: one choke
   point (`CommandRunner`), a collector that can never raise, and failures
   recorded as data rather than swallowed.
4. **Nothing may need a compiler.** Termux users should not have to build a
   Rust extension. Therefore: zero runtime dependencies, enforced by CI.

## Module map

| Module | Responsibility |
| --- | --- |
| `models.py` | Event envelope, batch, acknowledgement; hand-written validators mirroring the JSON Schemas |
| `config.py` | TOML loading, type-checked merge, validation, the insecure-endpoint gate |
| `database.py` | The SQLite outbox: migrations, dedupe, transactional batching, retention |
| `redaction.py` | Salted pseudonyms, location generalisation, header/token redaction |
| `collectors/base.py` | Plugin API and the mockable Termux command adapter |
| `collectors/*.py` | One source each |
| `uploader.py` | HTTPS client, backoff, acknowledgement handling |
| `heartbeat.py` | Liveness and capability reporting |
| `scheduler.py` | The loop: intervals, heartbeats, uploads, signals |
| `cli.py` | Commands and the example-config template |

## The event envelope

Every observation is stored as:

```json
{
  "event_id": "6f1a7a1e-...",
  "device_id": "device-test-001",
  "source": "battery",
  "event_type": "battery_sample",
  "event_time_utc": "2026-07-31T23:45:00Z",
  "collected_time_utc": "2026-07-31T23:45:03Z",
  "timezone_offset_minutes": -240,
  "schema_version": 1,
  "quality_flags": [],
  "payload": {}
}
```

Three decisions are load-bearing here.

**UTC plus offset, never local time alone.** `event_time_utc` is
unambiguous; `timezone_offset_minutes` lets a reader reconstruct wall-clock
time later. Storing only local time would make daylight-saving transitions
and travel unreconstructable.

**`event_time_utc` and `collected_time_utc` are separate.** They differ for
backfilled data (a call log entry from this morning read at midnight) and for
anything observed after an outage. Collapsing them would destroy the ability
to detect late arrival.

**`event_id` is usually deterministic.** It is a UUIDv5 over
`(device_id, source, event_type, event_time_utc, payload)`. The same
observation therefore produces the same id no matter how many times it is
collected or uploaded, which is what makes deduplication work identically on
the phone and on the server. Collectors that genuinely emit distinct events
at one timestamp pass a `dedupe_key`.

### Quality flags

Flags annotate trust; they never change what `payload` means.

| Flag | Meaning |
| --- | --- |
| `collector_error` | The collector failed; `payload.error` says how |
| `command_unavailable` | A required `termux-*` command is not installed |
| `permission_denied` | Android refused the permission |
| `mocked` | Produced from fixtures, not a real device |
| `experimental` | The source is not yet trustworthy |
| `late_arrival` | Collected substantially after it occurred |
| `redacted` | Sensitive fields were dropped or pseudonymised |
| `coarse` | Precision was deliberately reduced |
| `partial` | Some expected fields were missing |
| `clock_uncertain` | The source timestamp had no timezone |

A collector that fails emits a `collection_error` event rather than nothing.
That is deliberate: a silent gap and a broken collector must not look the
same downstream.

## Payload shapes

| Source | `event_type` | Key payload fields |
| --- | --- | --- |
| `battery` | `battery_sample` | `percentage`, `status`, `plugged`, `charging`, `health`, `temperature_celsius` |
| `wifi` | `wifi_sample` | `connected`, `ssid_pseudonym`, `bssid_pseudonym`, `rssi_dbm`, `frequency_mhz`, `supplicant_state` |
| `location` | `location_sample` | `location_mode`, `location_available`, then mode-dependent: `latitude`/`longitude` (coarse/precise/raw) or `geohash` |
| `location` | `location_disabled` | `location_mode: "disabled"` |
| `sensors` | `sensor_catalogue` | `available_sensors`, `sensor_count` |
| `sensors` | `sensor_summary` | `sensor`, `magnitude_mean/min/max/stddev` -- never a raw sample stream |
| `calls` | `call_record` | `direction`, `duration_seconds`, `counterparty_pseudonym` |
| `sms` | `sms_record` | `direction`, `read`, `body_length`, `counterparty_pseudonym`, `thread_pseudonym` |
| `heartbeat` | `collector_heartbeat` | versions, queue depth, per-collector availability and error counts |
| `tasker` | user-defined | whatever `ingest-event` was given |
| any | `collection_error` | `error`, `error_type` |
| any | `collection_unavailable` | `reason`, `missing_commands` |

Wi-Fi never emits `ip`, `mac_address` or `network_id`: device-identifying and
useless for behaviour.

## The outbox

Four tables plus `schema_migrations`:

- **`events`** -- the raw store. `event_id` is the primary key, so inserts
  use `INSERT OR IGNORE` and a replay is a no-op. Carries `uploaded`,
  `upload_attempts`, `last_upload_error`, `acknowledged_at_utc`,
  `dead_letter` and `batch_id`.
- **`sync_batches`** -- one row per upload attempt group, with status,
  attempt count and per-batch counts.
- **`collector_status`** -- availability, success and error counts, last
  error per source. Feeds `doctor` and heartbeats.
- **`runtime_state`** -- small key/value state such as the last successful
  upload time.

WAL mode plus a 30-second busy timeout means `ingest-event` from Tasker can
write while the daemon is looping.

### Why claim/commit is transactional

`claim_batch` selects pending events, creates the batch row and stamps
`batch_id` onto those events **inside one `BEGIN IMMEDIATE`**. A crash
between any two of those steps would otherwise leave a batch referencing
events that were never claimed, or events claimed by a batch that does not
exist. On completion, only the event ids the server actually acknowledged are
marked uploaded; everything else has its `batch_id` cleared and returns to
the pending pool.

### Dead-lettering, not dropping

After `dead_letter_after_attempts` failures an event stops being retried, but
it is **still stored**. `status` reports the count. Nothing is ever discarded
because it was inconvenient.

### Retention

Off by default. When enabled, only events that are both `uploaded` and
acknowledged before the cutoff are deleted. Pending, dead-lettered and
unacknowledged events are never touched.

## Upload protocol

`POST /api/v1/events/batch` with:

```
Authorization: Bearer <device token>
X-Device-ID: <pseudonymous device id>
X-Batch-ID: <unique batch id, stable across retries>
X-Protocol-Version: 1
X-Collector-Version: 0.1.0
Content-Type: application/json
Content-Encoding: gzip        (when the body exceeds the threshold)
```

The response acknowledges **per event**, distinguishing `stored` from
`duplicate`. Both count as success -- that equivalence is what makes retries
safe. Rejected events are named with a reason and remain queued until the
attempt limit.

Retries reuse the same `batch_id`, so the server can recognise a replay.
Because event ids are stable, the same events arriving under a *different*
batch id also deduplicate correctly. Both paths are tested.

Backoff is exponential with symmetric jitter, capped by config. Jitter
matters on a phone: without it, every device that queued during an outage
retries in lockstep the moment it ends.

## Backward-compatibility policy

- `protocol_version` changes only for a **breaking** wire change. Both
  repositories must be released together, and the cross-repository workflow
  will fail until they are.
- `schema_version` is stored on every event and never rewritten. Old events
  keep their original version forever; readers branch on it.
- Adding an optional field to a `payload` is **not** breaking. Payload shapes
  are source-specific and versioned by `schema_version`.
- Adding a `quality_flags` value **is** schema-visible (the enum is closed)
  and requires a `schema_version` bump.
- Renaming a `source` or an `event_type` is breaking and effectively never
  acceptable: it orphans historical data.
- The server must accept any `schema_version` it has ever accepted.

## What is deliberately absent

- No inbound server on the phone, in any mode.
- No remote command execution, ever.
- No plaintext transport outside a triple-locked test mode.
- No third-party runtime dependency.
- No automatic deletion of raw data.
