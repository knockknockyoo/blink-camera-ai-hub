#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -x .venv/bin/python ]]; then
  echo "Missing .venv. Run: python3.13 -m venv .venv" >&2
  exit 1
fi

if ! .venv/bin/python -c \
  'import torch; raise SystemExit(0 if torch.backends.mps.is_available() else 1)'
then
  echo "Apple MPS is unavailable in this Python environment." >&2
  echo "Use an arm64 Homebrew Python directly on the M1 Mac." >&2
  exit 1
fi

exec .venv/bin/python -m uvicorn native_ai.main:app \
  --host 0.0.0.0 \
  --port "${NATIVE_AI_PORT:-8790}"
