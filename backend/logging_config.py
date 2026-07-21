from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(data_dir: Path) -> logging.Logger:
    logger = logging.getLogger("blink-camera-ai-hub")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if getattr(logger, "_blink_ai_hub_configured", False):
        return logger

    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    logfile = RotatingFileHandler(
        log_dir / "blink-camera-ai-hub.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    logfile.setFormatter(formatter)
    logger.addHandler(logfile)
    logger._blink_ai_hub_configured = True  # type: ignore[attr-defined]
    return logger
