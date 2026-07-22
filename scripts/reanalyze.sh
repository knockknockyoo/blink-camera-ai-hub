#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "Resetting existing videos and preparing Blink Camera AI Hub for reanalysis"
python3 scripts/reset_for_reanalysis.py "$@"

echo "Restarting Blink Camera AI Hub."
exec bash scripts/run.sh
