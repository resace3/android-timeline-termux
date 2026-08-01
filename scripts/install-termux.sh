#!/data/data/com.termux/files/usr/bin/bash
#
# Idempotent installer for the android-timeline collector inside Termux.
#
# Safe to re-run: it never overwrites an existing config, token or salt
# without --force, and never downgrades file permissions.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

CONFIG_DIR="${HOME}/.config/android-timeline"
DATA_DIR="${HOME}/.local/share/android-timeline"
VENV_DIR="${HOME}/.local/share/android-timeline/venv"
BOOT_DIR="${HOME}/.termux/boot"

FORCE=0
SKIP_PACKAGES=0
SKIP_BOOT=0

usage() {
    cat <<'USAGE'
Usage: install-termux.sh [options]

  --force           overwrite an existing configuration file
  --skip-packages   do not run pkg install (useful in CI / offline)
  --skip-boot       do not install the Termux:Boot launcher
  -h, --help        show this help

The script never prints or logs the device token.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --force) FORCE=1 ;;
        --skip-packages) SKIP_PACKAGES=1 ;;
        --skip-boot) SKIP_BOOT=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

log()  { printf '[install] %s\n' "$*"; }
warn() { printf '[install] WARNING: %s\n' "$*" >&2; }
die()  { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

# --- 1. Verify we are actually in Termux ------------------------------------
# Checking the prefix directory (not just $PREFIX) means an exported variable
# is not enough to trick the installer into writing to the wrong place.
if [ ! -d "/data/data/com.termux/files/usr" ] && \
   ! printf '%s' "${PREFIX:-}" | grep -q 'com.termux'; then
    die "this script must be run inside Termux (no Termux prefix found).
     For a desktop/CI install use:  pip install -e ."
fi

log "Termux prefix: ${PREFIX:-/data/data/com.termux/files/usr}"

# --- 2. Required commands ---------------------------------------------------
missing_required=()
for cmd in python3 sqlite3; do
    command -v "$cmd" >/dev/null 2>&1 || missing_required+=("$cmd")
done

if [ "${#missing_required[@]}" -gt 0 ] && [ "$SKIP_PACKAGES" -eq 0 ]; then
    log "installing missing packages: ${missing_required[*]}"
    pkg install -y python
else
    if [ "${#missing_required[@]}" -gt 0 ]; then
        warn "missing commands (--skip-packages was given): ${missing_required[*]}"
    fi
fi

command -v python3 >/dev/null 2>&1 || die "python3 is required but not installed"

python_version="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
log "python ${python_version}"
python3 - <<'PY' || die "python 3.11 or newer is required (Termux: pkg upgrade python)"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY

# Termux:API provides the termux-* helpers. Collection degrades gracefully
# without it, so this is a warning rather than a hard failure.
if ! command -v termux-battery-status >/dev/null 2>&1; then
    warn "termux-battery-status not found. Install the Termux:API *app* from the
     same distribution source as Termux, then: pkg install termux-api"
fi

# --- 3. Directories ---------------------------------------------------------
mkdir -p "$CONFIG_DIR" "$DATA_DIR"
chmod 700 "$CONFIG_DIR" "$DATA_DIR" 2>/dev/null || warn "could not chmod 700 config/data dirs"

# --- 4. Virtual environment + install ---------------------------------------
if [ ! -d "$VENV_DIR" ]; then
    log "creating virtualenv at ${VENV_DIR}"
    python3 -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
. "${VENV_DIR}/bin/activate"

log "installing android-timeline from ${REPO_DIR}"
python3 -m pip install --disable-pip-version-check --quiet --upgrade pip
python3 -m pip install --disable-pip-version-check --quiet "${REPO_DIR}"

# --- 5. Configuration -------------------------------------------------------
CONFIG_FILE="${CONFIG_DIR}/config.toml"
if [ -f "$CONFIG_FILE" ] && [ "$FORCE" -eq 0 ]; then
    log "keeping existing ${CONFIG_FILE} (use --force to replace)"
else
    if [ -f "$CONFIG_FILE" ]; then
        backup="${CONFIG_FILE}.$(date -u +%Y%m%dT%H%M%SZ).bak"
        cp "$CONFIG_FILE" "$backup"
        log "backed up previous config to ${backup}"
    fi
    android-timeline init --print-example > "$CONFIG_FILE"
    log "wrote ${CONFIG_FILE}"
fi
chmod 600 "$CONFIG_FILE" 2>/dev/null || warn "could not chmod 600 ${CONFIG_FILE}"

# --- 6. Pseudonymisation salt ----------------------------------------------
SALT_FILE="${CONFIG_DIR}/salt"
if [ ! -f "$SALT_FILE" ]; then
    log "generating a local pseudonymisation salt"
    python3 -c 'import secrets; print(secrets.token_hex(32))' > "$SALT_FILE"
else
    log "keeping existing salt"
fi
chmod 600 "$SALT_FILE" 2>/dev/null || warn "could not chmod 600 ${SALT_FILE}"

# --- 7. Token placeholder ---------------------------------------------------
TOKEN_FILE="${CONFIG_DIR}/token"
if [ ! -f "$TOKEN_FILE" ]; then
    : > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE" 2>/dev/null || true
    log "created empty ${TOKEN_FILE} -- paste the enrollment token into it"
fi

# --- 8. Termux:Boot launcher ------------------------------------------------
if [ "$SKIP_BOOT" -eq 0 ]; then
    mkdir -p "$BOOT_DIR"
    install -m 700 "${REPO_DIR}/termux-boot/start-android-timeline" \
        "${BOOT_DIR}/start-android-timeline"
    log "installed Termux:Boot launcher at ${BOOT_DIR}/start-android-timeline"
    log "  (requires the Termux:Boot app, launched once after install)"
fi

# --- 9. Done ----------------------------------------------------------------
cat <<EOF

[install] Done.

Next steps:
  1. edit  ${CONFIG_FILE}
       - device.device_id   (pseudonymous)
       - server.base_url    (https:// URL of your Home Assistant app)
  2. paste the enrollment token into ${TOKEN_FILE}
  3. run: ${VENV_DIR}/bin/android-timeline doctor --check-server

Neither the token nor the salt is ever printed by any command.
EOF
