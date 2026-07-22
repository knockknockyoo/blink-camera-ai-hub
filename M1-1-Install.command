#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

echo "========================================"
echo " Blink Camera AI Hub M1 Setup"
echo "========================================"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "This package requires an Apple Silicon (M1/M2/M3/M4) Mac."
  read -r -p "Press Enter to exit."
  exit 1
fi

python_bin=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(not ((3, 10) <= sys.version_info[:2] <= (3, 13)))'; then
    python_bin="$candidate"
    break
  fi
done

if [[ -z "$python_bin" ]]; then
  echo "Python 3.10 through 3.13 is required. Python 3.13 is recommended."
  echo "Install it from https://www.python.org/downloads/macos/."
  read -r -p "Press Enter to exit."
  exit 1
fi

export BLINK_CAMERA_AI_HUB_PYTHON="$python_bin"
echo "Python selected: $($python_bin --version)"

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "Node.js 22 or newer is required. Install the LTS release from https://nodejs.org/."
  read -r -p "Press Enter to exit."
  exit 1
fi

node_major="$(node -p 'process.versions.node.split(".")[0]')"
if (( node_major < 22 )); then
  echo "Current Node.js: $(node --version)"
  echo "Install Node.js 22 or newer: https://nodejs.org/"
  read -r -p "Press Enter to exit."
  exit 1
fi

bash scripts/setup.sh

echo
echo "Setup is complete."
echo "1) M1-2-Connect-Blink.command"
echo "2) Optional: M1-3-Connect-Telegram.command"
echo "3) M1-4-Start.command"
read -r -p "Press Enter to close this window."
