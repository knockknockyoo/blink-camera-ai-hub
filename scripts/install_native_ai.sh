#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! /opt/homebrew/bin/brew list vips >/dev/null 2>&1; then
  echo "Installing the native libvips dependency for Moondream2."
  /opt/homebrew/bin/brew install vips
fi

if [[ ! -x .venv/bin/python ]]; then
  echo "Creating an Apple Silicon Python virtual environment."
  /opt/homebrew/bin/python3.13 -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-native.txt

if [[ ! -f .env ]]; then
  cp .env.example .env
fi

.venv/bin/python scripts/configure_native_ai.py
echo "Native AI dependencies and settings are ready."
echo "Start it with: bash scripts/run_native_ai.sh"
echo "Keep it running with: bash scripts/enable_native_ai_service.sh"
