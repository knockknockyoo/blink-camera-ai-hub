from __future__ import annotations

import asyncio
import json
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
from .ensemble_analyzer import EnsembleVideoAnalyzer
from .events import build_event, concatenate, merge_clips, should_keep
from .logging_config import configure_logging
from .remote_analyzer import RemoteVideoAnalyzer
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
            metadata_timeout_seconds=settings.blink_metadata_timeout_seconds,
            max_clips_per_scan=settings.blink_max_clips_per_scan,
        )
        self.analyzer = self._new_analyzer()
        self.telegram = TelegramNotifier(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            settings.camera_timezone,
            settings.telegram_protect_content,
        )
        self._initialize_telegram_history()
        self._download_lock = asyncio.Lock()
        self._analysis_lock = asyncio.Lock()
        self._notification_lock = asyncio.Lock()
        self._analysis_queue: asyncio.Queue[Path] = asyncio.Queue()
        self._queued_paths: set[Path] = set()
        self._queued_at: dict[Path, float] = {}
        self._active_analyses = 0
        self._analysis_completed = 0
        self._running = False
        self._tasks: list[asyncio.Task[None]] = []
        self._secondary_tasks: set[asyncio.Task[None]] = set()
        self._secondary_clip_ids: set[int] = set()
        self._secondary_semaphore = asyncio.Semaphore(
            max(1, settings.native_ai_concurrency)
        )
        self._dirty_cameras: set[str] = set()
        self.last_download_error: str | None = None
        self.last_analysis_error: str | None = None
        self.last_error: str | None = None
        self.download_progress: dict[str, Any] = {
            "phase": "idle",
            "current": 0,
            "total": 0,
            "file": None,
        }
        self.analysis_progress: dict[str, Any] = dict(self.download_progress)
        self.notification_progress: dict[str, Any] = dict(self.download_progress)
        self.downloader.progress_callback = self._update_download_progress
        self.downloader.downloaded_callback = self._enqueue_analysis

    def _new_analyzer(self) -> VideoAnalyzer | EnsembleVideoAnalyzer:
        yolo = VideoAnalyzer(
            self.settings.model_name,
            self.settings.confidence,
            self.settings.sample_fps,
            self.settings.camera_timezone,
            self.settings.person_min_area,
            self.settings.person_min_box_motion,
            self.settings.vehicle_min_box_motion,
            self.settings.vehicle_min_sharpness,
        )
        if not self.settings.native_ai_url:
            self.ai_backend = "YOLO"
            return yolo
        if not self.settings.native_ai_token:
            raise RuntimeError(
                "NATIVE_AI_TOKEN is required when NATIVE_AI_URL is configured."
            )
        rfdetr = RemoteVideoAnalyzer(
            self.settings.native_ai_url,
            self.settings.native_ai_token,
            self.settings.data_dir,
            self.settings.native_ai_timeout_seconds,
        )
        self.ai_backend = (
            f"YOLO + RF-DETR {self.settings.rfdetr_model_size.title()}"
        )
        return EnsembleVideoAnalyzer(yolo, rfdetr)

    def _update_download_progress(self, **values: Any) -> None:
        self.download_progress.update(values)

    async def start(self) -> None:
        if self._tasks:
            return
        self._running = True
        self._enqueue_pending_files()
        worker_count = max(1, self.settings.ai_worker_count)
        analyzers = [
            self.analyzer,
            *(self._new_analyzer() for _ in range(worker_count - 1)),
        ]
        self._tasks = [
            asyncio.create_task(self._download_loop(), name="blink-downloader"),
            asyncio.create_task(self._notification_loop(), name="telegram-notifier"),
            asyncio.create_task(
                self._analysis_recovery_loop(), name="analysis-queue-recovery"
            ),
            *(
                asyncio.create_task(
                    self._analysis_worker(index, analyzer),
                    name=f"video-analyzer-{index}",
                )
                for index, analyzer in enumerate(analyzers, start=1)
            ),
        ]
        self._recover_secondary_tasks()
        LOGGER.info(
            "[Workers] Downloader, %d fast YOLO workers, one durable RF-DETR queue, and Telegram notifier started",
            worker_count,
        )

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        for task in self._secondary_tasks:
            task.cancel()
        if self._secondary_tasks:
            await asyncio.gather(*self._secondary_tasks, return_exceptions=True)
        self._secondary_tasks.clear()

    def _sync_last_error(self) -> None:
        self.last_error = self.last_download_error or self.last_analysis_error

    async def _download_loop(self) -> None:
        while self._running:
            started_at = time.monotonic()
            delay = float(self.settings.scan_interval_seconds)
            failed = False
            try:
                await self.download_once()
                self.last_download_error = None
                await self._finalize_if_idle()
                if self.downloader.incomplete_downloads:
                    delay = max(1.0, self.settings.blink_backlog_retry_seconds)
            except Exception as exc:  # Recoverable Blink/network failure.
                failed = True
                self.last_download_error = str(exc)
                self.download_progress.update(phase="error", file=str(exc))
                delay = min(60.0, float(self.settings.scan_interval_seconds))
                LOGGER.exception("download scan failed")
            self._sync_last_error()
            elapsed = time.monotonic() - started_at
            if not failed and not self.downloader.incomplete_downloads:
                delay = max(1.0, delay - elapsed)
            LOGGER.info(
                "[Next download scan] Starting in %.0fs (current scan %.1fs, backlog=%s)",
                delay,
                elapsed,
                self.downloader.incomplete_downloads,
            )
            await asyncio.sleep(delay)

    def _enqueue_analysis(self, path: Path) -> None:
        resolved = path.resolve()
        if resolved in self._queued_paths or self.db.clip_exists(resolved):
            return
        self._queued_paths.add(resolved)
        self._queued_at[resolved] = time.monotonic()
        self._analysis_queue.put_nowait(resolved)
        LOGGER.info(
            "[AI queue] Added immediately after download: file=%s queued=%d",
            resolved.name,
            self._analysis_queue.qsize(),
        )

    def _enqueue_pending_files(self) -> int:
        before = self._analysis_queue.qsize()
        for path in sorted(self.settings.raw_dir.rglob("*.mp4")):
            self._enqueue_analysis(path)
        return self._analysis_queue.qsize() - before

    async def _analysis_recovery_loop(self) -> None:
        """Periodically recover files left unqueued after a crash or failed analysis."""
        delay = max(1.0, float(self.settings.analysis_interval_seconds))
        while self._running:
            await asyncio.sleep(delay)
            recovered = self._enqueue_pending_files()
            if recovered:
                LOGGER.info("[AI queue recovery] Requeued %d videos", recovered)

    async def _analysis_worker(
        self,
        worker_id: int,
        analyzer: VideoAnalyzer | EnsembleVideoAnalyzer,
    ) -> None:
        while self._running:
            path = await self._analysis_queue.get()
            self._active_analyses += 1
            self.analysis_progress.update(
                phase="analyzing",
                current=self._active_analyses,
                total=self._active_analyses + self._analysis_queue.qsize(),
                file=path.name,
            )
            LOGGER.info(
                "[AI worker %d] Started: file=%s queue_wait=%.2fs queued=%d active=%d",
                worker_id,
                path.name,
                time.monotonic() - self._queued_at.get(path, time.monotonic()),
                self._analysis_queue.qsize(),
                self._active_analyses,
            )
            try:
                await self._analyze_one(path, analyzer)
                self.last_analysis_error = None
            except Exception as exc:
                self.last_analysis_error = str(exc)
                LOGGER.exception(
                    "[AI worker %d] Failed; recovery scan will retry %s",
                    worker_id,
                    path.name,
                )
            finally:
                self._active_analyses -= 1
                self._queued_paths.discard(path)
                self._queued_at.pop(path, None)
                self._analysis_queue.task_done()
                self._sync_last_error()
                if not self._active_analyses and self._analysis_queue.empty():
                    self.analysis_progress.update(
                        phase="idle", current=0, total=0, file=None
                    )
                    await self._finalize_if_idle()

    async def _finalize_if_idle(self) -> None:
        if (
            self._dirty_cameras
            and not self._active_analyses
            and self._analysis_queue.empty()
            and not self._secondary_tasks
            and not self._download_lock.locked()
        ):
            await self._finalize_dirty_events()

    async def _analyze_one(
        self,
        path: Path,
        analyzer: VideoAnalyzer | EnsembleVideoAnalyzer | None = None,
    ) -> dict[str, Any]:
        analyzer = analyzer or self.analyzer
        if isinstance(analyzer, EnsembleVideoAnalyzer):
            return await self._analyze_primary(path, analyzer)

        started_at = time.monotonic()
        camera, captured_at = infer_metadata(path)
        result = await asyncio.to_thread(analyzer.analyze, path, captured_at)
        result.update(
            {
                "path": str(path.resolve()),
                "camera": camera,
                "captured_at": captured_at.isoformat(),
            }
        )
        clip_id = self.db.add_clip(result)
        result["id"] = clip_id
        self._dirty_cameras.add(camera)
        self._analysis_completed += 1
        LOGGER.info(
            "[AI complete] file=%s elapsed=%.2fs labels=%s models=%s score=%.3f motion=%.4f anomaly=%s",
            path.name,
            time.monotonic() - started_at,
            result.get("labels", {}),
            {
                name: vote.get("status")
                for name, vote in result.get("model_votes", {}).items()
            },
            result.get("score", 0),
            result.get("motion_score", 0),
            result.get("anomaly", False),
        )
        self._queue_clip_notification(result)
        await self._send_pending_notifications()
        return result

    async def _analyze_primary(
        self,
        path: Path,
        analyzer: EnsembleVideoAnalyzer,
    ) -> dict[str, Any]:
        """Finish YOLO immediately and move RF-DETR to its own durable queue."""
        started_at = time.monotonic()
        camera, captured_at = infer_metadata(path)
        yolo = analyzer.analyzers["yolo"]
        secondary_request = asyncio.create_task(
            self._run_secondary_model(
                analyzer.analyzers["rfdetr"],
                path,
                captured_at,
            ),
            name=f"rfdetr-request-{path.name}",
        )
        try:
            primary = await asyncio.to_thread(yolo.analyze, path, captured_at)
        except Exception:
            secondary_request.cancel()
            await asyncio.gather(secondary_request, return_exceptions=True)
            raise
        votes = {
            "yolo": analyzer._vote(primary),
            "rfdetr": {
                "status": "pending",
                "labels": {},
                "score": 0.0,
            },
        }
        result = analyzer._combine({"yolo": primary}, votes)
        result.update(
            {
                "path": str(path.resolve()),
                "camera": camera,
                "captured_at": captured_at.isoformat(),
            }
        )
        clip_id = self.db.add_clip(result)
        result["id"] = clip_id
        payload = {
            "clip_id": clip_id,
            "path": result["path"],
            "camera": camera,
            "captured_at": result["captured_at"],
            "primary_result": primary,
        }
        self.db.set_state(
            self._secondary_state_key(clip_id),
            json.dumps(payload, ensure_ascii=False),
        )
        self._schedule_secondary(
            payload,
            analyzer.analyzers["rfdetr"],
            secondary_request,
        )
        self._analysis_completed += 1
        LOGGER.info(
            "[AI primary complete] file=%s elapsed=%.2fs labels=%s "
            "models={'yolo': '%s', 'rfdetr': 'pending'}",
            path.name,
            time.monotonic() - started_at,
            result.get("labels", {}),
            votes["yolo"]["status"],
        )
        await self._send_initial_model_notification(result)
        return result

    @staticmethod
    def _secondary_state_key(clip_id: int) -> str:
        return f"ai:rfdetr:pending:{clip_id}"

    def _schedule_secondary(
        self,
        payload: dict[str, Any],
        analyzer: RemoteVideoAnalyzer,
        in_flight: asyncio.Task[dict[str, Any]] | None = None,
    ) -> None:
        clip_id = int(payload["clip_id"])
        if clip_id in self._secondary_clip_ids:
            return
        self._secondary_clip_ids.add(clip_id)
        task = asyncio.create_task(
            self._finish_secondary(payload, analyzer, in_flight),
            name=f"rfdetr-{clip_id}",
        )
        self._secondary_tasks.add(task)

        def completed(done: asyncio.Task[None]) -> None:
            self._secondary_tasks.discard(done)
            self._secondary_clip_ids.discard(clip_id)
            if self._running:
                asyncio.create_task(self._finalize_if_idle())

        task.add_done_callback(completed)

    def _recover_secondary_tasks(self) -> None:
        if not isinstance(self.analyzer, EnsembleVideoAnalyzer):
            return
        remote = self.analyzer.analyzers["rfdetr"]
        recovered = 0
        for key, value in self.db.list_state("ai:rfdetr:pending:"):
            try:
                payload = json.loads(value)
                path = Path(payload["path"])
                clip_id = int(payload["clip_id"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                LOGGER.error("[RF-DETR queue] Removing invalid item: %s", key)
                self.db.delete_state(key)
                continue
            if not path.is_file() or not self.db.clip_exists(path.resolve()):
                self.db.delete_state(key)
                continue
            self._schedule_secondary(payload, remote)
            recovered += 1
        if recovered:
            LOGGER.info(
                "[RF-DETR queue] Recovered %d durable pending videos",
                recovered,
            )

    async def _finish_secondary(
        self,
        payload: dict[str, Any],
        analyzer: RemoteVideoAnalyzer,
        in_flight: asyncio.Task[dict[str, Any]] | None = None,
    ) -> None:
        clip_id = int(payload["clip_id"])
        path = Path(payload["path"])
        captured_at = datetime.fromisoformat(payload["captured_at"])
        primary = payload["primary_result"]
        started_at = time.monotonic()
        try:
            secondary = (
                await in_flight
                if in_flight is not None
                else await self._run_secondary_model(
                    analyzer,
                    path,
                    captured_at,
                )
            )
            votes = {
                "yolo": EnsembleVideoAnalyzer._vote(primary),
                "rfdetr": EnsembleVideoAnalyzer._vote(secondary),
            }
            result = EnsembleVideoAnalyzer._combine(
                {"yolo": primary, "rfdetr": secondary},
                votes,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.error(
                "[RF-DETR queue] Failed for %s; preserving YOLO result: %s",
                path.name,
                exc,
            )
            votes = {
                "yolo": EnsembleVideoAnalyzer._vote(primary),
                "rfdetr": {
                    "status": "error",
                    "labels": {},
                    "score": 0.0,
                },
            }
            result = EnsembleVideoAnalyzer._combine({"yolo": primary}, votes)

        result.update(
            {
                "id": clip_id,
                "path": str(path.resolve()),
                "camera": payload["camera"],
                "captured_at": payload["captured_at"],
            }
        )
        self.db.update_clip_analysis(clip_id, result)
        self.db.delete_state(self._secondary_state_key(clip_id))
        self._dirty_cameras.add(payload["camera"])
        LOGGER.info(
            "[AI secondary complete] file=%s elapsed=%.2fs labels=%s "
            "models=%s",
            path.name,
            time.monotonic() - started_at,
            result.get("labels", {}),
            {
                name: vote.get("status")
                for name, vote in result.get("model_votes", {}).items()
            },
        )
        await self._deliver_final_model_result(result)

    async def _run_secondary_model(
        self,
        analyzer: RemoteVideoAnalyzer,
        path: Path,
        captured_at: datetime,
    ) -> dict[str, Any]:
        async with self._secondary_semaphore:
            return await asyncio.to_thread(
                analyzer.analyze,
                path,
                captured_at,
            )

    async def _telegram_send_with_id(
        self,
        event: dict[str, Any],
    ) -> int | None:
        sender = getattr(self.telegram, "send_event_message", None)
        if sender is not None:
            return await sender(event)
        return 0 if await self.telegram.send_event(event) else None

    async def _send_initial_model_notification(
        self,
        clip: dict[str, Any],
    ) -> None:
        if not self.telegram.configured:
            return
        event = self._notification_event(clip)
        if event is None:
            return
        event_key = self._telegram_key(event)
        file_key = self._telegram_file_key(event)
        async with self._notification_lock:
            if self.db.get_state(file_key) or self.db.get_state(event_key):
                self.db.set_state(
                    f"telegram:clip-sent:{clip['id']}",
                    datetime.now(timezone.utc).isoformat(),
                )
                return
            message_id = await self._telegram_send_with_id(event)
            if message_id is not None:
                sent_at = datetime.now(timezone.utc).isoformat()
                self.db.set_state(
                    f"telegram:clip-sent:{clip['id']}",
                    sent_at,
                )
                self.db.set_state(file_key, sent_at)
                self.db.set_state(event_key, sent_at)
                if message_id:
                    self.db.set_state(
                        f"telegram:clip-message:{clip['id']}",
                        str(message_id),
                    )
                    self.db.set_state(
                        self._telegram_file_message_key(event),
                        str(message_id),
                    )
        if message_id is None:
            self._queue_clip_notification(clip)
            return
        LOGGER.info(
            "[Telegram] Fast YOLO positive sent immediately: file=%s message=%s",
            Path(clip["path"]).name,
            message_id,
        )

    async def _deliver_final_model_result(
        self,
        clip: dict[str, Any],
    ) -> None:
        if not self.telegram.configured:
            return
        event = self._notification_event(clip)
        event_key = self._telegram_key(event) if event is not None else None
        message_value = None
        if event is not None:
            message_value = self.db.get_state(
                self._telegram_file_message_key(event)
            )
        message_value = message_value or self.db.get_state(
            f"telegram:clip-message:{clip['id']}"
        )
        if message_value and event is not None:
            async with self._notification_lock:
                await self.telegram.edit_event_caption(
                    int(message_value),
                    event,
                )
            return
        if event is not None and self.db.get_state(
            self._telegram_file_key(event)
        ):
            return
        if event_key and self.db.get_state(event_key):
            return
        pending_key = f"telegram:clip-pending:{clip['id']}"
        if event is None:
            self.db.delete_state(pending_key)
            return
        self._queue_clip_notification(clip)
        await self._send_pending_notifications()

    async def _notification_loop(self) -> None:
        while self._running:
            await self._send_pending_notifications()
            await asyncio.sleep(5)

    async def _send_pending_notifications(self) -> int:
        async with self._notification_lock:
            pending: list[tuple[str, dict[str, Any]]] = []
            for key, value in self.db.list_state("telegram:clip-pending:"):
                try:
                    pending.append((key, json.loads(value)))
                except json.JSONDecodeError:
                    LOGGER.error(
                        "[Telegram] Removing invalid pending notification: %s", key
                    )
                    self.db.delete_state(key)
            pending.sort(key=lambda item: item[1]["event"]["started_at"])

            sent = 0
            for key, payload in pending:
                clip_id = int(payload["clip_id"])
                queued_event = payload["event"]
                event = self.db.find_event_for_clip(clip_id) or queued_event
                for field in ("model_votes", "detected_by"):
                    if field in queued_event:
                        event[field] = queued_event[field]
                event_key = self._telegram_key(event)
                # Delivery uniqueness belongs to the downloaded source clip,
                # even when the event builder supplies a merged output video.
                file_key = self._telegram_file_key(queued_event)
                if self.db.get_state(file_key) or self.db.get_state(event_key):
                    self.db.delete_state(key)
                    continue
                self.notification_progress.update(
                    phase="notifying", file=event.get("video_path")
                )
                LOGGER.info(
                    "[Telegram] Sending analyzed clip immediately: camera=%s kind=%s file=%s",
                    event["camera"],
                    event["kind"],
                    Path(event["video_path"]).name,
                )
                message_id = await self._telegram_send_with_id(event)
                if message_id is None:
                    break
                self.db.delete_state(key)
                sent_at = datetime.now(timezone.utc).isoformat()
                self.db.set_state(f"telegram:clip-sent:{clip_id}", sent_at)
                self.db.set_state(file_key, sent_at)
                self.db.set_state(event_key, sent_at)
                if message_id:
                    self.db.set_state(
                        f"telegram:clip-message:{clip_id}",
                        str(message_id),
                    )
                    self.db.set_state(
                        self._telegram_file_message_key(queued_event),
                        str(message_id),
                    )
                sent += 1
            self.notification_progress.update(phase="idle", file=None)
            return sent

    async def download_once(self, since_override: datetime | None = None) -> int:
        if self._download_lock.locked():
            return 0
        async with self._download_lock:
            LOGGER.info("[Download scan started]")
            await self.cleanup_expired_media()
            downloaded = 0
            if self.downloader.configured and not self.settings.demo_mode:
                last = self.db.get_state("last_scan")
                since = since_override
                if since is None and last:
                    overlap_seconds = max(
                        0, self.settings.blink_scan_overlap_seconds
                    )
                    since = datetime.fromisoformat(last) - timedelta(
                        seconds=overlap_seconds
                    )
                    LOGGER.info(
                        "[Download scan] Applying %ds late-arrival overlap: since=%s",
                        overlap_seconds,
                        since.isoformat(),
                    )
                self.download_progress.update(
                    phase="downloading", current=0, total=0, file=None
                )
                downloaded = await self.downloader.download_new(since)

            now = datetime.now(timezone.utc).isoformat()
            if self.downloader.incomplete_downloads:
                LOGGER.warning(
                    "[Download scan] Keeping last_scan unchanged so deferred or failed clips can be retried"
                )
            else:
                self.db.set_state("last_scan", now)
            self.download_progress.update(phase="idle", current=0, total=0, file=None)
            LOGGER.info("[Download scan complete] downloaded=%d", downloaded)
            return downloaded

    async def analyze_pending(self) -> int:
        """Analyze pending files now; primarily used by maintenance scripts/tests."""
        if self._analysis_lock.locked():
            return 0
        async with self._analysis_lock:
            pending = [
                path
                for path in sorted(self.settings.raw_dir.rglob("*.mp4"))
                if not self.db.clip_exists(path.resolve())
            ]
            if pending:
                LOGGER.info("[AI queue] %d completed downloads pending", len(pending))
            self.analysis_progress.update(
                phase="analyzing" if pending else "idle",
                current=0,
                total=len(pending),
                file=None,
            )
            analyzed = 0
            worker_count = max(1, self.settings.ai_worker_count)
            analyzers = [
                self.analyzer,
                *(self._new_analyzer() for _ in range(worker_count - 1)),
            ]
            jobs: asyncio.Queue[tuple[int, Path]] = asyncio.Queue()
            for index, path in enumerate(pending, start=1):
                jobs.put_nowait((index, path))

            async def run_worker(analyzer: VideoAnalyzer) -> int:
                completed = 0
                while not jobs.empty():
                    try:
                        index, path = jobs.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    self.analysis_progress.update(current=index, file=path.name)
                    LOGGER.info(
                        "[AI analysis %d/%d] Started: %s",
                        index,
                        len(pending),
                        path.name,
                    )
                    try:
                        await self._analyze_one(path, analyzer)
                        completed += 1
                    except Exception as exc:
                        LOGGER.error("[AI analysis] Pending video failed: %s", exc)
                    finally:
                        jobs.task_done()
                return completed

            results = await asyncio.gather(
                *(run_worker(analyzer) for analyzer in analyzers),
            )
            analyzed = sum(results)
            await self._send_pending_notifications()

            still_pending = any(
                not self.db.clip_exists(path.resolve())
                for path in self.settings.raw_dir.rglob("*.mp4")
            )
            if (
                self._dirty_cameras
                and not still_pending
                and not self._download_lock.locked()
            ):
                await self._finalize_dirty_events()
            self.analysis_progress.update(phase="idle", current=0, total=0, file=None)
            return analyzed

    async def _finalize_dirty_events(self) -> None:
        cameras = sorted(self._dirty_cameras)
        for camera in cameras:
            LOGGER.info("[Event creation] camera=%s", camera)
            await asyncio.to_thread(self.rebuild_events, camera)
            self._dirty_cameras.discard(camera)

    def _queue_clip_notification(self, clip: dict[str, Any]) -> None:
        if not self.telegram.configured:
            return
        event = self._notification_event(clip)
        if event is None:
            return
        if self.db.get_state(self._telegram_file_key(event)):
            return
        key = f"telegram:clip-pending:{clip['id']}"
        payload = {
            "clip_id": clip["id"],
            "event": event,
        }
        self.db.set_state(key, json.dumps(payload, ensure_ascii=False))
        LOGGER.info(
            "[Telegram queue] Added analyzed clip for immediate delivery: id=%s kind=%s file=%s",
            clip["id"],
            event["kind"],
            Path(clip["path"]).name,
        )

    def _notification_event(
        self,
        clip: dict[str, Any],
    ) -> dict[str, Any] | None:
        event = build_event([clip])
        if not should_keep(event, self.settings.keep_unknown_motion):
            return None
        event.pop("source_paths", None)
        event["camera"] = clip["camera"]
        event["video_path"] = clip["path"]
        for field in ("model_votes", "detected_by"):
            if field in clip:
                event[field] = clip[field]
        return event

    async def scan(self, since_override: datetime | None = None) -> dict[str, int]:
        """Trigger a download scan; the independent AI worker consumes its queue."""
        if self._download_lock.locked():
            return {"downloaded": 0, "analyzed": 0, "events": 0}
        downloaded = await self.download_once(since_override)
        await self._finalize_if_idle()
        return {
            "downloaded": downloaded,
            "analyzed": 0,
            "events": len(self.db.list_events()),
        }

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

    @staticmethod
    def _telegram_filename(event: dict[str, Any]) -> str:
        """Return the unique source filename used as the delivery identity."""
        return Path(event["video_path"]).name

    def _telegram_file_key(self, event: dict[str, Any]) -> str:
        return f"telegram:file-sent:{self._telegram_filename(event)}"

    def _telegram_file_message_key(self, event: dict[str, Any]) -> str:
        return f"telegram:file-message:{self._telegram_filename(event)}"

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
            self.notification_progress.update(
                phase="notifying", file=event.get("video_path")
            )
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
        downloading = self._download_lock.locked()
        analyzing = bool(self._secondary_tasks) or self._active_analyses > 0 or (
            self._analysis_lock.locked()
            and self.analysis_progress["phase"] != "idle"
        )
        notifying = self.notification_progress["phase"] == "notifying"
        progress = (
            self.analysis_progress
            if analyzing
            else self.notification_progress
            if notifying
            else self.download_progress
        )
        return {
            "configured": self.downloader.configured,
            "scanning": downloading,
            "analyzing": analyzing,
            "notifying": notifying,
            "interval_seconds": self.settings.scan_interval_seconds,
            "analysis_interval_seconds": self.settings.analysis_interval_seconds,
            "ai_backend": self.ai_backend,
            "ai_worker_count": max(1, self.settings.ai_worker_count),
            "analysis_queue_size": self._analysis_queue.qsize(),
            "active_analyses": self._active_analyses,
            "rfdetr_pending": len(self._secondary_tasks),
            "last_scan": self.db.get_state("last_scan"),
            "last_error": self.last_error,
            "progress": progress,
            "workers": {
                "downloader": self.download_progress,
                "analyzer": self.analysis_progress,
                "notifier": self.notification_progress,
            },
            "telegram_configured": self.telegram.configured,
            "counts": self.db.counts(),
        }
