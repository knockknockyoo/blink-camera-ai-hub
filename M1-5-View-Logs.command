#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p data/logs
touch data/logs/blink-camera-ai-hub.log
echo "Ctrl+C를 누르면 로그 보기를 종료합니다."
tail -f data/logs/blink-camera-ai-hub.log
