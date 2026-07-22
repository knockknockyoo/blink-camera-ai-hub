#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

DB_FILE="data/sentinel.db"
SKIP_CONFIRM=false

usage() {
  echo "Usage: bash scripts/reset_telegram_history.sh [--yes]"
  echo ""
  echo "Preserve Telegram connection settings and delete only per-event delivery records."
  echo "--yes  Skip the confirmation prompt."
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
  echo "Error: The Blink Camera AI Hub backend is running."
  echo "Stop it with Ctrl+C in its terminal, then try again."
  exit 1
fi

if [[ ! -f "$DB_FILE" ]]; then
  echo "Error: Database file not found: $DB_FILE"
  echo "Run this script from the Blink Camera AI Hub project directory."
  exit 1
fi

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "Error: sqlite3 was not found."
  exit 1
fi

record_count="$(
  sqlite3 -readonly "$DB_FILE" \
    "SELECT COUNT(*) FROM state WHERE key LIKE 'telegram:%' AND key <> 'telegram:initialized';"
)"

echo "Current Telegram delivery records: ${record_count}"
if [[ "$record_count" == "0" ]]; then
  echo "There are no delivery records to delete."
  exit 0
fi

echo "Warning: Existing detection events may be sent again after these records are deleted."
if [[ "$SKIP_CONFIRM" != true ]]; then
  read -r -p "Type yes to continue: " answer
  if [[ "$answer" != "yes" ]]; then
    echo "Cancelled."
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

echo "Complete: deleted ${record_count} Telegram delivery records."
echo "Database backup: $backup"
echo "Restart with: bash scripts/run.sh"
