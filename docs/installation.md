# Installation

> This has never been performed on a physical device by the author. Expect
> rough edges and please report them.

## 1. Install the Termux apps -- from ONE source

This is the step that breaks most installations, so it comes first.

Termux, Termux:API and Termux:Boot are **separate Android apps** that talk to
each other. Android only permits that when they share a signing key. F-Droid
builds and GitHub-release builds are signed with **different** keys.

**Install all three from the same place. Never mix.**

| Source | Notes |
| --- | --- |
| [F-Droid](https://f-droid.org/packages/com.termux/) | Easiest; updates handled by the F-Droid client |
| [GitHub releases](https://github.com/termux/termux-app/releases) | Newer; you must install all three from GitHub too |

If you previously installed Termux from one source, you must **uninstall it**
before installing from the other -- Android will refuse the update otherwise.

Symptoms of mixed sources: `termux-battery-status` hangs forever or returns
nothing, and no error is printed. `android-timeline doctor` will report the
commands as unavailable.

> The Google Play version of Termux is deprecated and has different
> restrictions. Do not use it.

You need:

- **Termux** -- the terminal.
- **Termux:API** -- the app that provides the `termux-*` commands. You also
  need the package: `pkg install termux-api`.
- **Termux:Boot** -- starts the collector after a reboot. Launch it once
  manually after installing, or it never activates.

## 2. Install the collector

```bash
pkg update
pkg install python git
git clone https://github.com/resace3/android-timeline-termux
cd android-timeline-termux
./scripts/install-termux.sh
```

The installer is idempotent and safe to re-run. It:

- refuses to run outside Termux,
- checks for Python 3.11+,
- creates `~/.config/android-timeline` and
  `~/.local/share/android-timeline` with mode `700`,
- creates a virtualenv and installs the package,
- writes `config.toml` (backing up any existing one rather than clobbering
  it -- `--force` to replace),
- generates a random pseudonymisation salt if one does not exist,
- creates an empty `token` file with mode `600`,
- installs the Termux:Boot launcher.

Useful flags: `--skip-packages` (offline/CI), `--skip-boot`, `--force`.

## 3. Enroll the device

On the Home Assistant side, create a device and copy the token it shows
**once**. Then, on the phone:

```bash
printf '%s' 'PASTE_TOKEN_HERE' > ~/.config/android-timeline/token
chmod 600 ~/.config/android-timeline/token
```

`printf` rather than `echo` avoids a trailing newline, and starting the line
with a space keeps it out of shell history on most setups. The token is never
printed by any command afterwards.

## 4. Configure

Edit `~/.config/android-timeline/config.toml`:

```toml
[device]
device_id = "device-my-pixel-001"      # pseudonymous; not a serial number

[server]
base_url = "https://your-home-assistant.example:8099"
```

Everything sensitive is off by default. Turn things on deliberately:

```toml
[privacy]
location_mode = "coarse"    # disabled | coarse | geohash | precise | raw

[collectors.location]
enabled = true
```

## 5. Verify

```bash
android-timeline doctor --check-server
```

Every check should be `ok` or `warn`. A `fail` tells you what is wrong. The
output is safe to paste into an issue: it contains no token and no salt.

Then try one collection pass:

```bash
android-timeline --json collect-once --heartbeat
android-timeline --json status
android-timeline --json upload
```

## 6. Run it continuously

```bash
./scripts/start-collector.sh     # background, writes a PID file
./scripts/stop-collector.sh      # SIGTERM, waits for the current tick
```

`start-collector.sh` acquires a Termux wake lock. Without one, Android
suspends the process and the queue silently stops filling.

### Android will still try to kill it

Grant Termux battery-optimisation exemption:

**Settings -> Apps -> Termux -> Battery -> Unrestricted**

Manufacturer skins (Xiaomi, Samsung, Huawei, OnePlus, Oppo) add their own,
more aggressive process killers with their own settings screens.
<https://dontkillmyapp.com> documents them per vendor. **This is the single
most likely reason for data gaps on a real phone, and it is not something
this software can fix.**

## 7. After a reboot

Termux:Boot runs `~/.termux/boot/start-android-timeline`, which the installer
placed there. Verify after your next reboot:

```bash
cat ~/.local/share/android-timeline/boot.log
android-timeline --json status
```

If nothing happened: open the Termux:Boot app once manually. It does not
activate until it has been launched at least once.

## Uninstalling

```bash
./scripts/uninstall-termux.sh              # keeps your data
./scripts/uninstall-termux.sh --purge-data # deletes events, config, token, salt
```

The purge asks you to type `DELETE`. It cannot be triggered by accident.
