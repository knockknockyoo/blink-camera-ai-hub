from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp


LOGGER = logging.getLogger("blink-camera-ai-hub")
MAX_VIDEO_BYTES = 50 * 1024 * 1024
KIND_NAMES = {
    "person": "Person",
    "motion": "Unclassified motion",
    "vehicle": "Vehicle",
}
MODEL_NAMES = {
    "yolo": "YOLO",
    "moondream2": "Moondream2",
}
MODEL_STATUS = {
    "positive": "✅",
    "negative": "❌",
    "error": "⚠️",
    "pending": "⏳",
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
        anomaly = "\n⚠️ Anomaly detected" if event.get("anomaly") else ""
        details = f"\nDetected: {labels}" if labels else ""
        votes = event.get("model_votes", {})
        model_details = ""
        if votes:
            statuses = " · ".join(
                f"{MODEL_NAMES[name]} {MODEL_STATUS.get(votes.get(name, {}).get('status'), '⚠️')}"
                for name in MODEL_NAMES
                if name in votes
            )
            detected_by = [
                MODEL_NAMES[name]
                for name in MODEL_NAMES
                if votes.get(name, {}).get("status") == "positive"
            ]
            decision = (
                f"Positive ({', '.join(detected_by)})"
                if detected_by
                else "Negative"
            )
            model_details = f"\nAI: {statuses}\nDecision: {decision}"
        return (
            f"🚨 Blink Camera AI Hub · {kind}\n"
            f"Camera {event['camera']} · {captured:%Y-%m-%d %H:%M:%S %Z}"
            f"{details}{model_details}{anomaly}"
        )

    async def send_event_message(self, event: dict) -> int | None:
        if not self.configured or not event.get("video_path"):
            return None
        path = Path(event["video_path"])
        if not path.exists():
            LOGGER.error("[Telegram] Video file not found: %s", path)
            return None
        if path.stat().st_size > MAX_VIDEO_BYTES:
            LOGGER.error("[Telegram] Video exceeds 50 MB and will not be sent: %s", path.name)
            return None

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
            LOGGER.exception("[Telegram] Send request failed")
            return None
        if response.status != 200 or not result.get("ok"):
            LOGGER.error(
                "[Telegram] Send failed: HTTP=%s description=%s",
                response.status,
                result.get("description", "unknown"),
            )
            return None
        LOGGER.info("[Telegram] Video sent: %s", path.name)
        return int(result.get("result", {}).get("message_id", 0))

    async def send_event(self, event: dict) -> bool:
        return await self.send_event_message(event) is not None

    async def edit_event_caption(self, message_id: int, event: dict) -> bool:
        if not self.configured or not message_id:
            return False
        url = f"https://api.telegram.org/bot{self.bot_token}/editMessageCaption"
        payload = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "caption": self.caption(event),
        }
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, data=payload) as response:
                    result = await response.json(content_type=None)
        except Exception:
            LOGGER.exception("[Telegram] Caption update request failed")
            return False
        if response.status != 200 or not result.get("ok"):
            LOGGER.error(
                "[Telegram] Caption update failed: HTTP=%s description=%s",
                response.status,
                result.get("description", "unknown"),
            )
            return False
        LOGGER.info("[Telegram] Model votes updated for message %s", message_id)
        return True
