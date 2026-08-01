#!/data/data/com.termux/files/usr/bin/bash
#
# Stop the collector loop. SIGTERM lets the scheduler finish its current
# tick so no batch is left claimed but unsent.
set -euo pipefail

DATA_DIR="${ANDROID_TIMELINE_HOME:-${HOME}/.local/share/android-timeline}"
PID_FILE="${DATA_DIR}/collector.pid"

# Releasing the wake lock must never fail the script: Termux:API may be
# absent, and the collector is already stopped by the time we get here.
release_wake_lock() {
    if command -v termux-wake-unlock >/dev/null 2>&1; then
        termux-wake-unlock || true
    fi
}

if [ ! -f "$PID_FILE" ]; then
    echo "[stop] no PID file at ${PID_FILE}; nothing to do"
    exit 0
fi

PID="$(cat "$PID_FILE")"
if ! kill -0 "$PID" 2>/dev/null; then
    echo "[stop] PID ${PID} is not running; clearing stale PID file"
    rm -f "$PID_FILE"
    exit 0
fi

echo "[stop] sending SIGTERM to ${PID}"
kill -TERM "$PID"

for _ in $(seq 1 30); do
    if ! kill -0 "$PID" 2>/dev/null; then
        rm -f "$PID_FILE"
        release_wake_lock
        echo "[stop] stopped cleanly"
        exit 0
    fi
    sleep 1
done

echo "[stop] still running after 30s; sending SIGKILL" >&2
kill -KILL "$PID" 2>/dev/null || true
rm -f "$PID_FILE"
release_wake_lock
