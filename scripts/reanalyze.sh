#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "Blink Camera AI Hub 기존 영상 초기화 및 재분석 준비"
python3 scripts/reset_for_reanalysis.py "$@"

echo "Blink Camera AI Hub를 다시 시작합니다."
exec bash scripts/run.sh
