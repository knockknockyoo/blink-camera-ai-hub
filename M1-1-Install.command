#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

echo "========================================"
echo " Blink Camera AI Hub M1 설치"
echo "========================================"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "이 패키지는 Apple Silicon(M1/M2/M3/M4) Mac용입니다."
  read -r -p "Enter를 누르면 종료합니다."
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
  echo "Python 3.10~3.13이 필요합니다. Python 3.13 설치를 권장합니다."
  echo "https://www.python.org/downloads/macos/ 에서 설치하세요."
  read -r -p "Enter를 누르면 종료합니다."
  exit 1
fi

export BLINK_CAMERA_AI_HUB_PYTHON="$python_bin"
echo "사용할 Python: $($python_bin --version)"

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "Node.js 22 이상이 필요합니다. https://nodejs.org/ 에서 LTS 버전을 설치하세요."
  read -r -p "Enter를 누르면 종료합니다."
  exit 1
fi

node_major="$(node -p 'process.versions.node.split(".")[0]')"
if (( node_major < 22 )); then
  echo "현재 Node.js: $(node --version)"
  echo "Node.js 22 이상을 설치하세요: https://nodejs.org/"
  read -r -p "Enter를 누르면 종료합니다."
  exit 1
fi

bash scripts/setup.sh

echo
echo "설치가 완료됐습니다."
echo "1) M1-2-Connect-Blink.command"
echo "2) 필요하면 M1-3-Connect-Telegram.command"
echo "3) M1-4-Start.command"
read -r -p "Enter를 누르면 창을 닫습니다."
