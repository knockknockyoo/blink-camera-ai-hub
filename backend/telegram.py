from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp


LOGGER = logging.getLogger("blink-camera-ai-hub")
MAX_VIDEO_BYTES = 50 * 1024 * 1024
KIND_NAMES = {
    "person": "사람",
    "motion": "이상 움직임",
    "vehicle": "차량",
}


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        timezone_name: str,
        protect_content: bool = True,
    ):
        self.bot_token = bot_token.strip()
        self.chat_id = chat_id.strip()
        self.timezone_name = timezone_name
        self.protect_content = protect_content

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def caption(self, event: dict) -> str:
        captured = datetime.fromisoformat(event["started_at"]).astimezone(
            ZoneInfo(self.timezone_name)
        )
        kind = KIND_NAMES.get(event["kind"], event["kind"])
        labels = ", ".join(
            f"{name} {count}" for name, count in event.get("labels", {}).items()
        )
        anomaly = "\n⚠️ 이상징후 감지" if event.get("anomaly") else ""
        details = f"\n감지: {labels}" if labels else ""
        return (
            f"🚨 Blink Camera AI Hub · {kind}\n"
            f"카메라 {event['camera']} · {captured:%Y-%m-%d %H:%M:%S KST}"
            f"{details}{anomaly}"
        )

    async def send_event(self, event: dict) -> bool:
        if not self.configured or not event.get("video_path"):
            return False
        path = Path(event["video_path"])
        if not path.exists():
            LOGGER.error("[Telegram] 영상 파일 없음: %s", path)
            return False
        if path.stat().st_size > MAX_VIDEO_BYTES:
            LOGGER.error("[Telegram] 영상이 50MB를 초과해 발송하지 않음: %s", path.name)
            return False

        form = aiohttp.FormData()
        form.add_field("chat_id", self.chat_id)
        form.add_field("caption", self.caption(event))
        form.add_field("supports_streaming", "true")
        form.add_field("protect_content", "true" if self.protect_content else "false")
        url = f"https://api.telegram.org/bot{self.bot_token}/sendVideo"
        try:
            timeout = aiohttp.ClientTimeout(total=180)
            with path.open("rb") as video:
                form.add_field(
                    "video",
                    video,
                    filename=path.name,
                    content_type="video/mp4",
                )
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, data=form) as response:
                        result = await response.json(content_type=None)
        except Exception:
            LOGGER.exception("[Telegram] 발송 요청 실패")
            return False
        if response.status != 200 or not result.get("ok"):
            LOGGER.error(
                "[Telegram] 발송 실패: HTTP=%s description=%s",
                response.status,
                result.get("description", "unknown"),
            )
            return False
        LOGGER.info("[Telegram] 영상 발송 완료: %s", path.name)
        return True
