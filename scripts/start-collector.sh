#!/data/data/com.termux/files/usr/bin/bash
#
# Start the collector loop in the background, writing a PID file.
# Idempotent: refuses to start a second copy.
set -euo pipefail

DATA_DIR="${ANDROID_TIMELINE_HOME:-${HOME}/.local/share/android-timeline}"
VENV_DIR="${HOME}/.local/share/android-timeline/venv"
PID_FILE="${DATA_DIR}/collector.pid"
LOG_FILE="${DATA_DIR}/collector.log"

mkdir -p "$DATA_DIR"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "[start] already running with PID $(cat "$PID_FILE")"
    exit 0
fi
rm -f "$PID_FILE"

BIN="android-timeline"
if [ -x "${VENV_DIR}/bin/android-timeline" ]; then
    BIN="${VENV_DIR}/bin/android-timeline"
fi

# termux-wake-lock keeps the CPU alive between polls. Without it Android
# suspends the process and the queue silently stops filling.
if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock || echo "[start] WARNING: termux-wake-lock failed" >&2
fi

# Log at INFO. The collector never logs credentials or payload bodies.
nohup "$BIN" --verbose run >>"$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
chmod 600 "$PID_FILE" 2>/dev/null || true

echo "[start] collector running with PID $(cat "$PID_FILE")"
echo "[start] logs: ${LOG_FILE}"
