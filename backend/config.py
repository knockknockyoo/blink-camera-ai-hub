from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.getenv("DATA_DIR", ROOT / "data"))
    scan_interval_seconds: int = int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))
    analysis_interval_seconds: int = int(
        os.getenv("ANALYSIS_INTERVAL_SECONDS", "300")
    )
    merge_window_seconds: int = int(os.getenv("MERGE_WINDOW_SECONDS", "120"))
    video_retention_days: int = int(os.getenv("VIDEO_RETENTION_DAYS", "90"))
    camera_filter: str = os.getenv("BLINK_CAMERA", "all")
    blink_clip_timeout_seconds: float = float(
        os.getenv("BLINK_CLIP_TIMEOUT_SECONDS", "90")
    )
    blink_metadata_timeout_seconds: float = float(
        os.getenv("BLINK_METADATA_TIMEOUT_SECONDS", "120")
    )
    blink_download_retries: int = int(os.getenv("BLINK_DOWNLOAD_RETRIES", "1"))
    blink_max_clips_per_scan: int = int(os.getenv("BLINK_MAX_CLIPS_PER_SCAN", "20"))
    blink_download_delay_seconds: float = float(
        os.getenv("BLINK_DOWNLOAD_DELAY_SECONDS", "5")
    )
    blink_backlog_retry_seconds: float = float(
        os.getenv("BLINK_BACKLOG_RETRY_SECONDS", "15")
    )
    blink_scan_overlap_seconds: int = int(
        os.getenv("BLINK_SCAN_OVERLAP_SECONDS", "900")
    )
    model_name: str = os.getenv("MODEL_NAME", "yolo11n.pt")
    confidence: float = float(os.getenv("DETECTION_CONFIDENCE", "0.15"))
    sample_fps: float = float(os.getenv("SAMPLE_FPS", "5"))
    person_min_area: float = float(os.getenv("PERSON_MIN_AREA", "0.004"))
    person_min_box_motion: float = float(os.getenv("PERSON_MIN_BOX_MOTION", "0.06"))
    vehicle_min_box_motion: float = float(os.getenv("VEHICLE_MIN_BOX_MOTION", "0.01"))
    vehicle_min_sharpness: float = float(os.getenv("VEHICLE_MIN_SHARPNESS", "700"))
    camera_timezone: str = os.getenv("CAMERA_TIMEZONE", "Asia/Seoul")
    keep_unknown_motion: bool = _bool("KEEP_UNKNOWN_MOTION", False)
    demo_mode: bool = _bool("DEMO_MODE", False)
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    telegram_protect_content: bool = _bool("TELEGRAM_PROTECT_CONTENT", True)

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def event_dir(self) -> Path:
        return self.data_dir / "events"

    @property
    def rejected_dir(self) -> Path:
        return self.data_dir / "rejected"

    @property
    def auth_file(self) -> Path:
        return self.data_dir / "blink-auth.json"

    @property
    def db_file(self) -> Path:
        return self.data_dir / "sentinel.db"

    def ensure_dirs(self) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.event_dir.mkdir(parents=True, exist_ok=True)
        self.rejected_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
