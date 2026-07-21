#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -d .venv ]]; then
  echo "먼저 bash scripts/setup.sh를 실행하세요."
  exit 1
fi

. .venv/bin/activate

export MPLCONFIGDIR="${MPLCONFIGDIR:-$PWD/data/matplotlib}"
export YOLO_CONFIG_DIR="${YOLO_CONFIG_DIR:-$PWD/data/ultralytics}"
mkdir -p "$MPLCONFIGDIR" "$YOLO_CONFIG_DIR"

if [[ ! -f data/blink-auth.json ]]; then
  export DEMO_MODE="${DEMO_MODE:-true}"
  echo "Blink 인증 전이므로 데모 데이터로 시작합니다."
fi

backend_pid=""
backend_owned=false

# Keep the service alive when the SSH session or controlling terminal closes.
# INT and TERM are still handled below, so Ctrl+C and normal shutdown work.
trap '' HUP

cleanup() {
  if [[ "$backend_owned" == true && -n "$backend_pid" ]]; then
    kill "$backend_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if curl -fsS http://127.0.0.1:8787/api/health >/dev/null 2>&1; then
  echo "이미 실행 중인 Blink Camera AI Hub 백엔드를 사용합니다."
else
  echo "Blink Camera AI Hub 백엔드를 시작합니다 (http://127.0.0.1:8787)."
  uvicorn backend.main:app --host 127.0.0.1 --port 8787 &
  backend_pid=$!
  backend_owned=true
  backend_ready=false
  for _ in {1..40}; do
    if ! kill -0 "$backend_pid" 2>/dev/null; then
      echo "백엔드가 시작되지 않았습니다. 위 오류 메시지를 확인하세요."
      exit 1
    fi
    if curl -fsS http://127.0.0.1:8787/api/health >/dev/null 2>&1; then
      backend_ready=true
      break
    fi
    sleep 0.25
  done

  if [[ "$backend_ready" != true ]]; then
    echo "10초 안에 백엔드 상태 확인에 실패했습니다."
    exit 1
  fi
fi

if curl -fsS http://localhost:3000/ >/dev/null 2>&1; then
  echo "화면도 이미 실행 중입니다: http://localhost:3000"
  if [[ "$backend_owned" == true ]]; then
    wait "$backend_pid"
  fi
else
  echo "백엔드 준비 완료. 화면을 시작합니다 (http://localhost:3000)."
  nohup npm run dev
fi
