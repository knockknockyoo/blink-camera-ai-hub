#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python_bin="${BLINK_CAMERA_AI_HUB_PYTHON:-${BLINK_SENTINEL_PYTHON:-}}"
if [[ -z "$python_bin" ]]; then
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(not ((3, 10) <= sys.version_info[:2] <= (3, 13)))'; then
      python_bin="$candidate"
      break
    fi
  done
fi

if [[ -z "$python_bin" ]]; then
  echo "Python 3.10 through 3.13 is required. Python 3.13 is recommended."
  exit 1
fi

if [[ -d .venv ]] && ! .venv/bin/python -c 'import sys; raise SystemExit(not ((3, 10) <= sys.version_info[:2] <= (3, 13)))' >/dev/null 2>&1; then
  backup=".venv-incompatible-$(date +%Y%m%d-%H%M%S)"
  echo "The existing virtual environment is incompatible and will be moved to $backup."
  mv .venv "$backup"
fi

if [[ ! -d .venv ]]; then
  echo "Creating a new virtual environment with $($python_bin --version)."
  "$python_bin" -m venv .venv
fi

if [[ ! -x .venv/bin/python || ! -f .venv/bin/activate ]]; then
  echo "Virtual environment creation failed."
  exit 1
fi

. .venv/bin/activate
echo "Virtual environment ready: $(python --version) ($(command -v python))"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
npm ci --ignore-scripts --no-audit --no-fund

if [[ ! -f .env ]]; then
  cp .env.example .env
fi

echo
echo "Setup complete. Preview the demo with: bash scripts/run.sh"
echo "Connect a Blink account with: bash scripts/connect_blink.sh"
