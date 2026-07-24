#!/usr/bin/env python3
"""Remove derived videos/results and make Blink Camera AI Hub re-scan recent clips."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_FILE = DATA_DIR / "sentinel.db"
VIDEO_DIRS = (DATA_DIR / "raw", DATA_DIR / "rejected", DATA_DIR / "events")


def backend_is_running() -> bool:
    try:
        with urlopen("http://127.0.0.1:8787/api/health", timeout=0.5) as response:
            return response.status == 200
    except (URLError, TimeoutError, OSError):
        return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remove existing videos and analysis results, then prepare to analyze recent clips again."
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=24,
        help="Time range to download again. Default: 24 hours.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the deletion confirmation prompt.",
    )
    parser.add_argument(
        "--keep-telegram-history",
        action="store_true",
        help="Deprecated compatibility option; Telegram history is preserved by default.",
    )
    parser.add_argument(
        "--resend-telegram",
        action="store_true",
        help="Explicitly clear delivery history so reanalyzed videos may be sent again.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.hours <= 0 or args.hours > 72:
        print("Error: --hours must be greater than 0 and no more than 72.")
        return 2

    if backend_is_running():
        print("Error: Blink Camera AI Hub is running. Stop it with Ctrl+C and try again.")
        return 1

    if not DB_FILE.exists():
        print(f"Error: Database file not found: {DB_FILE}")
        print("Run this script from the Blink Camera AI Hub project directory.")
        return 1

    if not args.yes:
        print("Existing raw, rejected, and event videos and analysis results will be deleted.")
        print("Blink authentication, .env, and Telegram connection settings will be preserved.")
        if args.resend_telegram:
            print("Events detected again during reanalysis will be sent to Telegram again.")
        answer = input("Type yes to continue: ").strip().lower()
        if answer != "yes":
            print("Cancelled.")
            return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DATA_DIR / f"sentinel.db.before-reanalysis-{stamp}.bak"
    shutil.copy2(DB_FILE, backup)

    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    with sqlite3.connect(DB_FILE) as connection:
        connection.execute("DELETE FROM events")
        connection.execute("DELETE FROM clips")
        connection.execute("DELETE FROM state WHERE key LIKE 'ai:moondream:%'")
        if args.resend_telegram:
            connection.execute("DELETE FROM state WHERE key LIKE 'telegram:%'")
        else:
            connection.execute(
                "DELETE FROM state WHERE key LIKE 'telegram:clip-pending:%'"
            )
            connection.execute(
                "DELETE FROM state WHERE key LIKE 'telegram:clip-sent:%'"
            )
            connection.execute(
                "DELETE FROM state WHERE key LIKE 'telegram:clip-message:%'"
            )
            connection.execute(
                "DELETE FROM state WHERE key LIKE 'telegram:file-message:%'"
            )
        connection.execute(
            """
            INSERT INTO state(key, value) VALUES('last_scan', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (since.isoformat(),),
        )
        connection.commit()

    deleted = 0
    for directory in VIDEO_DIRS:
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            continue
        for path in directory.rglob("*"):
            if path.is_file():
                path.unlink()
                deleted += 1

    print(f"Complete: deleted {deleted} existing video files and analysis records.")
    print(f"Database backup: {backup}")
    print(f"The next run will scan the most recent {args.hours:g} hours.")
    if args.resend_telegram:
        print("Eligible events detected again will be sent to Telegram again.")
    else:
        print("Telegram delivery history was preserved; identical filenames will not be sent twice.")
    print("Start now with: bash scripts/run.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
