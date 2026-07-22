#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -d .venv ]]; then
  echo "Run bash scripts/setup.sh first."
  exit 1
fi

. .venv/bin/activate
python -m backend.setup_blink

echo "Connection complete. Start with bash scripts/run.sh."
