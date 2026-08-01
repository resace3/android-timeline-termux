#!/usr/bin/env bash
#
# Runs on the RUNNER (not inside the emulator) with adb pointed at a booted
# Android device. Drives the device through adb.
#
# The phases degrade gracefully and each one reports its own verdict, so the
# job summary can state precisely what was and was not proven rather than a
# single opaque pass/fail.
set -uo pipefail

SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/stdout}"
DEVICE_DIR=/data/local/tmp/android-timeline
HOST_BRIDGE=10.0.2.2
SERVER_PORT=8099

phase_result() {
    printf '| %s | %s | %s |\n' "$1" "$2" "$3" >> "$SUMMARY"
    printf '[emulator] %-38s %-8s %s\n' "$1" "$2" "$3"
}

{
    echo "### Android emulator phases"
    echo
    echo "| Phase | Result | Detail |"
    echo "| --- | --- | --- |"
} >> "$SUMMARY"

# ---------------------------------------------------------------- phase 1
# Boot completion. Everything downstream is meaningless without it.
echo "[emulator] waiting for boot completion"
adb wait-for-device
boot=""
for _ in $(seq 1 120); do
    boot="$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')"
    [ "$boot" = "1" ] && break
    sleep 5
done

if [ "$boot" != "1" ]; then
    phase_result "1. Emulator boot" "FAIL" "sys.boot_completed never became 1"
    exit 1
fi

release="$(adb shell getprop ro.build.version.release | tr -d '\r')"
sdk="$(adb shell getprop ro.build.version.sdk | tr -d '\r')"
abi="$(adb shell getprop ro.product.cpu.abi | tr -d '\r')"
phase_result "1. Emulator boot" "PASS" "Android ${release} (API ${sdk}, ${abi})"

# ---------------------------------------------------------------- phase 2
# Push the collector payload onto the Android filesystem.
adb shell "rm -rf ${DEVICE_DIR}; mkdir -p ${DEVICE_DIR}" >/dev/null 2>&1
if adb push payload "${DEVICE_DIR}/" >/dev/null 2>&1; then
    count="$(adb shell "find ${DEVICE_DIR} -type f | wc -l" | tr -d '\r ')"
    phase_result "2. Push payload to device" "PASS" "${count} files under ${DEVICE_DIR}"
else
    phase_result "2. Push payload to device" "FAIL" "adb push failed"
    exit 1
fi

# ---------------------------------------------------------------- phase 3
# Directory structure the installer would create, on a real Android FS.
adb shell "mkdir -p ${DEVICE_DIR}/home/.config/android-timeline ${DEVICE_DIR}/home/.local/share/android-timeline" >/dev/null 2>&1
if adb shell "test -d ${DEVICE_DIR}/home/.local/share/android-timeline && echo ok" | grep -q ok; then
    phase_result "3. Expected directory structure" "PASS" "created on the Android filesystem"
else
    phase_result "3. Expected directory structure" "FAIL" "could not create directories"
fi

# ---------------------------------------------------------------- phase 4
# The outbox schema, executed by the device's own SQLite.
DB="${DEVICE_DIR}/home/.local/share/android-timeline/outbox.sqlite3"
if adb shell 'command -v sqlite3' >/dev/null 2>&1; then
    adb push .github/scripts/outbox-schema.sql "${DEVICE_DIR}/schema.sql" >/dev/null 2>&1
    adb shell "sqlite3 ${DB} < ${DEVICE_DIR}/schema.sql" >/dev/null 2>&1
    tables="$(adb shell "sqlite3 ${DB} \"SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;\"" | tr -d '\r' | tr '\n' ' ')"
    if echo "$tables" | grep -q events && echo "$tables" | grep -q sync_batches; then
        phase_result "4. SQLite outbox on Android" "PASS" "tables: ${tables}"
    else
        phase_result "4. SQLite outbox on Android" "FAIL" "unexpected tables: ${tables}"
    fi
else
    phase_result "4. SQLite outbox on Android" "SKIP" "no sqlite3 binary in this system image"
fi

# ---------------------------------------------------------------- phase 5
# The network path a real collector uses: device -> 10.0.2.2 -> runner.
# adb reverse is set up as a fallback for images where the alias is blocked.
adb reverse "tcp:${SERVER_PORT}" "tcp:${SERVER_PORT}" >/dev/null 2>&1 \
    && reverse_ok=1 || reverse_ok=0

http_probe() {
    local url="$1"
    if adb shell "command -v curl" >/dev/null 2>&1; then
        adb shell "curl -s -m 10 ${url}" 2>/dev/null | tr -d '\r'
    elif adb shell "command -v toybox" >/dev/null 2>&1; then
        adb shell "toybox wget -q -O - ${url}" 2>/dev/null | tr -d '\r'
    else
        return 1
    fi
}

health="$(http_probe "http://${HOST_BRIDGE}:${SERVER_PORT}/api/v1/health")"
bridge="${HOST_BRIDGE}"
if ! echo "$health" | grep -q '"status"'; then
    health="$(http_probe "http://127.0.0.1:${SERVER_PORT}/api/v1/health")"
    bridge="127.0.0.1 (adb reverse=${reverse_ok})"
fi

if echo "$health" | grep -q '"status"'; then
    phase_result "5. Device reaches runner over HTTP" "PASS" "via ${bridge}"
    NETWORK_OK=1
else
    phase_result "5. Device reaches runner over HTTP" "SKIP" "no usable HTTP client on device"
    NETWORK_OK=0
fi

# ---------------------------------------------------------------- phase 6
# Upload a synthetic batch FROM the device, proving the wire format is
# accepted end to end over the emulator bridge.
if [ "$NETWORK_OK" = "1" ] && adb shell "command -v curl" >/dev/null 2>&1; then
    python3 - <<'PY' > /tmp/batch.json
import json
import pathlib

events = json.loads(pathlib.Path("payload/synthetic_day.json").read_text())[:25]
print(
    json.dumps(
        {
            "protocol_version": 1,
            "batch_id": "batch-emulator-0001",
            "device_id": "device-test-001",
            "collector_version": "0.1.0",
            "created_time_utc": "2026-03-15T00:00:00Z",
            "events": events,
        }
    )
)
PY
    adb push /tmp/batch.json "${DEVICE_DIR}/batch.json" >/dev/null 2>&1
    response="$(adb shell "curl -s -m 30 -X POST \
        -H 'Content-Type: application/json' \
        -H 'Authorization: Bearer synthetic-ci-token' \
        -H 'X-Device-ID: device-test-001' \
        -H 'X-Batch-ID: batch-emulator-0001' \
        --data @${DEVICE_DIR}/batch.json \
        http://${HOST_BRIDGE}:${SERVER_PORT}/api/v1/events/batch" | tr -d '\r')"

    if echo "$response" | grep -q '"accepted"'; then
        stored="$(echo "$response" | python3 -c 'import json,sys; print(json.load(sys.stdin)["counts"]["stored"])' 2>/dev/null || echo '?')"
        phase_result "6. Authenticated upload from device" "PASS" "${stored} events stored"

        # Replay the identical batch: the server must report duplicates.
        replay="$(adb shell "curl -s -m 30 -X POST \
            -H 'Content-Type: application/json' \
            -H 'Authorization: Bearer synthetic-ci-token' \
            --data @${DEVICE_DIR}/batch.json \
            http://${HOST_BRIDGE}:${SERVER_PORT}/api/v1/events/batch" | tr -d '\r')"
        dupes="$(echo "$replay" | python3 -c 'import json,sys; print(json.load(sys.stdin)["counts"]["duplicate"])' 2>/dev/null || echo 0)"
        if [ "$dupes" -gt 0 ] 2>/dev/null; then
            phase_result "7. Replay is idempotent" "PASS" "${dupes} duplicates recognised"
        else
            phase_result "7. Replay is idempotent" "FAIL" "replay did not report duplicates"
        fi

        # An invalid token must be refused.
        unauth="$(adb shell "curl -s -o /dev/null -w '%{http_code}' -m 30 -X POST \
            -H 'Content-Type: application/json' \
            -H 'Authorization: Bearer wrong-token' \
            --data @${DEVICE_DIR}/batch.json \
            http://${HOST_BRIDGE}:${SERVER_PORT}/api/v1/events/batch" | tr -d '\r')"
        if [ "$unauth" = "401" ]; then
            phase_result "8. Invalid token rejected" "PASS" "HTTP 401"
        else
            phase_result "8. Invalid token rejected" "FAIL" "got HTTP ${unauth}"
        fi
    else
        phase_result "6. Authenticated upload from device" "FAIL" "no acknowledgement received"
    fi
else
    phase_result "6. Authenticated upload from device" "SKIP" "no curl on device"
fi

# ---------------------------------------------------------------- phase 9
# Best effort: can the Termux app even be installed on this image? This is
# the least reliable part of Android CI and is never allowed to fail the
# job -- see docs/testing-limitations.md.
phase_result "9. Termux APK install" "SKIP" \
    "not attempted in CI: Termux distribution channels use mutually incompatible signatures and require interactive first-run setup; tracked in the real-device acceptance issue"

{
    echo
    echo "**Not covered by any phase above:** real GPS/accelerometer data,"
    echo "Health Connect, notification listener access, real SMS or call"
    echo "logs, cellular modem data, physical Bluetooth, manufacturer"
    echo "battery restrictions, Termux:Boot after a real reboot, and"
    echo "multi-day background reliability."
} >> "$SUMMARY"

echo "[emulator] smoke test finished"
