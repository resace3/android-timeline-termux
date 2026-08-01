#!/data/data/com.termux/files/usr/bin/bash
#
# Non-zero exit means something needs attention. Suitable for a cron entry.
# Prints JSON so it can be piped somewhere useful; never prints secrets.
set -euo pipefail

VENV_DIR="${HOME}/.local/share/android-timeline/venv"
BIN="android-timeline"
if [ -x "${VENV_DIR}/bin/android-timeline" ]; then
    BIN="${VENV_DIR}/bin/android-timeline"
fi

CHECK_SERVER=""
[ "${1:-}" = "--check-server" ] && CHECK_SERVER="--check-server"

# shellcheck disable=SC2086
exec "$BIN" --json doctor $CHECK_SERVER
