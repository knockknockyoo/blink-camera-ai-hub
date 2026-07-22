#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p data/logs
touch data/logs/blink-camera-ai-hub.log
echo "Press Ctrl+C to stop following the log."
tail -f data/logs/blink-camera-ai-hub.log
