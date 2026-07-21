#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
bash scripts/connect_telegram.sh
echo
read -r -p "Telegram 연결이 끝났습니다. Enter를 누르면 창을 닫습니다."
