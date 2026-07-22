#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -d .venv ]]; then
  echo "Run bash scripts/setup.sh first."
  exit 1
fi

. .venv/bin/activate

export MPLCONFIGDIR="${MPLCONFIGDIR:-$PWD/data/matplotlib}"
export YOLO_CONFIG_DIR="${YOLO_CONFIG_DIR:-$PWD/data/ultralytics}"
mkdir -p "$MPLCONFIGDIR" "$YOLO_CONFIG_DIR"

if [[ ! -f data/blink-auth.json ]]; then
  export DEMO_MODE="${DEMO_MODE:-true}"
  echo "Blink is not authenticated, so the application will start with demo data."
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
  echo "Using the Blink Camera AI Hub backend that is already running."
else
  echo "Starting the Blink Camera AI Hub backend at http://127.0.0.1:8787."
  uvicorn backend.main:app --host 127.0.0.1 --port 8787 &
  backend_pid=$!
  backend_owned=true
  backend_ready=false
  for _ in {1..40}; do
    if ! kill -0 "$backend_pid" 2>/dev/null; then
      echo "The backend did not start. Review the error messages above."
      exit 1
    fi
    if curl -fsS http://127.0.0.1:8787/api/health >/dev/null 2>&1; then
      backend_ready=true
      break
    fi
    sleep 0.25
  done

  if [[ "$backend_ready" != true ]]; then
    echo "The backend health check did not succeed within 10 seconds."
    exit 1
  fi
fi

if curl -fsS http://localhost:3000/ >/dev/null 2>&1; then
  echo "The dashboard is already running at http://localhost:3000."
  if [[ "$backend_owned" == true ]]; then
    wait "$backend_pid"
  fi
else
  echo "The backend is ready. Starting the dashboard at http://localhost:3000."
  nohup npm run dev
fi
