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
  echo "Python 3.10~3.13이 필요합니다. Python 3.13 설치를 권장합니다."
  exit 1
fi

if [[ -d .venv ]] && ! .venv/bin/python -c 'import sys; raise SystemExit(not ((3, 10) <= sys.version_info[:2] <= (3, 13)))' >/dev/null 2>&1; then
  backup=".venv-incompatible-$(date +%Y%m%d-%H%M%S)"
  echo "기존 가상환경이 호환되지 않아 $backup 으로 옮깁니다."
  mv .venv "$backup"
fi

if [[ ! -d .venv ]]; then
  echo "$($python_bin --version)으로 새 가상환경을 만듭니다."
  "$python_bin" -m venv .venv
fi

if [[ ! -x .venv/bin/python || ! -f .venv/bin/activate ]]; then
  echo "가상환경 생성에 실패했습니다."
  exit 1
fi

. .venv/bin/activate
echo "가상환경 준비 완료: $(python --version) ($(command -v python))"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
npm ci --ignore-scripts --no-audit --no-fund

if [[ ! -f .env ]]; then
  cp .env.example .env
fi

echo
echo "설치 완료. 먼저 데모를 보려면: bash scripts/run.sh"
echo "Blink 계정을 연결하려면: bash scripts/connect_blink.sh"
