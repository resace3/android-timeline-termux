# Security policy

## Status

This is experimental personal software that has never run on a physical
Android device. Do not deploy it anywhere that matters without reading
[docs/security.md](docs/security.md) and
[docs/testing-limitations.md](docs/testing-limitations.md) first.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting:
**Security -> Report a vulnerability** on
<https://github.com/resace3/android-timeline-termux>.

Please do **not** open a public issue for a vulnerability, and please do not
include real personal data, real tokens or a real Home Assistant URL in the
report -- a synthetic reproduction is always enough.

There is no service-level commitment on this repository. Expect a best-effort
response.

## Supported versions

Only the latest tagged release and `main` receive fixes.

## Threat model in one page

The collector runs on a device the user controls, writes to storage the user
controls, and talks to a server the user controls. The design assumptions are:

| Assumption | Consequence |
| --- | --- |
| The phone may be lost or stolen | Secrets are `0600` files, never in the config; the queue holds pseudonymised data, not raw identifiers |
| The network is hostile | TLS verification is mandatory outside an explicitly flagged test mode; tokens travel only in the `Authorization` header, never in a URL |
| The server may be unreachable for days | The queue is append-only and durable; nothing is dropped, and retries are idempotent |
| Logs get pasted into bug reports | No command prints a token; `doctor` shows a length and a digest prefix only |
| The user may misconfigure things | Insecure transport needs three independent locks; sensitive collectors are off by default |

## Explicitly out of scope

- Protecting data from someone with root on the phone.
- Protecting data from Android itself, or from other apps with the same
  permissions the user granted Termux.
- Defending against a malicious Home Assistant app -- the user runs both
  sides.

## Things this software will never do

These are enforced by tests, not just by policy:

- Accept an inbound connection, or provide any form of remote shell.
- Send an authorization header anywhere except the configured server.
- Store SMS bodies or contact names without explicit opt-in.
- Store raw precise coordinates without explicit opt-in.
- Write a token or salt to a log, a heartbeat, a Home Assistant entity or a
  CI artifact.
- Delete a raw event that has not been acknowledged.
