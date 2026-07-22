#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -x .venv/bin/python ]]; then
  echo "Run bash scripts/setup.sh first."
  exit 1
fi

.venv/bin/python -m scripts.connect_telegram
