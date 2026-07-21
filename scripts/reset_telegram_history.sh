#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

DB_FILE="data/sentinel.db"
SKIP_CONFIRM=false

usage() {
  echo "사용법: bash scripts/reset_telegram_history.sh [--yes]"
  echo ""
  echo "Telegram 연결정보는 유지하고 이벤트별 발송 완료 기록만 삭제합니다."
  echo "--yes  확인 질문을 생략합니다."
}

case "${1:-}" in
  "") ;;
  --yes) SKIP_CONFIRM=true ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    usage
    exit 2
    ;;
esac

if curl -fsS --max-time 1 http://127.0.0.1:8787/api/health >/dev/null 2>&1; then
  echo "오류: Blink Camera AI Hub 백엔드가 실행 중입니다."
  echo "실행 중인 터미널에서 Ctrl+C로 종료한 뒤 다시 실행하세요."
  exit 1
fi

if [[ ! -f "$DB_FILE" ]]; then
  echo "오류: DB 파일을 찾을 수 없습니다: $DB_FILE"
  echo "BlinkSentinel-M1 프로젝트 폴더의 스크립트를 실행하는지 확인하세요."
  exit 1
fi

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "오류: sqlite3 명령을 찾을 수 없습니다."
  exit 1
fi

record_count="$(
  sqlite3 -readonly "$DB_FILE" \
    "SELECT COUNT(*) FROM state WHERE key LIKE 'telegram:%' AND key <> 'telegram:initialized';"
)"

echo "현재 Telegram 발송 완료 기록: ${record_count}개"
if [[ "$record_count" == "0" ]]; then
  echo "삭제할 발송 완료 기록이 없습니다."
  exit 0
fi

echo "주의: 기록을 삭제하면 다음 스캔에서 기존 감지 이벤트가 다시 발송될 수 있습니다."
if [[ "$SKIP_CONFIRM" != true ]]; then
  read -r -p "계속하려면 yes를 입력하세요: " answer
  if [[ "$answer" != "yes" ]]; then
    echo "취소했습니다."
    exit 0
  fi
fi

stamp="$(date +%Y%m%d-%H%M%S)"
backup="data/sentinel.db.before-telegram-reset-${stamp}.bak"
cp "$DB_FILE" "$backup"

sqlite3 "$DB_FILE" <<'SQL'
BEGIN IMMEDIATE;
INSERT INTO state(key, value)
VALUES('telegram:initialized', datetime('now'))
ON CONFLICT(key) DO NOTHING;
DELETE FROM state
WHERE key LIKE 'telegram:%'
  AND key <> 'telegram:initialized';
COMMIT;
SQL

echo "완료: Telegram 발송 완료 기록 ${record_count}개를 삭제했습니다."
echo "DB 백업: $backup"
echo "다시 실행하세요: bash scripts/run.sh"
