#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
bash scripts/connect_blink.sh
echo
read -r -p "Blink setup is complete. Press Enter to close this window."
