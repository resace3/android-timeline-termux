#!/data/data/com.termux/files/usr/bin/bash
#
# Remove the collector. Collected data is preserved unless --purge-data is
# given, and the confirmation prompt cannot be skipped accidentally.
set -euo pipefail

CONFIG_DIR="${HOME}/.config/android-timeline"
DATA_DIR="${HOME}/.local/share/android-timeline"
VENV_DIR="${DATA_DIR}/venv"
BOOT_LAUNCHER="${HOME}/.termux/boot/start-android-timeline"

PURGE_DATA=0
ASSUME_YES=0

while [ $# -gt 0 ]; do
    case "$1" in
        --purge-data) PURGE_DATA=1 ;;
        --yes) ASSUME_YES=1 ;;
        -h|--help)
            echo "Usage: uninstall-termux.sh [--purge-data] [--yes]"
            echo "  --purge-data  also delete the event database, config, token and salt"
            exit 0
            ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

# Stop first so nothing is writing while we remove files.
if [ -x "$(dirname "${BASH_SOURCE[0]}")/stop-collector.sh" ]; then
    "$(dirname "${BASH_SOURCE[0]}")/stop-collector.sh" || true
fi

rm -f "$BOOT_LAUNCHER"
echo "[uninstall] removed Termux:Boot launcher"

rm -rf "$VENV_DIR"
echo "[uninstall] removed virtualenv"

if [ "$PURGE_DATA" -eq 1 ]; then
    if [ "$ASSUME_YES" -eq 0 ]; then
        printf '[uninstall] Delete ALL collected events, config, token and salt? [type DELETE] '
        read -r reply
        if [ "$reply" != "DELETE" ]; then
            echo "[uninstall] aborted; data left intact"
            exit 1
        fi
    fi
    rm -rf "$DATA_DIR" "$CONFIG_DIR"
    echo "[uninstall] purged ${DATA_DIR} and ${CONFIG_DIR}"
else
    echo "[uninstall] collected data kept at ${DATA_DIR}"
    echo "[uninstall] configuration kept at ${CONFIG_DIR}"
    echo "[uninstall] re-run with --purge-data to remove them"
fi
