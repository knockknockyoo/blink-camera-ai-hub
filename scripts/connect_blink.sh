#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -d .venv ]]; then
  echo "먼저 bash scripts/setup.sh를 실행하세요."
  exit 1
fi

. .venv/bin/activate
python -m backend.setup_blink

echo "연결 완료. bash scripts/run.sh로 시작하세요."
