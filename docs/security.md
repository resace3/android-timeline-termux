# Security model

Deny by default. Every control below has a test; where a test exists it is
named.

## 1. No inbound surface

The phone is only ever an HTTPS **client**. There is no listener, no port, no
webhook receiver and no remote command path. `ingest-event` is a local CLI
command; reaching it already requires code execution on the device.

This is the single most important property. It means a compromised server can
misbehave with the data it was sent, but cannot reach into the phone.

## 2. Transport

TLS certificate verification uses the system trust store and is on by
default. Turning it off, or using plaintext HTTP, requires **three
independent locks**:

1. `server.allow_insecure_test_endpoint = true` in config, **and**
2. the environment variable `ANDROID_TIMELINE_INSECURE_TEST_MODE` set to the
   exact phrase `yes-i-am-running-an-automated-test`, **and**
3. a hostname in `{localhost, 127.0.0.1, ::1, 10.0.2.2}`.

Any one missing and `load_config` raises, listing what is wrong. A stray
config edit, a copied example or an inherited environment variable cannot
individually open the door.

*Tests:* `tests/unit/test_config.py::TestInsecureEndpointGate` (five cases).

## 3. Credentials

| | |
| --- | --- |
| Where the token lives | `~/.config/android-timeline/token`, mode `600`, or `$ANDROID_TIMELINE_TOKEN` |
| Where the salt lives | `~/.config/android-timeline/salt`, mode `600`, or `$ANDROID_TIMELINE_SALT` |
| Where they never live | the config file, any log, any heartbeat, any event payload, any CI artifact, any URL |

`redact_token()` renders a token as `<redacted len=N sha256:xxxxxxxx>`. That
is what `doctor` prints, so its output can be pasted into a bug report
safely. `redact_headers()` replaces `Authorization`, `Cookie` and friends
before any debug logging.

Tokens travel in the `Authorization` header only. `_safe_url()` strips query
strings before logging, and no code path places a credential in a URL.

*Tests:* `test_redaction.py::TestTokenRedaction`, `::TestHeaderRedaction`,
`test_uploader.py::TestLoggingRedaction`,
`test_cli.py::TestDoctor::test_never_prints_the_token`,
`test_http_roundtrip.py::TestNoSecretsOnTheWire`.

## 4. Data minimisation

Defaults, all enforced in `config.DEFAULT_COLLECTORS` and `PrivacyConfig`:

| Setting | Default |
| --- | --- |
| `collectors.calls.enabled` | `false` |
| `collectors.sms.enabled` | `false` |
| `collectors.location.enabled` | `false` |
| `privacy.location_mode` | `disabled` |
| `privacy.store_message_bodies` | `false` |
| `privacy.store_contact_names` | `false` |
| `retention.enabled` | `false` |

When call or SMS collection *is* enabled:

- Phone numbers become `HMAC-SHA256(salt, normalised_number)` truncated to
  16 hex characters, prefixed `p_`. Different devices use different salts, so
  the same number is unlinkable across exports.
- Numbers are normalised before hashing, so `+1-555-0100` and `+1 (555) 0100`
  produce the same pseudonym.
- Message bodies are replaced by `body_length`.
- Contact names are dropped entirely.
- The collectors **refuse to run at all** without a salt, rather than falling
  back to an unsalted hash.

Location generalisation:

| Mode | Emits |
| --- | --- |
| `disabled` (default) | `location_available: false`, no coordinates |
| `coarse` | coordinates rounded to `coarse_decimal_places` (default 2, ~1 km) |
| `geohash` | a geohash only; no coordinates at all |
| `precise` | coordinates rounded to 4 dp (~11 m) |
| `raw` | full precision -- explicit opt-in only |

`transform_location()` drops `latitude`/`longitude` out of the `extra`
mapping too, so a collector cannot smuggle precision past the mode.

Wi-Fi never emits `ip`, `mac_address` or `network_id`.

*Tests:* `test_redaction.py::TestPseudonymize`, `::TestTransformLocation`,
`test_collectors.py::TestSensitiveCollectors`, `::TestWifi`.

## 5. Input handling

The collector treats every `termux-*` response as untrusted:

- Fixed `argv` lists, never a shell string, so command injection has no path.
- Timeouts on every invocation.
- Non-zero exits, malformed JSON and unexpected types become
  `collection_error` events; the daemon never dies.
- Payload strings that look like SQL are stored as ordinary values --
  everything is a bound parameter.

Envelope validation rejects: unknown top-level fields, non-UTC timestamps,
out-of-range timezone offsets, booleans where integers are expected,
path-traversal-shaped source names, and oversized flag lists.

*Tests:* `test_models.py::TestEventValidation`,
`test_database.py::TestExport::test_sql_injection_in_filter_is_inert`,
`test_collectors.py` failure cases, `test_schemas.py::TestValidatorsAgree`.

## 6. Supply chain

- **Zero third-party runtime dependencies**, asserted by a dedicated CI job
  that installs the package without extras and inspects its metadata.
- Dev tooling is audited with `pip-audit --strict`.
- `bandit` runs as an independent second opinion alongside ruff's
  flake8-bandit rules.
- CodeQL analyses both `python` and `actions`.
- Dependabot covers GitHub Actions, pip and Docker.
- GitHub secret scanning **with push protection** is enabled, plus a CI job
  that greps full history for credential-shaped strings.

## 7. CI hygiene

- Workflows declare `permissions: contents: read` at the top level and widen
  only where a job needs it.
- `persist-credentials: false` on every checkout.
- No workflow has access to a real Home Assistant instance, URL or token.
  Nothing in this repository can reach one.
- Every fixture uses reserved identifiers (`example.invalid`, `+1-555-01xx`,
  `device-test-001`). A CI job fails the build if a non-fictional phone
  number appears under `tests/`.
- Emulator diagnostics are uploaded **only on failure**, and are passed
  through a `sed` redaction filter first.

## 8. File permissions

The installer creates `~/.config/android-timeline` and
`~/.local/share/android-timeline` as `700`, and `config.toml`, `token` and
`salt` as `600`. `cli._secure()` re-applies `600` when it writes a config.
Android's per-app sandbox already isolates Termux's home directory; this is
defence in depth against anything sharing the Termux UID.

## 9. Known weaknesses

Stated plainly rather than omitted:

- **The salt lives next to the data it protects.** Anyone who can read the
  outbox can usually read the salt. Pseudonymisation protects exports and
  server-side storage, not an attacker with the phone unlocked.
- **The token is a bearer token in a file.** No hardware-backed keystore is
  used; Termux has no straightforward access to one. Rotation and revocation
  are the mitigation.
- **No certificate pinning.** The system trust store is used. For a personal
  Home Assistant with a self-signed certificate, add the CA to Android's
  trust store rather than disabling verification.
- **Nothing here has run on a real phone.** All of the above is verified in
  CI only. See [testing-limitations.md](testing-limitations.md).
