from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


VIDEO_DATE_PATTERNS = (
    re.compile(r"(?P<date>\d{4}[-_]\d{2}[-_]\d{2})[T_ -](?P<time>\d{2}[-_:]\d{2}[-_:]\d{2})"),
    re.compile(r"(?P<date>\d{8})[-_](?P<time>\d{6})"),
)


def video_timestamp(path: Path, timezone_name: str) -> datetime:
    """Read the capture timestamp from a Blink Camera AI Hub filename or mtime."""
    timezone = ZoneInfo(timezone_name)
    for pattern in VIDEO_DATE_PATTERNS:
        match = pattern.search(path.stem)
        if not match:
            continue
        digits = re.sub(r"\D", "", match.group("date") + match.group("time"))
        try:
            return datetime.strptime(digits, "%Y%m%d%H%M%S").replace(tzinfo=timezone)
        except ValueError:
            continue
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone)


def delete_expired_videos(
    directories: tuple[Path, ...],
    cutoff: datetime,
    timezone_name: str,
) -> dict[str, int]:
    """Delete MP4 files older than cutoff only from configured video folders."""
    deleted = 0
    bytes_freed = 0
    failed = 0
    for directory in directories:
        if not directory.exists():
            continue
        for path in directory.rglob("*.mp4"):
            try:
                captured_at = video_timestamp(path, timezone_name)
                if captured_at.astimezone(cutoff.tzinfo) >= cutoff:
                    continue
                size = path.stat().st_size
                path.unlink()
                deleted += 1
                bytes_freed += size
            except OSError:
                failed += 1
    return {"files": deleted, "bytes": bytes_freed, "failed": failed}
