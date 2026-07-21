#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -x .venv/bin/python ]]; then
  echo "먼저 bash scripts/setup.sh를 실행하세요."
  exit 1
fi

.venv/bin/python -m scripts.connect_telegram
