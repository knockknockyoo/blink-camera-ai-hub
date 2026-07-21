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
        description="기존 영상과 분석 결과를 지우고 최근 영상을 다시 분석하도록 준비합니다."
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=24,
        help="다시 다운로드할 시간 범위입니다. 기본값: 24시간",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="삭제 확인 질문을 생략합니다.",
    )
    parser.add_argument(
        "--keep-telegram-history",
        action="store_true",
        help="재분석된 사건을 텔레그램으로 다시 보내지 않습니다.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.hours <= 0 or args.hours > 72:
        print("오류: --hours는 0보다 크고 72 이하여야 합니다.")
        return 2

    if backend_is_running():
        print("오류: Blink Camera AI Hub가 실행 중입니다. Ctrl+C로 종료한 후 다시 실행하세요.")
        return 1

    if not DB_FILE.exists():
        print(f"오류: DB 파일을 찾을 수 없습니다: {DB_FILE}")
        print("BlinkSentinel-M1 폴더 안에서 이 스크립트를 실행하는지 확인하세요.")
        return 1

    if not args.yes:
        print("기존 raw/rejected/events 영상과 분석 결과를 삭제합니다.")
        print("Blink 인증, .env, 텔레그램 연결 설정은 보존됩니다.")
        if not args.keep_telegram_history:
            print("재분석에서 다시 검출된 사건은 텔레그램으로 재발송됩니다.")
        answer = input("계속하려면 yes를 입력하세요: ").strip().lower()
        if answer != "yes":
            print("취소했습니다.")
            return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DATA_DIR / f"sentinel.db.before-reanalysis-{stamp}.bak"
    shutil.copy2(DB_FILE, backup)

    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    with sqlite3.connect(DB_FILE) as connection:
        connection.execute("DELETE FROM events")
        connection.execute("DELETE FROM clips")
        if not args.keep_telegram_history:
            connection.execute("DELETE FROM state WHERE key LIKE 'telegram:%'")
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

    print(f"완료: 기존 영상 파일 {deleted}개와 분석 기록을 삭제했습니다.")
    print(f"DB 백업: {backup}")
    print(f"다음 실행에서 최근 {args.hours:g}시간 영상을 다시 확인합니다.")
    if args.keep_telegram_history:
        print("기존 텔레그램 발송 기록을 유지했습니다.")
    else:
        print("다시 검출된 알림 대상 사건은 텔레그램으로 재발송됩니다.")
    print("이제 실행하세요: bash scripts/run.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
