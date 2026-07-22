from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from .analyzer import VideoAnalyzer
from .blink_client import BlinkDownloader
from .config import ROOT, Settings
from .db import Database
from .events import build_event, concatenate, merge_clips, should_keep
from .logging_config import configure_logging
from .retention import delete_expired_videos
from .telegram import TelegramNotifier


LOGGER = logging.getLogger("blink-camera-ai-hub")
DATE_PATTERNS = (
    re.compile(r"(?P<date>\d{4}[-_]\d{2}[-_]\d{2})[T_ -](?P<time>\d{2}[-_:]\d{2}[-_:]\d{2})"),
    re.compile(r"(?P<date>\d{8})[-_](?P<time>\d{6})"),
)


def infer_metadata(path: Path) -> tuple[str, datetime]:
    stem = path.stem
    local_timezone = datetime.now().astimezone().tzinfo
    captured = datetime.fromtimestamp(path.stat().st_mtime, tz=local_timezone)
    for pattern in DATE_PATTERNS:
        match = pattern.search(stem)
        if not match:
            continue
        digits = re.sub(r"\D", "", match.group("date") + match.group("time"))
        try:
            captured = datetime.strptime(digits, "%Y%m%d%H%M%S").replace(tzinfo=local_timezone)
        except ValueError:
            pass
        break
    camera = path.parent.name if path.parent.name not in {"raw", "videos"} else "Outdoor"
    return camera, captured


class MonitorService:
    def __init__(self, settings: Settings):
        settings.ensure_dirs()
        configure_logging(settings.data_dir)
        self.settings = settings
        self.db = Database(settings.db_file)
        self.downloader = BlinkDownloader(
            settings.auth_file,
            settings.raw_dir,
            settings.camera_filter,
            download_retries=settings.blink_download_retries,
            download_delay_seconds=settings.blink_download_delay_seconds,
            clip_timeout_seconds=settings.blink_clip_timeout_seconds,
            max_clips_per_scan=settings.blink_max_clips_per_scan,
        )
        self.analyzer = VideoAnalyzer(
            settings.model_name,
            settings.confidence,
            settings.sample_fps,
            settings.camera_timezone,
            settings.person_min_area,
            settings.person_min_box_motion,
            settings.vehicle_min_box_motion,
            settings.vehicle_min_sharpness,
        )
        self.telegram = TelegramNotifier(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            settings.camera_timezone,
            settings.telegram_protect_content,
        )
        self._initialize_telegram_history()
        self._lock = asyncio.Lock()
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self.last_error: str | None = None
        self.progress: dict[str, Any] = {
            "phase": "idle",
            "current": 0,
            "total": 0,
            "file": None,
        }

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="blink-monitor")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        self._running = True
        while self._running:
            started_at = time.monotonic()
            try:
                await self.scan()
            except Exception as exc:  # Keep the scheduler alive after recoverable Blink/network errors.
                self.last_error = str(exc)
                self.progress.update(phase="error", file=str(exc))
                LOGGER.exception("scan failed")
            elapsed = time.monotonic() - started_at
            delay = max(1.0, self.settings.scan_interval_seconds - elapsed)
            LOGGER.info(
                "[Next scan] Starting in %.0fs (current scan %.1fs)",
                delay,
                elapsed,
            )
            await asyncio.sleep(delay)

    async def scan(self, since_override: datetime | None = None) -> dict[str, int]:
        if self._lock.locked():
            return {"downloaded": 0, "analyzed": 0, "events": 0}
        async with self._lock:
            LOGGER.info("[Scan started]")
            await self.cleanup_expired_media()
            downloaded = 0
            if self.downloader.configured and not self.settings.demo_mode:
                last = self.db.get_state("last_scan")
                since = since_override or (datetime.fromisoformat(last) if last else None)
                self.progress.update(phase="downloading", current=0, total=0, file=None)
                downloaded = await self.downloader.download_new(since)

            analyzed = 0
            cameras: set[str] = set()
            pending = [
                path
                for path in sorted(self.settings.raw_dir.rglob("*.mp4"))
                if not self.db.clip_exists(path)
            ]
            LOGGER.info("[AI analysis] %d clips pending", len(pending))
            self.progress.update(phase="analyzing", current=0, total=len(pending), file=None)
            for index, path in enumerate(pending, start=1):
                self.progress.update(current=index, file=path.name)
                LOGGER.info("[AI analysis %d/%d] Started: %s", index, len(pending), path.name)
                camera, captured_at = infer_metadata(path)
                result = await asyncio.to_thread(self.analyzer.analyze, path, captured_at)
                result.update(
                    {
                        "path": str(path.resolve()),
                        "camera": camera,
                        "captured_at": captured_at.isoformat(),
                    }
                )
                self.db.add_clip(result)
                cameras.add(camera)
                analyzed += 1
                LOGGER.info(
                    "[AI analysis %d/%d] Complete: labels=%s score=%.3f motion=%.4f anomaly=%s",
                    index,
                    len(pending),
                    result.get("labels", {}),
                    result.get("score", 0),
                    result.get("motion_score", 0),
                    result.get("anomaly", False),
                )

            for camera in cameras:
                self.progress.update(phase="events", current=0, total=len(cameras), file=camera)
                LOGGER.info("[Event creation] camera=%s", camera)
                await asyncio.to_thread(self.rebuild_events, camera)

            await self.notify_telegram(self.db.list_events(limit=500))

            now = datetime.now(timezone.utc).isoformat()
            if self.downloader.incomplete_downloads:
                LOGGER.warning(
                    "[Scan] Keeping last_scan unchanged so failed clips can be retried"
                )
            else:
                self.db.set_state("last_scan", now)
            self.last_error = None
            event_count = len(self.db.list_events())
            self.progress.update(phase="idle", current=0, total=0, file=None)
            LOGGER.info(
                "[Scan complete] downloaded=%d analyzed=%d events=%d",
                downloaded,
                analyzed,
                event_count,
            )
            return {"downloaded": downloaded, "analyzed": analyzed, "events": event_count}

    async def cleanup_expired_media(self) -> None:
        """Remove expired videos and metadata at most once per day."""
        retention_days = max(0, self.settings.video_retention_days)
        if retention_days == 0:
            return

        now = datetime.now(timezone.utc)
        last_value = self.db.get_state("retention:last_cleanup")
        if last_value:
            try:
                last_cleanup = datetime.fromisoformat(last_value)
                if last_cleanup.tzinfo is None:
                    last_cleanup = last_cleanup.replace(tzinfo=timezone.utc)
                if now - last_cleanup.astimezone(timezone.utc) < timedelta(days=1):
                    return
            except ValueError:
                pass

        cutoff = now - timedelta(days=retention_days)
        video_stats = await asyncio.to_thread(
            delete_expired_videos,
            (
                self.settings.raw_dir,
                self.settings.rejected_dir,
                self.settings.event_dir,
            ),
            cutoff,
            self.settings.camera_timezone,
        )
        db_stats = await asyncio.to_thread(self.db.delete_before, cutoff)
        self.db.set_state("retention:last_cleanup", now.isoformat())

        if video_stats["files"] or db_stats["clips"] or db_stats["events"]:
            LOGGER.info(
                "[Retention cleanup] Deleted %d files older than %d days (%.1f MB), "
                "removed %d clips and %d events from the database",
                video_stats["files"],
                retention_days,
                video_stats["bytes"] / (1024 * 1024),
                db_stats["clips"],
                db_stats["events"],
            )
        if video_stats["failed"]:
            LOGGER.warning(
                "[Retention cleanup] Failed to delete %d files; retrying during the next cleanup",
                video_stats["failed"],
            )

    def rebuild_events(self, camera: str) -> list[dict[str, Any]]:
        clips = self.db.recent_clips(camera, limit=5000)
        events: list[dict[str, Any]] = []
        for group in merge_clips(clips, self.settings.merge_window_seconds):
            event = build_event(group)
            if not should_keep(event, self.settings.keep_unknown_motion):
                continue
            stamp = datetime.fromisoformat(event["started_at"]).strftime("%Y%m%d-%H%M%S")
            output = self.settings.event_dir / camera / f"{stamp}-{event['kind']}.mp4"
            video = concatenate([Path(path) for path in event.pop("source_paths")], output)
            event["video_path"] = str(video.resolve()) if video else None
            events.append(event)
        self.db.replace_events(camera, events)
        self.archive_obsolete_event_videos(camera, events)
        self.archive_unneeded(camera, clips, events)
        return events

    def _telegram_key(self, event: dict[str, Any]) -> str:
        return f"telegram:{event['camera']}:{event['started_at']}"

    def _initialize_telegram_history(self) -> None:
        if not self.telegram.configured or self.db.get_state("telegram:initialized"):
            return
        for event in self.db.list_events(limit=500):
            self.db.set_state(self._telegram_key(event), "existing")
        self.db.set_state("telegram:initialized", datetime.now(timezone.utc).isoformat())
        LOGGER.info("[Telegram] Existing events excluded from notifications")

    def reload_telegram_settings(self) -> bool:
        values = dotenv_values(ROOT / ".env")
        protect = str(values.get("TELEGRAM_PROTECT_CONTENT", "true")).lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.telegram = TelegramNotifier(
            str(values.get("TELEGRAM_BOT_TOKEN") or ""),
            str(values.get("TELEGRAM_CHAT_ID") or ""),
            self.settings.camera_timezone,
            protect,
        )
        self._initialize_telegram_history()
        LOGGER.info("[Telegram] Settings reloaded: configured=%s", self.telegram.configured)
        return self.telegram.configured

    async def notify_telegram(self, events: list[dict[str, Any]]) -> None:
        if not self.telegram.configured:
            return
        for event in events:
            notification_key = self._telegram_key(event)
            if self.db.get_state(notification_key):
                continue
            self.progress.update(phase="notifying", file=event.get("video_path"))
            LOGGER.info(
                "[Telegram] Sending: camera=%s kind=%s started=%s",
                event["camera"],
                event["kind"],
                event["started_at"],
            )
            if await self.telegram.send_event(event):
                self.db.set_state(notification_key, datetime.now(timezone.utc).isoformat())

    def archive_obsolete_event_videos(
        self,
        camera: str,
        events: list[dict[str, Any]],
    ) -> None:
        active_paths = {
            Path(event["video_path"]).resolve()
            for event in events
            if event.get("video_path")
        }
        event_camera_dir = self.settings.event_dir / camera
        for source in event_camera_dir.glob("*.mp4"):
            if source.resolve() in active_paths:
                continue
            destination_dir = self.settings.rejected_dir / camera / "events"
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = destination_dir / source.name
            suffix = 1
            while destination.exists():
                destination = destination_dir / f"{source.stem}-{suffix}{source.suffix}"
                suffix += 1
            shutil.move(str(source), str(destination))

    def archive_unneeded(
        self,
        camera: str,
        clips: list[dict[str, Any]],
        events: list[dict[str, Any]],
    ) -> None:
        kept_ids = {clip_id for event in events for clip_id in event["clip_ids"]}
        for clip in clips:
            if clip["id"] in kept_ids:
                continue
            source = Path(clip["path"])
            if not source.exists():
                continue
            try:
                source.relative_to(self.settings.raw_dir)
            except ValueError:
                continue
            destination = self.settings.rejected_dir / camera / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                destination = destination.with_name(
                    f"{destination.stem}-{clip['id']}{destination.suffix}"
                )
            shutil.move(str(source), str(destination))
            self.db.update_clip_path(clip["id"], destination.resolve())

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.downloader.configured,
            "scanning": self._lock.locked(),
            "interval_seconds": self.settings.scan_interval_seconds,
            "last_scan": self.db.get_state("last_scan"),
            "last_error": self.last_error,
            "progress": self.progress,
            "telegram_configured": self.telegram.configured,
            "counts": self.db.counts(),
        }
