from __future__ import annotations

import asyncio

from .blink_client import interactive_setup
from .config import settings


if __name__ == "__main__":
    asyncio.run(interactive_setup(settings.auth_file))
