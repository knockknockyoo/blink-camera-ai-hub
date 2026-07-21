from __future__ import annotations

import asyncio
from dataclasses import replace

from backend.config import settings
from backend.service import MonitorService


async def run() -> None:
    local_settings = replace(settings, demo_mode=True)
    monitor = MonitorService(local_settings)
    monitor.db.clear_analysis()
    result = await monitor.scan()
    print(result)


if __name__ == "__main__":
    asyncio.run(run())
