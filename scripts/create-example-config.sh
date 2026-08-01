#!/usr/bin/env bash
#
# Regenerate config.example.toml from the template embedded in the CLI.
# The CLI template is the single source of truth; CI fails if the committed
# file has drifted from it.
set -euo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-${REPO_DIR}/config.example.toml}"

python3 -m android_timeline.cli init --print-example > "$TARGET"
echo "wrote ${TARGET}"
