# Research and decisions

Findings from surveying current upstream documentation and repositories
before writing any code, and the decisions that followed. Dated **July 2026**;
re-check before assuming any of it is still true.

The Home Assistant side of the research lives in
[`android-timeline-home-assistant/docs/research-and-decisions.md`](https://github.com/resace3/android-timeline-home-assistant/blob/main/docs/research-and-decisions.md).

---

## 1. Termux distribution and app signing

**Found.** Termux, Termux:API, Termux:Boot and Termux:Tasker are separate
Android applications that communicate with one another. Android only permits
that when they share a signing key. F-Droid builds and GitHub-release builds
use **different** keys, so a mixed installation silently fails to work --
`termux-*` commands hang or return nothing, with no error. The Google Play
build is deprecated and additionally restricted.

**Decision.** Documentation instructs users to install all Termux apps from
one source and never mix, and explains the failure symptom explicitly rather
than just saying "install Termux". `doctor` surfaces the resulting state as
`unavailable` commands so the user has a diagnostic path.

## 2. Termux runtime constraints

**Found.** Termux ships Python but building native wheels requires `clang`
and often fails or takes a very long time on a phone. Popular pure-Python
choices are not always dependency-free: `jsonschema` pulls `rpds-py`, a Rust
extension.

**Decision.** **Zero third-party runtime dependencies.** The collector uses
only `sqlite3`, `urllib.request`, `ssl`, `gzip`, `json`, `hmac`,
`hashlib`, `uuid`, `tomllib` and `argparse`. JSON Schema validation is
implemented by hand in `models.py` for runtime use, and the real
`jsonschema` library is a **dev/test dependency** that CI uses to prove the
two agree (`tests/contract/test_schemas.py`). A dedicated CI job installs the
package without extras and fails if the runtime dependency set is non-empty.

Consequence: `tomllib` sets the floor at Python 3.11, which Termux exceeds.

## 3. Official Termux Docker image in GitHub Actions

**Found.** `termux/termux-docker` is the official image
(`ENTRYPOINT ["/entrypoint.sh"]`, `CMD ["login"]`, drops to uid/gid 1000,
`PREFIX=/data/data/com.termux/files/usr`). Two documented problems affect CI:

- GitHub Actions rewrites the entrypoint of `jobs.<id>.container` to
  `tail -f /dev/null`, bypassing `/entrypoint.sh` entirely
  ([actions/runner#1964](https://github.com/actions/runner/issues/1964),
  [community discussion #24944](https://github.com/orgs/community/discussions/24944)).
- `pkg install` inside the container on a GitHub runner commonly fails with
  *"None of the mirrors are accessible"*, caused by DNS resolution failing in
  the container network. Reported as
  [termux/termux-docker#51](https://github.com/termux/termux-docker/issues/51)
  and **closed as wontfix**. `--user system` does not help.
- Root is deliberately unsupported: the Termux package manager is not
  designed to run as uid 0, and `/entrypoint_root.sh` exists only as an
  escape hatch.

**Decision.** Keep the official image -- substituting Debian and calling it
Termux would be dishonest. Work with the constraints instead of around them:

- Drive `docker run` directly rather than using a job container, so the real
  entrypoint executes.
- Run as `--user 1000:1000` and *assert* we are not root.
- Treat package installation as best-effort, capture the exact failure into
  the job summary, and skip the Python-dependent smoke test when it fails.
- Mark the job `continue-on-error: true` and label it **informational** in
  the workflow header, the README and `testing-limitations.md`.
- Add a check that the installer **refuses** to run on `debian:12-slim`, so
  the "is this really Termux?" question is answered by a test rather than by
  assertion.

Layers 1 and 3 are the primary validation.

## 4. Android emulator on GitHub-hosted runners

**Found.** `reactivecircus/android-emulator-runner` (v2.38.0 at time of
writing) is the maintained option. Hardware acceleration on `ubuntu-latest`
requires an explicit udev rule to make `/dev/kvm` world-accessible; Linux
runners are substantially faster and cheaper than macOS for this. AVD
snapshots can be cached with `actions/cache`. The emulator reaches the host
loopback interface via the special address `10.0.2.2`; `adb reverse` is the
alternative.

Also found: installing the Termux **app** inside an emulator is the fragile
part. The distribution channels use incompatible signatures, first run
requires an interactive bootstrap, and there is no supported non-interactive
way to execute a command inside Termux from `adb` without first editing
`termux.properties` -- which requires Termux to have run at least once.

**Decision.** Use the maintained action with KVM enabled and AVD caching.
Split the workflow into phases that each report their own verdict, so the job
summary states precisely what was proven. Use `10.0.2.2` as the primary
bridge with `adb reverse` as a fallback. **Do not** attempt Termux app
installation in CI: a flaky job that appears to test Termux-on-Android is
worse than an honest skip. Phase 9 records the skip and its reason.

What the emulator *does* validate is still worth having: the outbox schema
under the device's own SQLite build, the network path from device to server,
authenticated upload, idempotent replay and token rejection.

## 5. Protocol source of truth

**Found.** Three options were considered: schemas hosted here, hosted in the
Home Assistant repository, or duplicated with a checksum test. A third
repository was ruled out by the brief.

**Decision.** The **Home Assistant repository is the source of truth**. This
repository vendors a byte-identical copy under `schemas/`, guarded by
`schemas/PROTOCOL_SHA256SUMS`, which `tests/contract/test_schemas.py` verifies
on every run without needing network access. The cross-repository end-to-end
workflow checks out both repositories and compares the files directly, so
genuine drift is caught even if both checksum files were updated.

Rationale: the server is the party that must accept old data forever, so it
should own the contract. Vendoring keeps this repository's CI hermetic and
offline.

## 6. Event identity and idempotency

**Found.** Retry-safety needs stable identity. Batch-level idempotency keys
alone are insufficient: after a partial acknowledgement the same events must
be re-sent under a *different* batch id.

**Decision.** `event_id` is a UUIDv5 over
`(device_id, source, event_type, event_time_utc, canonical_payload)` in a
fixed namespace. Deduplication then works identically on both sides, and both
replay paths (same batch id, and same events in a new batch) are covered by
tests. Collectors that legitimately emit several events at one timestamp pass
a `dedupe_key` that enters the payload.

## 7. Time representation

**Found.** Local timestamps without an offset cannot be reconstructed across
daylight-saving changes or travel. Termux's `termux-call-log` emits a local,
human-formatted date with no timezone at all.

**Decision.** Store `event_time_utc` (UTC, `Z`-suffixed) plus
`timezone_offset_minutes`, and keep `collected_time_utc` separate so late
arrival is measurable. Where a source timestamp has no offset -- as with the
call log -- parse it as UTC and attach the `clock_uncertain` quality flag
rather than inventing a timezone the reader cannot audit.

## 8. Sensitive data handling

**Found.** Digital-phenotyping collectors routinely capture SMS bodies,
contact names, precise coordinates and notification text. Most of that is
about people who did not consent.

**Decision.** Metadata-only by default, everything sensitive off by default,
salted HMAC pseudonyms for identifiers, and a location mode ladder where
`raw` is the only setting that emits full precision. The call and SMS
collectors **refuse to run** without a salt rather than degrading to an
unsalted hash. The Tasker recipes explicitly warn against sending
notification text or Bluetooth device names.

## 9. Testing the collection layer without Android

**Found.** Every Termux:API interaction is a subprocess call. That is the
only Android-specific surface in the collection path.

**Decision.** Abstract it behind `CommandRunner` with two implementations:
`TermuxCommandRunner` (real subprocess) and `MockCommandRunner` (fixture
files, selected by `$ANDROID_TIMELINE_TERMUX_MOCK_DIR`). Events produced
through the mock always carry the `mocked` quality flag, so synthetic data
cannot be mistaken for real observations even if it reaches a database. This
single seam is what makes Layer 1 cover most of the code.

## 10. GitHub Actions hygiene

**Found.** Current guidance: pin actions to immutable versions, grant minimum
`GITHUB_TOKEN` permissions, avoid running untrusted PR code with secrets, and
avoid `persist-credentials` on checkout. CodeQL now supports an `actions`
language for analysing workflows themselves.

**Decision.** All actions pinned to exact released versions. Top-level
`permissions: contents: read`, widened per job only where required
(`security-events: write` for CodeQL, `contents: write`/`id-token: write`/
`attestations: write` for releases). `persist-credentials: false` everywhere.
No secrets are used by any workflow in this repository -- there is nothing
for a fork PR to steal. CodeQL runs over both `python` and `actions`.

## 11. Release strategy

**Decision.** Tag-triggered releases re-run the **full** test suite against
the tagged tree rather than trusting an earlier green run, verify the tag
matches `pyproject.toml`, publish an sdist, a wheel and an archive of the
installation scripts and schemas, emit SHA-256 checksums and generate build
provenance attestations via `actions/attest-build-provenance`.

**PyPI publishing is deliberately not configured.** Claiming a global package
name for software that has never run on a physical phone is not appropriate,
and the documented installation path is a `git clone` inside Termux anyway.

---

## Open questions

- Can Termux be installed and driven non-interactively in an emulator
  reliably enough to be worth the flakiness? Revisit if upstream adds a
  supported headless bootstrap.
- Is a small helper APK a better source for screen/app/notification events
  than the Tasker bridge? It would need its own permission story.
- Should the salt move to Android's keystore? Termux has no straightforward
  access today.
