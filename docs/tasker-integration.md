# Tasker integration (optional)

Termux polls. Some things are events, not states -- screen unlocks, app
switches, Bluetooth connections -- and polling for them is both inaccurate
and expensive. [Tasker](https://tasker.joaoapps.com/) can observe those
directly and hand them to the collector.

**Tasker is entirely optional.** Nothing in CI requires it, and the collector
works without it.

## How it works

Tasker runs one command:

```bash
android-timeline ingest-event --source tasker --type screen_on --payload '{}'
```

That writes a normal event into the same SQLite outbox, so it is
deduplicated, batched, retried and uploaded exactly like a polled sample.
There is no separate path and no network listener.

## Setup

1. Install **Tasker** and the **Termux:Tasker** plugin (from the same source
   as Termux -- see [installation.md](installation.md)).
2. Allow external apps to run Termux commands:

   ```bash
   mkdir -p ~/.termux
   echo 'allow-external-apps = true' >> ~/.termux/termux.properties
   termux-reload-settings
   ```

   > This lets *any* app with the Termux:Tasker permission run scripts in
   > `~/.termux/tasker/`. Only scripts in that directory, and only if you put
   > them there. Consider whether you want it.

3. Create the bridge script:

   ```bash
   mkdir -p ~/.termux/tasker
   cat > ~/.termux/tasker/android-timeline-event <<'EOF'
   #!/data/data/com.termux/files/usr/bin/sh
   # $1 = event type, $2 = JSON payload (optional)
   exec "$HOME/.local/share/android-timeline/venv/bin/android-timeline" \
       ingest-event --source tasker --type "$1" --payload "${2:-\{\}}"
   EOF
   chmod 700 ~/.termux/tasker/android-timeline-event
   ```

4. In Tasker: **Task -> Add Action -> Plugin -> Termux:Tasker**
   - Executable: `android-timeline-event`
   - Arguments: `screen_on` (and optionally a JSON payload)
   - **Uncheck** "Terminal session" so it runs in the background.

## Recipes

`event_type` must match `^[a-z][a-z0-9_]{0,63}$`.

### Screen on / off

| Profile | Event | Task arguments |
| --- | --- | --- |
| Event -> Display -> Display On | `screen_on` | `screen_on` |
| Event -> Display -> Display Off | `screen_off` | `screen_off` |

### Device unlocked

Profile: **Event -> Display -> Device Unlocked**

```
android-timeline-event device_unlocked
```

### Foreground app changed

Profile: **Application -> (any)**, with **Invert** off. Pass the package name
rather than the app label -- it is stabler and less identifying:

```
android-timeline-event app_foreground {"package":"%APP"}
```

> `%APP` reveals your app usage in detail. Consider a Tasker variable that
> maps packages to categories (`messaging`, `browser`, `work`) and sending
> the category instead.

### Notification posted

Profile: **Event -> UI -> Notification**

```
android-timeline-event notification_posted {"package":"%evtprm2","category":"other"}
```

> **Do not send `%NTITLE` or `%NTEXT`.** Notification titles and bodies
> contain message content, names and one-time codes. The recipe above sends
> the posting package only. This mirrors the collector's own defaults, where
> message bodies are never stored without an explicit opt-in.

### Bluetooth connected / disconnected

Profile: **Event -> Net -> BT Connected**

```
android-timeline-event bluetooth_connected {"device_class":"audio"}
```

Profile: **Event -> Net -> BT Disconnected**

```
android-timeline-event bluetooth_disconnected {"device_class":"audio"}
```

> Send a class or a Tasker-side pseudonym, not `%btname` -- Bluetooth device
> names identify people ("Sam's AirPods") and places (car models).

## Timestamps

By default the event is timestamped when `ingest-event` runs, which for
Tasker is within a second of the real event. For a replayed or delayed event,
pass the time explicitly:

```bash
android-timeline ingest-event --source tasker --type screen_on \
    --event-time 2026-07-31T23:45:00Z
```

`--event-time` must be RFC 3339 UTC ending in `Z`. The collected time is
recorded separately, so the server can see the lag.

## Deduplication

Event ids are derived from `(device_id, source, event_type, event_time_utc,
payload)`. Two identical Tasker events in the same millisecond collapse into
one -- usually what you want when a profile double-fires. If you need them
distinct, put a counter in the payload.

## Verifying

```bash
android-timeline --json export --source tasker --limit 20
```

## Quality flags

Add flags when the event is uncertain:

```bash
android-timeline ingest-event --source tasker --type screen_on \
    --quality-flag late_arrival
```

Only the known flags are accepted (`collector_error`,
`command_unavailable`, `permission_denied`, `mocked`, `experimental`,
`late_arrival`, `redacted`, `coarse`, `partial`, `clock_uncertain`); anything
else is rejected rather than silently stored.
