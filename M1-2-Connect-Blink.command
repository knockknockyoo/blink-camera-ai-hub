#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
bash scripts/connect_blink.sh
echo
read -r -p "Blink 연결이 끝났습니다. Enter를 누르면 창을 닫습니다."
