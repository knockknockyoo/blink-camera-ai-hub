from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from backend.config import settings
from backend.service import MonitorService


async def run(hours: float) -> None:
    monitor = MonitorService(settings)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    result = await monitor.scan(since_override=since)
    print(result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scan Blink clips from a specified time range.")
    parser.add_argument("--hours", type=float, default=3)
    args = parser.parse_args()
    asyncio.run(run(args.hours))
