# Testing limitations

This document exists so nobody -- including future me -- mistakes a green
badge for a working phone.

## The one-line summary

**No physical Android device has ever run this software.** Every claim below
is about CI.

## The three layers, and what each actually proves

### Layer 1 -- Ubuntu, Python 3.11/3.12/3.13 (`ci.yml`)

**Status: required. Must pass before merge.**

Proven:

- Event envelope construction, validation, deterministic ids, UTC handling
  and timezone-offset retention.
- The SQLite outbox: migrations, primary-key deduplication, transactional
  batch claim/commit, partial acknowledgement, retry counters, dead-lettering
  and the retention switch (off by default).
- Upload behaviour: header construction, gzip, exponential backoff with
  jitter, retryable vs non-retryable errors, batch replay, and preservation
  of the queue across a simulated outage -- against a **real local HTTP
  server**, not a mock.
- Configuration validation, including the three-lock insecure-endpoint gate.
- Redaction: token fingerprints, salted pseudonyms, location generalisation.
- Every collector against fixture data, including malformed output, non-zero
  exits and missing commands.
- Conformance of the published JSON Schemas with the stdlib validators, and
  the vendored protocol checksums.
- That the runtime has **zero** third-party dependencies.

Not proven: anything Android-specific. Every `termux-*` command is a
fixture here.

### Layer 2 -- official `termux/termux-docker` (`termux-container.yml`)

**Status: informational. Non-blocking by design.**

Proven (when the image starts, which it reliably does):

- The container runs the documented `/entrypoint.sh` as **uid 1000**, not
  root, with `PREFIX=/data/data/com.termux/files/usr` and the matching
  `HOME`.
- The genuine Termux filesystem layout exists at those paths.
- `scripts/install-termux.sh` **accepts** that environment.
- `scripts/install-termux.sh` **refuses** to run on a plain `debian:12-slim`
  container. This is the check that stops us quietly passing an ordinary
  Linux image off as a Termux test.

Attempted, usually fails:

- `pkg install python`. On GitHub-hosted runners this typically dies with
  *"None of the mirrors are accessible"*. The cause is DNS resolution inside
  the container network, reported upstream as
  [termux/termux-docker#51](https://github.com/termux/termux-docker/issues/51)
  and **closed as wontfix**. The workflow captures the exact failure into the
  job summary rather than hiding it, and the collector smoke test that
  depends on Python is skipped when this happens.

Why this job is not required: its outcome depends on upstream mirror and DNS
behaviour we do not control. Making it a required check would mean blocking
merges on someone else's infrastructure. It runs on every relevant PR, on
`main`, and weekly on a schedule, so a regression is still visible.

**Explicitly not proven:** Termux:API commands, battery, location, sensors,
Termux:Boot, Android permissions, Binder, or anything else that needs the
Android framework. A Linux container running the Termux rootfs is not
Android.

### Layer 3 -- Android emulator, API 34 x86_64 (`android-emulator.yml`)

**Status: non-blocking.**

Proven, phase by phase (each phase reports its own verdict in the job
summary):

1. The emulator boots and `sys.boot_completed` becomes 1.
2. The collector payload can be pushed to a real Android filesystem.
3. The directory structure the installer creates works on Android.
4. The outbox schema is accepted by **the device's own SQLite build**.
5. The device can reach the runner over HTTP via the emulator host bridge
   (`10.0.2.2`), with `adb reverse` as a fallback.
6. An authenticated batch upload from the device is accepted.
7. Replaying the identical batch is recognised as duplicates.
8. An invalid bearer token is rejected with HTTP 401.

Skipped, and honestly labelled as such:

9. **Installing the Termux app itself.** Termux is distributed through
   F-Droid and GitHub releases with mutually incompatible signatures, and
   the first run requires interactive bootstrap setup. Automating that in
   CI produces a flaky job that proves less than it appears to, so it is not
   attempted. Running the Python collector *inside the Termux app on
   Android* is therefore **not** covered by any automated test.

## What requires a real phone

None of these can be established by any workflow in this repository:

| Area | Why CI cannot cover it |
| --- | --- |
| GPS quality | Emulated location is a fixed injected value |
| Accelerometer / gyroscope | Emulated sensors produce synthetic constants |
| Health Connect | Not present or populated on emulator images |
| Notification listener access | Requires a user-granted special permission |
| Real SMS and call logs | No carrier, no message history |
| Cellular modem data | No modem |
| Physical Bluetooth devices | No radios |
| Manufacturer battery-killing | Vendor-specific, absent from AOSP images |
| Doze and App Standby over hours | CI jobs are minutes long |
| Termux:Boot after a real reboot | Emulator reboot is not a device reboot |
| Battery impact | Meaningless without real hardware |
| 24-hour collection continuity | Same |

These are tracked in the **Real Android device acceptance testing** issue,
which must not be closed without evidence gathered on an actual phone.

## What "green" means

A green CI badge on this repository means: *the platform-independent logic is
correct, the protocol is conformant, and the code runs on Android's SQLite
and can talk to a server from an emulator.*

It does **not** mean the collector works on your phone.
