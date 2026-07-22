#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
bash scripts/connect_telegram.sh
echo
read -r -p "Telegram setup is complete. Press Enter to close this window."
