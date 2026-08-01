# Permissions

Each collector needs a specific Android permission, granted to the
**Termux:API** app, not to the collector. Grant only what you actually enable.

## Mapping

| Collector | Termux:API command | Android permission | Default | Notes |
| --- | --- | --- | --- | --- |
| `battery` | `termux-battery-status` | none | **enabled** | No runtime permission needed |
| `wifi` | `termux-wifi-connectioninfo` | `ACCESS_FINE_LOCATION` | **enabled** | Android treats SSID/BSSID as location data |
| `location` | `termux-location` | `ACCESS_FINE_LOCATION` (+ `ACCESS_BACKGROUND_LOCATION`) | disabled | Background access needs a separate, harder grant |
| `sensors` | `termux-sensor` | none (usually) | disabled | `BODY_SENSORS` for some heart-rate sensors |
| `calls` | `termux-call-log` | `READ_CALL_LOG` | disabled | Metadata only |
| `sms` | `termux-sms-list` | `READ_SMS` | disabled | Metadata only unless you opt in to bodies |

Everything except battery is off by default, or needs a permission you must
grant deliberately.

## Granting

**Settings -> Apps -> Termux:API -> Permissions**

Grant them to **Termux:API**, not Termux. The `termux-*` commands are thin
clients that ask Termux:API to do the work, so the permission has to sit
there.

### Background location is a separate step

Android splits location into foreground and background. Granting "while using
the app" is not enough for a collector that runs in the background:

**Settings -> Apps -> Termux:API -> Permissions -> Location -> Allow all the
time**

Android 11+ will not let an app request this directly; the user must choose
it from the settings screen. Without it, `termux-location` returns nothing
whenever the screen is off -- which is most of the time.

### Call log and SMS

These are "restricted" permissions. Android may hide the toggle, and Google
Play policy restricts which apps may request them at all. If the toggle is
missing on your device, the collector will report the command as unavailable
via a `collection_unavailable` event and `doctor` will show it.

**Consider whether you should.** Call and SMS records contain the other
person too, and they did not consent. The metadata-only defaults exist for
that reason. Message bodies and contact names require a second, explicit
opt-in in config on top of enabling the collector.

## What is NOT requested

- No camera, microphone or contacts access.
- No notification listener (a special access grant, not a normal permission).
- No accessibility service.
- No device admin.
- No `QUERY_ALL_PACKAGES`.

If a future collector needs any of these it will be opt-in, documented here
first, and disabled by default.

## Battery optimisation

Not a permission, but the thing most likely to break collection:

**Settings -> Apps -> Termux -> Battery -> Unrestricted**

Manufacturer skins add extra killers with their own screens. See
<https://dontkillmyapp.com> for per-vendor instructions. Symptoms are gaps in
the data that the coverage metrics will show but the collector cannot
prevent.

## Checking what you actually have

```bash
android-timeline --json doctor
```

The `termux_api_commands` check lists every collector with its availability
(`supported`, `experimental`, `mocked`, `unavailable`) and any missing
commands. `unavailable` on an enabled collector means either Termux:API is
not installed, the package is missing (`pkg install termux-api`), the apps
came from different signing sources, or the permission was denied.

## How missing permissions appear in the data

A denied permission or missing command does not produce silence. The
collector emits:

```json
{
  "source": "location",
  "event_type": "collection_unavailable",
  "quality_flags": ["command_unavailable"],
  "payload": {
    "reason": "required command(s) unavailable",
    "missing_commands": ["termux-location"]
  }
}
```

so the server can distinguish "you were not moving" from "we could not tell".
That distinction is the whole point of the coverage metrics on the server
side.
