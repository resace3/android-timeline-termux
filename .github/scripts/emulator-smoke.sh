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
CI_TOKEN=synthetic-ci-token

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
adb shell "rm -rf ${DEVICE_DIR}; mkdir -p ${DEVICE_DIR}" >/dev/null 2>&1
if adb push payload "${DEVICE_DIR}/" >/dev/null 2>&1; then
    count="$(adb shell "find ${DEVICE_DIR} -type f | wc -l" | tr -d '\r ')"
    phase_result "2. Push payload to device" "PASS" "${count} files under ${DEVICE_DIR}"
else
    phase_result "2. Push payload to device" "FAIL" "adb push failed"
    exit 1
fi

# ---------------------------------------------------------------- phase 3
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
#
# AOSP system images ship neither curl nor wget, so the fallback speaks
# HTTP/1.1 over toybox netcat. That is still a genuine request originating
# on the device: only the client library differs from the real collector.
HTTP_CLIENT=none
if adb shell 'command -v curl' >/dev/null 2>&1; then
    HTTP_CLIENT=curl
elif adb shell 'command -v toybox' >/dev/null 2>&1 && \
     adb shell 'toybox nc --help' >/dev/null 2>&1; then
    HTTP_CLIENT=nc
elif adb shell 'command -v nc' >/dev/null 2>&1; then
    HTTP_CLIENT=nc
fi

nc_cmd() {
    if adb shell 'command -v toybox' >/dev/null 2>&1; then
        echo "toybox nc"
    else
        echo "nc"
    fi
}

# device_request <request-file-on-device> -> raw response on stdout
device_request() {
    local remote="$1"
    case "$HTTP_CLIENT" in
        nc)
            adb shell "$(nc_cmd) -w 20 ${HOST_BRIDGE} ${SERVER_PORT} < ${remote}" 2>/dev/null | tr -d '\r'
            ;;
        curl)
            # curl is driven directly by the callers below; unused here.
            return 1
            ;;
        *) return 1 ;;
    esac
}

build_request() {
    # $1 method, $2 path, $3 body file (optional), $4 extra header lines
    local method="$1" path="$2" body_file="${3:-}" extra="${4:-}"
    {
        printf '%s %s HTTP/1.1\r\n' "$method" "$path"
        printf 'Host: %s:%s\r\n' "$HOST_BRIDGE" "$SERVER_PORT"
        printf 'User-Agent: android-timeline-emulator-smoke\r\n'
        printf 'Connection: close\r\n'
        if [ -n "$extra" ]; then printf '%s' "$extra"; fi
        if [ -n "$body_file" ]; then
            printf 'Content-Type: application/json\r\n'
            printf 'Content-Length: %s\r\n' "$(wc -c < "$body_file" | tr -d ' ')"
            printf '\r\n'
            cat "$body_file"
        else
            printf '\r\n'
        fi
    }
}

if [ "$HTTP_CLIENT" = "none" ]; then
    phase_result "5. Device reaches runner over HTTP" "SKIP" "no HTTP client or netcat on device"
    NETWORK_OK=0
else
    build_request GET /api/v1/health > /tmp/req-health.txt
    adb push /tmp/req-health.txt "${DEVICE_DIR}/req-health.txt" >/dev/null 2>&1

    if [ "$HTTP_CLIENT" = "curl" ]; then
        health="$(adb shell "curl -s -m 15 http://${HOST_BRIDGE}:${SERVER_PORT}/api/v1/health" | tr -d '\r')"
    else
        health="$(device_request "${DEVICE_DIR}/req-health.txt")"
    fi

    if echo "$health" | grep -q '"status"'; then
        phase_result "5. Device reaches runner over HTTP" "PASS" "via ${HOST_BRIDGE} using ${HTTP_CLIENT}"
        NETWORK_OK=1
    else
        phase_result "5. Device reaches runner over HTTP" "FAIL" "no response via ${HTTP_CLIENT}"
        echo "[emulator] health response was: ${health}"
        NETWORK_OK=0
    fi
fi

# ---------------------------------------------------------------- phase 6-8
if [ "$NETWORK_OK" = "1" ]; then
    python3 - <<'PY' > /tmp/batch.json
import json
import pathlib

events = json.loads(pathlib.Path("payload/synthetic_day.json").read_text())[:25]
pathlib.Path("/tmp/batch.json")
print(
    json.dumps(
        {
            "protocol_version": 1,
            "batch_id": "batch-emulator-0001",
            "device_id": "device-test-001",
            "collector_version": "0.1.0",
            "created_time_utc": "2026-03-15T00:00:00Z",
            "events": events,
        },
        separators=(",", ":"),
    )
)
PY
    # Strip the trailing newline: it would be counted in Content-Length.
    printf '%s' "$(cat /tmp/batch.json)" > /tmp/batch-body.json

    auth_headers=$(printf 'Authorization: Bearer %s\r\nX-Device-ID: device-test-001\r\nX-Batch-ID: batch-emulator-0001\r\n' "$CI_TOKEN")
    bad_headers=$(printf 'Authorization: Bearer wrong-token\r\nX-Device-ID: device-test-001\r\n')

    build_request POST /api/v1/events/batch /tmp/batch-body.json "$auth_headers" > /tmp/req-batch.txt
    build_request POST /api/v1/events/batch /tmp/batch-body.json "$bad_headers" > /tmp/req-bad.txt
    adb push /tmp/req-batch.txt "${DEVICE_DIR}/req-batch.txt" >/dev/null 2>&1
    adb push /tmp/req-bad.txt "${DEVICE_DIR}/req-bad.txt" >/dev/null 2>&1

    send() {
        if [ "$HTTP_CLIENT" = "curl" ]; then
            adb shell "curl -s -i -m 30 -X POST \
                -H 'Content-Type: application/json' \
                -H 'Authorization: Bearer $2' \
                --data @${DEVICE_DIR}/batch-body.json \
                http://${HOST_BRIDGE}:${SERVER_PORT}/api/v1/events/batch" | tr -d '\r'
        else
            device_request "$1"
        fi
    }
    adb push /tmp/batch-body.json "${DEVICE_DIR}/batch-body.json" >/dev/null 2>&1

    response="$(send "${DEVICE_DIR}/req-batch.txt" "$CI_TOKEN")"
    body="$(echo "$response" | awk 'BEGIN{b=0} /^$/{b=1;next} b{print}')"

    if echo "$body" | grep -q '"accepted"'; then
        stored="$(echo "$body" | python3 -c 'import json,sys; print(json.load(sys.stdin)["counts"]["stored"])' 2>/dev/null || echo '?')"
        phase_result "6. Authenticated upload from device" "PASS" "${stored} events stored"

        replay="$(send "${DEVICE_DIR}/req-batch.txt" "$CI_TOKEN")"
        replay_body="$(echo "$replay" | awk 'BEGIN{b=0} /^$/{b=1;next} b{print}')"
        dupes="$(echo "$replay_body" | python3 -c 'import json,sys; print(json.load(sys.stdin)["counts"]["duplicate"])' 2>/dev/null || echo 0)"
        if [ "${dupes:-0}" -gt 0 ] 2>/dev/null; then
            phase_result "7. Replay is idempotent" "PASS" "${dupes} duplicates recognised"
        else
            phase_result "7. Replay is idempotent" "FAIL" "replay did not report duplicates"
        fi

        unauth="$(send "${DEVICE_DIR}/req-bad.txt" wrong-token)"
        if echo "$unauth" | head -n 1 | grep -q '401'; then
            phase_result "8. Invalid token rejected" "PASS" "HTTP 401"
        else
            phase_result "8. Invalid token rejected" "FAIL" "unexpected status: $(echo "$unauth" | head -n 1)"
        fi
    else
        phase_result "6. Authenticated upload from device" "FAIL" "no acknowledgement received"
        echo "[emulator] response was: ${response}"
    fi
else
    phase_result "6. Authenticated upload from device" "SKIP" "no usable HTTP client on device"
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
