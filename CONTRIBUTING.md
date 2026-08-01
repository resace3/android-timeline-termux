# Contributing

Thanks for looking. This is a personal, experimental project, so the bar is
"honest and tested" rather than "feature complete".

## Ground rules

1. **Never commit real data.** No real phone numbers, coordinates, SSIDs,
   device identifiers, Home Assistant URLs or tokens -- not in code, tests,
   fixtures, issues, screenshots or commit messages. CI fails the build if it
   finds credential-shaped strings. Use the reserved ranges:
   `device-test-001`, `+1-555-0100`, `example-network`,
   `place-home-synthetic`, `example.invalid`.
2. **Never claim validation that did not happen.** If a change is only tested
   on Linux, say so. The `Testing status` table in the README and
   `docs/testing-limitations.md` must stay accurate; a PR that makes them
   wrong will not be merged.
3. **The runtime stays dependency-free.** Termux users must never need a
   compiler. CI enforces an empty runtime dependency set. Dev-only tools go
   in the `dev` extra.
4. **Sensitive defaults stay off.** Any change that makes a privacy-relevant
   default more permissive needs an explicit rationale in the PR.

## Setup

```bash
python -m pip install -e ".[dev]"
```

Requires Python 3.11 or newer (`tomllib`, `StrEnum`, dataclass slots).

## Before you push

```bash
ruff format .
ruff check .
mypy
pytest
```

Run the collectors without Termux by pointing them at the fixtures:

```bash
export ANDROID_TIMELINE_TERMUX_MOCK_DIR=tests/fixtures/termux
export ANDROID_TIMELINE_TOKEN=synthetic-dev-token
export ANDROID_TIMELINE_SALT=synthetic-dev-salt
android-timeline --json collect-once
```

## Adding a collector

1. Subclass `Collector` in `android_timeline/collectors/`. Pick a `source`
   name and never change it -- it is stored on every event forever.
2. Declare `required_commands` so `probe()` and `doctor` can report
   availability honestly.
3. Register it in `collectors/__init__.py` **and** in
   `config.DEFAULT_COLLECTORS`. A test asserts the two agree.
4. Default to disabled if the source is sensitive.
5. Add a fixture under `tests/fixtures/termux/` and tests covering: the happy
   path, a malformed response, a non-zero exit and a missing command.
6. Document the payload shape in `docs/architecture.md`.

Collectors must never raise. A failure becomes a `collection_error` quality
event so the gap is visible in the data instead of silently absent.

## Changing the protocol

The schemas in `schemas/` are vendored from
[`android-timeline-home-assistant`](https://github.com/resace3/android-timeline-home-assistant),
which is the source of truth. A protocol change means:

1. Change it there first.
2. Copy the files here byte-for-byte.
3. Regenerate `schemas/PROTOCOL_SHA256SUMS`
   (`sha256sum schemas/*.schema.json`).
4. Bump `PROTOCOL_VERSION` in `android_timeline/__init__.py` if the change is
   breaking, and say so in the PR.
5. Expect the cross-repository end-to-end workflow to fail until both sides
   are released together.

## Pull requests

- Branch from `main`; direct pushes to `main` are blocked.
- Keep changes focused. A refactor and a behaviour change in one PR is hard
  to review.
- All required checks must pass. Do not skip, disable or `continue-on-error`
  a failing test to get a merge -- fix the cause or explain why the test was
  wrong.
- The Termux container and Android emulator workflows are non-blocking by
  design. If you make one of them reliably green, say so and we can promote
  it.

## Commit messages

Imperative mood, one logical change. Reference an issue where one exists.
