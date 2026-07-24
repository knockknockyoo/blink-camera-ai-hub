from __future__ import annotations

import tempfile
import unittest
import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from backend.analyzer import (
    anomaly_reasons,
    classify_event,
    credible_person_detection,
    credible_vehicle_detection,
    label_is_supported,
    moving_detection_track,
)
from backend.db import Database
from backend.blink_client import BlinkDownloader
from backend.config import Settings
from backend.ensemble_analyzer import EnsembleVideoAnalyzer
from backend.events import build_event, clips_are_related, merge_clips, should_keep
from backend.retention import delete_expired_videos
from backend.remote_analyzer import RemoteVideoAnalyzer
from backend.rfdetr_analyzer import (
    RFDETRVideoAnalyzer,
    deduplicate_frame_detections,
)
from backend.service import MonitorService
from backend.telegram import TelegramNotifier


def clip(clip_id: int, at: datetime, labels=None, motion=0.01):
    return {
        "id": clip_id,
        "path": f"/tmp/{clip_id}.mp4",
        "camera": "Outdoor",
        "captured_at": at.isoformat(),
        "duration": 10,
        "labels": labels or {},
        "score": 0.9,
        "motion_score": motion,
        "anomaly": False,
        "anomaly_reasons": [],
    }


class CoreTests(unittest.TestCase):
    def test_rfdetr_small_model_is_selected_lazily(self):
        sentinel = object()
        analyzer = RFDETRVideoAnalyzer("small", 0.15, 5)
        fake_module = SimpleNamespace(RFDETRSmall=lambda: sentinel)

        with patch.dict("sys.modules", {"rfdetr": fake_module}):
            self.assertIs(analyzer._load_model(), sentinel)
            self.assertIs(analyzer._load_model(), sentinel)

    def test_rfdetr_overlapping_boxes_are_counted_once(self):
        detections = [
            ("person", [100, 100, 300, 500], 0.91),
            ("person", [110, 105, 305, 505], 0.75),
            ("person", [600, 100, 800, 500], 0.82),
            ("bicycle", [100, 100, 300, 500], 0.88),
        ]

        kept = deduplicate_frame_detections(detections)

        self.assertEqual(
            [(label, score) for label, _box, score in kept],
            [
                ("person", 0.91),
                ("bicycle", 0.88),
                ("person", 0.82),
            ],
        )

    def test_remote_analyzer_sends_shared_relative_path(self):
        class Response(BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "raw" / "2" / "clip.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            analyzer = RemoteVideoAnalyzer(
                "http://host.docker.internal:8790",
                "secret",
                root,
                10,
            )
            captured_at = datetime.now(timezone.utc)
            response = Response(
                json.dumps(
                    {
                        "duration": 1,
                        "labels": {"person": 1},
                        "score": 1,
                        "motion_score": 0.2,
                        "anomaly": False,
                        "anomaly_reasons": [],
                    }
                ).encode()
            )

            with patch("backend.remote_analyzer.urlopen", return_value=response) as call:
                result = analyzer.analyze(video, captured_at)

            request = call.call_args.args[0]
            body = json.loads(request.data)
            self.assertEqual(body["path"], "raw/2/clip.mp4")
            self.assertEqual(request.headers["X-ai-token"], "secret")
            self.assertEqual(result["labels"], {"person": 1})

    def test_service_uses_native_ai_when_url_is_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            service = MonitorService(
                Settings(
                    data_dir=Path(directory),
                    video_retention_days=0,
                    native_ai_url="http://host.docker.internal:8790",
                    native_ai_token="secret",
                )
            )

            self.assertIsInstance(service.analyzer, EnsembleVideoAnalyzer)
            self.assertIsInstance(
                service.analyzer.analyzers["rfdetr"],
                RemoteVideoAnalyzer,
            )
            self.assertEqual(
                service.status()["ai_backend"],
                "YOLO + RF-DETR Small",
            )

    def test_native_ai_videos_are_analyzed_concurrently(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                service = MonitorService(
                    Settings(
                        data_dir=root,
                        video_retention_days=0,
                        native_ai_url="http://host.docker.internal:8790",
                        native_ai_token="secret",
                        ai_worker_count=2,
                    )
                )
                for name in ("20260723_010000_1.mp4", "20260723_010100_1.mp4"):
                    path = root / "raw" / "1" / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"video")

                state = {"active": 0, "maximum": 0}
                lock = threading.Lock()

                class Analyzer:
                    def analyze(self, _path, _captured_at):
                        with lock:
                            state["active"] += 1
                            state["maximum"] = max(
                                state["maximum"], state["active"]
                            )
                        time.sleep(0.05)
                        with lock:
                            state["active"] -= 1
                        return {
                            "duration": 1,
                            "labels": {},
                            "score": 0,
                            "motion_score": 0,
                            "anomaly": False,
                            "anomaly_reasons": [],
                        }

                async def skip_event_rebuild():
                    return None

                service.analyzer = Analyzer()
                service._new_analyzer = Analyzer
                service._finalize_dirty_events = skip_event_rebuild
                analyzed = await service.analyze_pending()

                self.assertEqual(analyzed, 2)
                self.assertEqual(state["maximum"], 2)

        asyncio.run(run())

    def test_ensemble_runs_models_concurrently_and_accepts_either_positive(self):
        barrier = threading.Barrier(2)
        state = {"active": 0, "maximum": 0}
        lock = threading.Lock()

        class Analyzer:
            def __init__(self, labels):
                self.labels = labels

            def analyze(self, _path, _captured_at):
                with lock:
                    state["active"] += 1
                    state["maximum"] = max(state["maximum"], state["active"])
                barrier.wait(timeout=2)
                time.sleep(0.02)
                with lock:
                    state["active"] -= 1
                return {
                    "duration": 10,
                    "labels": self.labels,
                    "score": 0.8 if self.labels else 0.1,
                    "motion_score": 0.2,
                    "anomaly": False,
                    "anomaly_reasons": [],
                }

        analyzer = EnsembleVideoAnalyzer(
            Analyzer({}),
            Analyzer({"person": 2}),
        )
        result = analyzer.analyze(
            Path("clip.mp4"),
            datetime.now(timezone.utc),
        )

        self.assertEqual(state["maximum"], 2)
        self.assertEqual(result["labels"], {"person": 2})
        self.assertEqual(result["detected_by"], ["rfdetr"])
        self.assertEqual(result["model_votes"]["yolo"]["status"], "negative")
        self.assertEqual(
            result["model_votes"]["rfdetr"]["status"],
            "positive",
        )

    def test_ensemble_keeps_yolo_result_when_rfdetr_fails(self):
        class Yolo:
            def analyze(self, _path, _captured_at):
                return {
                    "duration": 4,
                    "labels": {"motorcycle": 1},
                    "score": 0.75,
                    "motion_score": 0.1,
                    "anomaly": False,
                    "anomaly_reasons": [],
                }

        class RFDetr:
            def analyze(self, _path, _captured_at):
                raise RuntimeError("native service unavailable")

        result = EnsembleVideoAnalyzer(Yolo(), RFDetr()).analyze(
            Path("clip.mp4"),
            datetime.now(timezone.utc),
        )

        self.assertEqual(result["labels"], {"motorcycle": 1})
        self.assertEqual(result["detected_by"], ["yolo"])
        self.assertEqual(result["model_votes"]["yolo"]["status"], "positive")
        self.assertEqual(
            result["model_votes"]["rfdetr"]["status"],
            "error",
        )

    def test_ensemble_reports_first_positive_while_other_model_is_pending(self):
        async def run():
            slow_finished = threading.Event()
            partial_results = []

            class FastYolo:
                def analyze(self, _path, _captured_at):
                    return {
                        "duration": 4,
                        "labels": {"person": 1},
                        "score": 0.8,
                        "motion_score": 0.1,
                        "anomaly": False,
                        "anomaly_reasons": [],
                    }

            class SlowRFDetr:
                def analyze(self, _path, _captured_at):
                    time.sleep(0.1)
                    slow_finished.set()
                    return {
                        "duration": 4,
                        "labels": {},
                        "score": 0.1,
                        "motion_score": 0.1,
                        "anomaly": False,
                        "anomaly_reasons": [],
                    }

            async def first_positive(result):
                self.assertFalse(slow_finished.is_set())
                partial_results.append(result)

            analyzer = EnsembleVideoAnalyzer(FastYolo(), SlowRFDetr())
            final = await analyzer.analyze_async(
                Path("clip.mp4"),
                datetime.now(timezone.utc),
                first_positive,
            )

            self.assertEqual(len(partial_results), 1)
            self.assertEqual(
                partial_results[0]["model_votes"]["yolo"]["status"],
                "positive",
            )
            self.assertEqual(
                partial_results[0]["model_votes"]["rfdetr"]["status"],
                "pending",
            )
            self.assertEqual(
                final["model_votes"]["rfdetr"]["status"],
                "negative",
            )

        asyncio.run(run())

    def test_service_sends_first_vote_then_updates_same_telegram_message(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                video = root / "raw" / "1" / "20260723_120000_1.mp4"
                video.parent.mkdir(parents=True)
                video.write_bytes(b"video")
                service = MonitorService(
                    Settings(
                        data_dir=root,
                        video_retention_days=0,
                        native_ai_url="",
                        telegram_bot_token="token",
                        telegram_chat_id="123",
                    )
                )

                class FastYolo:
                    def analyze(self, _path, _captured_at):
                        return {
                            "duration": 4,
                            "labels": {"person": 1},
                            "score": 0.8,
                            "motion_score": 0.1,
                            "anomaly": False,
                            "anomaly_reasons": [],
                        }

                class SlowRFDetr:
                    def analyze(self, _path, _captured_at):
                        time.sleep(0.2)
                        return {
                            "duration": 4,
                            "labels": {"person": 1},
                            "score": 0.9,
                            "motion_score": 0.1,
                            "anomaly": False,
                            "anomaly_reasons": [],
                        }

                sent = []
                edited = []

                class Telegram:
                    configured = True

                    async def send_event_message(self, event):
                        sent.append(event)
                        return 321

                    async def edit_event_caption(self, message_id, event):
                        edited.append((message_id, event))
                        return True

                    async def send_event(self, _event):
                        raise AssertionError("The video must not be sent twice.")

                service.telegram = Telegram()
                started_at = time.monotonic()
                result = await service._analyze_one(
                    video,
                    EnsembleVideoAnalyzer(FastYolo(), SlowRFDetr()),
                )
                primary_elapsed = time.monotonic() - started_at

                self.assertEqual(len(sent), 1)
                self.assertLess(primary_elapsed, 0.1)
                self.assertEqual(
                    sent[0]["model_votes"]["rfdetr"]["status"],
                    "pending",
                )
                await asyncio.gather(*list(service._secondary_tasks))
                self.assertEqual(len(edited), 1)
                self.assertEqual(edited[0][0], 321)
                self.assertEqual(
                    edited[0][1]["model_votes"]["rfdetr"]["status"],
                    "positive",
                )
                self.assertEqual(len(sent), 1)
                self.assertEqual(result["labels"], {"person": 1})
                self.assertEqual(
                    service.db.list_state("telegram:clip-pending:"),
                    [],
                )

        asyncio.run(run())

    def test_fast_rfdetr_updates_caption_after_slow_video_upload(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                video = root / "raw" / "1" / "20260723_120000_race.mp4"
                video.parent.mkdir(parents=True)
                video.write_bytes(b"video")
                service = MonitorService(
                    Settings(
                        data_dir=root,
                        video_retention_days=0,
                        native_ai_url="",
                        telegram_bot_token="token",
                        telegram_chat_id="123",
                    )
                )

                class Analyzer:
                    def __init__(self, labels):
                        self.labels = labels

                    def analyze(self, _path, _captured_at):
                        return {
                            "duration": 4,
                            "labels": self.labels,
                            "score": 0.9,
                            "motion_score": 0.1,
                            "anomaly": False,
                            "anomaly_reasons": [],
                        }

                sent = []
                edited = []

                class Telegram:
                    configured = True

                    async def send_event_message(self, event):
                        sent.append(event)
                        await asyncio.sleep(0.05)
                        return 654

                    async def edit_event_caption(self, message_id, event):
                        edited.append((message_id, event))
                        return True

                service.telegram = Telegram()
                await service._analyze_one(
                    video,
                    EnsembleVideoAnalyzer(
                        Analyzer({"person": 1}),
                        Analyzer({"person": 1}),
                    ),
                )
                await asyncio.gather(*list(service._secondary_tasks))

                self.assertEqual(len(sent), 1)
                self.assertEqual(
                    sent[0]["model_votes"]["rfdetr"]["status"],
                    "pending",
                )
                self.assertEqual(len(edited), 1)
                self.assertEqual(edited[0][0], 654)
                self.assertEqual(
                    edited[0][1]["model_votes"]["rfdetr"]["status"],
                    "positive",
                )

        asyncio.run(run())

    def test_same_filename_is_never_sent_twice(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                service = MonitorService(
                    Settings(
                        data_dir=root,
                        video_retention_days=0,
                        native_ai_url="",
                        telegram_bot_token="token",
                        telegram_chat_id="123",
                    )
                )
                sent = []

                class Telegram:
                    configured = True

                    async def send_event_message(self, event):
                        sent.append(event)
                        return 1000 + len(sent)

                service.telegram = Telegram()
                captured_at = datetime.now(timezone.utc).isoformat()

                def clip(clip_id, camera):
                    return {
                        "id": clip_id,
                        "path": str(
                            root
                            / "raw"
                            / camera
                            / "20260723_120000_unique.mp4"
                        ),
                        "camera": camera,
                        "captured_at": captured_at,
                        "duration": 4,
                        "labels": {"person": 1},
                        "score": 0.9,
                        "motion_score": 0.1,
                        "anomaly": False,
                        "anomaly_reasons": [],
                    }

                await asyncio.gather(
                    service._send_initial_model_notification(clip(1, "1")),
                    service._send_initial_model_notification(clip(2, "2")),
                )

                self.assertEqual(len(sent), 1)
                self.assertIsNotNone(
                    service.db.get_state(
                        "telegram:file-sent:20260723_120000_unique.mp4"
                    )
                )

        asyncio.run(run())

    def test_models_start_in_parallel_for_each_video(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                video = root / "raw" / "1" / "20260723_120001_1.mp4"
                video.parent.mkdir(parents=True)
                video.write_bytes(b"video")
                service = MonitorService(
                    Settings(
                        data_dir=root,
                        video_retention_days=0,
                        native_ai_url="",
                    )
                )
                barrier = threading.Barrier(2, timeout=1)

                class Analyzer:
                    def __init__(self, labels):
                        self.labels = labels

                    def analyze(self, _path, _captured_at):
                        barrier.wait()
                        return {
                            "duration": 4,
                            "labels": self.labels,
                            "score": 0.9 if self.labels else 0.1,
                            "motion_score": 0.1,
                            "anomaly": False,
                            "anomaly_reasons": [],
                        }

                await service._analyze_one(
                    video,
                    EnsembleVideoAnalyzer(
                        Analyzer({"person": 1}),
                        Analyzer({}),
                    ),
                )
                await asyncio.gather(*list(service._secondary_tasks))

        asyncio.run(run())

    def test_secondary_positive_sends_when_fast_yolo_is_negative(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                video = root / "raw" / "2" / "20260723_120000_2.mp4"
                video.parent.mkdir(parents=True)
                video.write_bytes(b"video")
                service = MonitorService(
                    Settings(
                        data_dir=root,
                        video_retention_days=0,
                        native_ai_url="",
                        telegram_bot_token="token",
                        telegram_chat_id="123",
                    )
                )

                class Yolo:
                    def analyze(self, _path, _captured_at):
                        return {
                            "duration": 4,
                            "labels": {},
                            "score": 0.1,
                            "motion_score": 0.1,
                            "anomaly": False,
                            "anomaly_reasons": [],
                        }

                class RFDetr:
                    def analyze(self, _path, _captured_at):
                        return {
                            "duration": 4,
                            "labels": {"person": 1},
                            "score": 1.0,
                            "motion_score": 0.1,
                            "anomaly": False,
                            "anomaly_reasons": [],
                        }

                sent = []

                class Telegram:
                    configured = True

                    async def send_event_message(self, event):
                        sent.append(event)
                        return 777

                    async def edit_event_caption(self, _message_id, _event):
                        raise AssertionError("No provisional message exists.")

                service.telegram = Telegram()
                primary = await service._analyze_one(
                    video,
                    EnsembleVideoAnalyzer(Yolo(), RFDetr()),
                )
                self.assertEqual(primary["labels"], {})
                self.assertEqual(sent, [])

                await asyncio.gather(*list(service._secondary_tasks))

                self.assertEqual(len(sent), 1)
                self.assertEqual(sent[0]["labels"], {"person": 1})
                self.assertEqual(
                    sent[0]["model_votes"]["yolo"]["status"],
                    "negative",
                )
                self.assertEqual(
                    sent[0]["model_votes"]["rfdetr"]["status"],
                    "positive",
                )
                clips = service.db.recent_clips("2")
                self.assertEqual(clips[0]["labels"], {"person": 1})

        asyncio.run(run())

    def test_blink_clip_download_retries_transient_failure(self):
        class FlakyItem:
            def __init__(self):
                self.attempts = 0

            async def prepare_download(self, _blink):
                self.attempts += 1
                if self.attempts < 3:
                    raise AttributeError("No network_id or id in response")
                return True

            async def download_video(self, _blink, destination):
                Path(destination).write_bytes(b"video")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloader = BlinkDownloader(
                root / "auth.json",
                root / "raw",
                download_retries=4,
                download_delay_seconds=0,
                retry_backoff_seconds=0,
            )
            item = FlakyItem()
            destination = root / "raw" / "clip.mp4"
            destination.parent.mkdir(parents=True)
            downloaded = asyncio.run(
                downloader._download_local_item(item, object(), destination)
            )

            self.assertTrue(downloaded)
            self.assertEqual(item.attempts, 3)
            self.assertEqual(destination.read_bytes(), b"video")

    def test_blink_clip_download_timeout_skips_for_next_scan(self):
        class HangingItem:
            async def prepare_download(self, _blink):
                await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloader = BlinkDownloader(
                root / "auth.json",
                root / "raw",
                download_retries=1,
                download_delay_seconds=0,
                clip_timeout_seconds=0.01,
            )
            destination = root / "raw" / "clip.mp4"
            destination.parent.mkdir(parents=True)

            downloaded = asyncio.run(
                downloader._download_local_item(HangingItem(), object(), destination)
            )

            self.assertFalse(downloaded)
            self.assertFalse(destination.exists())

    def test_completed_download_is_published_atomically(self):
        class Item:
            async def prepare_download(self, _blink):
                return True

            async def download_video(self, _blink, destination):
                self.destination = Path(destination)
                self.destination.write_bytes(b"video")
                self.final_was_visible_during_download = Path(
                    str(self.destination).removesuffix(".part")
                ).exists()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            completed = []
            downloader = BlinkDownloader(
                root / "auth.json",
                root / "raw",
                downloaded_callback=completed.append,
            )
            item = Item()
            destination = root / "raw" / "clip.mp4"
            destination.parent.mkdir(parents=True)

            downloaded = asyncio.run(
                downloader._download_local_item(item, object(), destination)
            )

            self.assertTrue(downloaded)
            self.assertFalse(item.final_was_visible_during_download)
            self.assertEqual(destination.read_bytes(), b"video")
            self.assertFalse(destination.with_suffix(".mp4.part").exists())
            self.assertEqual(completed, [destination.resolve()])

    def test_downloaded_videos_run_in_parallel_and_notify_immediately(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                service = MonitorService(
                    Settings(
                        data_dir=root,
                        video_retention_days=0,
                        ai_worker_count=2,
                        telegram_bot_token="token",
                        telegram_chat_id="123",
                    )
                )
                videos = [
                    root / "raw" / "1" / f"20260723_12000{index}_1.mp4"
                    for index in range(2)
                ]
                for video in videos:
                    video.parent.mkdir(parents=True, exist_ok=True)
                    video.write_bytes(b"video")

                barrier = threading.Barrier(2)
                state = {"active": 0, "maximum": 0}
                state_lock = threading.Lock()

                class Analyzer:
                    def analyze(self, _path, _captured_at):
                        with state_lock:
                            state["active"] += 1
                            state["maximum"] = max(
                                state["maximum"], state["active"]
                            )
                        barrier.wait(timeout=2)
                        time.sleep(0.05)
                        with state_lock:
                            state["active"] -= 1
                        return {
                            "duration": 1,
                            "labels": {"person": 1},
                            "score": 0.9,
                            "motion_score": 0.2,
                            "anomaly": False,
                            "anomaly_reasons": [],
                        }

                delivered = []

                class Telegram:
                    configured = True

                    async def send_event(self, event):
                        delivered.append(Path(event["video_path"]).name)
                        return True

                async def skip_event_rebuild():
                    return None

                service.telegram = Telegram()
                service._finalize_dirty_events = skip_event_rebuild
                service._running = True
                workers = [
                    asyncio.create_task(service._analysis_worker(1, Analyzer())),
                    asyncio.create_task(service._analysis_worker(2, Analyzer())),
                ]
                for video in videos:
                    service._enqueue_analysis(video)

                await asyncio.wait_for(service._analysis_queue.join(), timeout=3)
                service._running = False
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)

                self.assertEqual(state["maximum"], 2)
                self.assertEqual(len(delivered), 2)
                self.assertTrue(
                    all(service.db.clip_exists(video.resolve()) for video in videos)
                )

        asyncio.run(run())

    def test_blink_metadata_stage_times_out(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                downloader = BlinkDownloader(
                    root / "auth.json",
                    root / "raw",
                    metadata_timeout_seconds=0.01,
                )
                with self.assertRaisesRegex(RuntimeError, "metadata stage"):
                    await downloader._metadata_stage(
                        "test operation", asyncio.Event().wait()
                    )

        asyncio.run(run())

    def test_manifest_refresh_does_not_prepare_stored_clips(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                downloader = BlinkDownloader(root / "auth.json", root / "raw")

                class Item:
                    prepared = False

                    async def prepare_download(self, _blink):
                        self.prepared = True

                item = Item()

                class SyncModule:
                    def __init__(self):
                        self.calls = 0
                        self._local_storage = {
                            "status": True,
                            "manifest_stale": True,
                            "manifest": [item],
                        }

                    async def update_local_storage_manifest(self):
                        self.calls += 1
                        self._local_storage["manifest_stale"] = False
                        return True

                sync_module = SyncModule()
                blink = SimpleNamespace(sync={"Home": sync_module})

                await downloader._refresh_local_storage_manifests(blink)

                self.assertEqual(sync_module.calls, 1)
                self.assertFalse(item.prepared)
                self.assertFalse(downloader.incomplete_downloads)

        asyncio.run(run())

    def test_cloud_downloads_are_published_from_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw"
            staging = root / ".cloud-download-staging"
            staged_clip = staging / "Outdoor" / "clip.mp4"
            staged_clip.parent.mkdir(parents=True)
            staged_clip.write_bytes(b"video")
            downloader = BlinkDownloader(root / "auth.json", raw)

            published = downloader._publish_cloud_downloads(staging)

            self.assertEqual(published, 1)
            self.assertEqual((raw / "Outdoor" / "clip.mp4").read_bytes(), b"video")
            self.assertFalse(staged_clip.exists())

    def test_ai_analysis_can_run_while_downloader_is_active(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                service = MonitorService(
                    Settings(
                        data_dir=root,
                        video_retention_days=0,
                        telegram_bot_token="",
                        telegram_chat_id="",
                    )
                )
                clip_ready = asyncio.Event()
                release_download = asyncio.Event()
                raw_clip = root / "raw" / "1" / "20260722_120000_1.mp4"

                class Downloader:
                    configured = True
                    incomplete_downloads = False

                    async def download_new(self, _since):
                        raw_clip.parent.mkdir(parents=True, exist_ok=True)
                        raw_clip.write_bytes(b"video")
                        clip_ready.set()
                        await release_download.wait()
                        return 1

                class Analyzer:
                    def analyze(self, _path, _captured_at):
                        return {
                            "duration": 1,
                            "labels": {"person": 1},
                            "score": 0.9,
                            "motion_score": 0.2,
                            "anomaly": False,
                            "anomaly_reasons": [],
                        }

                service.downloader = Downloader()
                service.analyzer = Analyzer()
                download_task = asyncio.create_task(service.download_once())
                await clip_ready.wait()

                analyzed = await service.analyze_pending()

                self.assertEqual(analyzed, 1)
                self.assertTrue(service._download_lock.locked())
                self.assertTrue(service.db.clip_exists(raw_clip.resolve()))
                release_download.set()
                self.assertEqual(await download_task, 1)

        asyncio.run(run())

    def test_download_scan_overlaps_watermark_for_late_blink_clips(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                service = MonitorService(
                    Settings(
                        data_dir=root,
                        video_retention_days=0,
                        blink_scan_overlap_seconds=900,
                        telegram_bot_token="",
                        telegram_chat_id="",
                    )
                )
                watermark = datetime(2026, 7, 23, 1, 22, tzinfo=timezone.utc)
                service.db.set_state("last_scan", watermark.isoformat())
                requested_since = []

                class Downloader:
                    configured = True
                    incomplete_downloads = False

                    async def download_new(self, since):
                        requested_since.append(since)
                        return 0

                service.downloader = Downloader()

                self.assertEqual(await service.download_once(), 0)
                self.assertEqual(
                    requested_since,
                    [watermark - timedelta(minutes=15)],
                )
                explicit_since = watermark - timedelta(hours=12)
                self.assertEqual(
                    await service.download_once(since_override=explicit_since),
                    0,
                )
                self.assertEqual(requested_since[-1], explicit_since)

        asyncio.run(run())

    def test_ai_analysis_sends_relevant_result_to_telegram_immediately(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                service = MonitorService(
                    Settings(
                        data_dir=root,
                        video_retention_days=0,
                        telegram_bot_token="token",
                        telegram_chat_id="123",
                    )
                )
                raw_clip = root / "raw" / "1" / "20260722_120000_1.mp4"
                raw_clip.parent.mkdir(parents=True)
                raw_clip.write_bytes(b"video")
                delivered = []

                class Analyzer:
                    def analyze(self, _path, _captured_at):
                        return {
                            "duration": 1,
                            "labels": {"person": 1},
                            "score": 0.9,
                            "motion_score": 0.2,
                            "anomaly": False,
                            "anomaly_reasons": [],
                        }

                class Telegram:
                    configured = True

                    async def send_event(self, event):
                        delivered.append(event)
                        return True

                async def skip_event_rebuild():
                    return None

                service.analyzer = Analyzer()
                service.telegram = Telegram()
                service._finalize_dirty_events = skip_event_rebuild

                analyzed = await service.analyze_pending()

                self.assertEqual(analyzed, 1)
                self.assertEqual(len(delivered), 1)
                self.assertEqual(delivered[0]["kind"], "person")
                self.assertEqual(delivered[0]["video_path"], str(raw_clip.resolve()))
                self.assertEqual(service.db.list_state("telegram:clip-pending:"), [])

        asyncio.run(run())

    def test_blink_backlog_prioritizes_newest_and_defers_the_rest(self):
        now = datetime.now(timezone.utc)
        items = [
            (
                SimpleNamespace(created_at=now + timedelta(minutes=index)),
                Path(f"{index}.mp4"),
            )
            for index in range(5)
        ]
        downloader = BlinkDownloader(
            Path("auth.json"),
            Path("raw"),
            max_clips_per_scan=2,
        )

        selected, deferred = downloader._prioritize_local_items(items)

        self.assertEqual([path.name for _, path in selected], ["4.mp4", "3.mp4"])
        self.assertEqual(deferred, 3)

    def test_classification_prioritizes_people_and_ignores_animals(self):
        self.assertEqual(classify_event({"person": 1, "dog": 1}, 0.1), "person")
        self.assertEqual(classify_event({"dog": 1}, 0.1), "motion")
        self.assertEqual(classify_event({}, 0.08), "motion")

    def test_anomaly_rules(self):
        night = datetime(2026, 7, 16, 23, 30, tzinfo=timezone.utc)
        self.assertIn("Person detected at night", anomaly_reasons({"person": 1}, night, 0.1))
        self.assertIn("Multiple people detected", anomaly_reasons({"person": 2}, night, 0.1))
        self.assertEqual(anomaly_reasons({}, night, 0.09), [])
        self.assertEqual(anomaly_reasons({"chair": 1}, night, 0.07), [])

    def test_static_person_false_positive_is_rejected(self):
        still = np.zeros((90, 160), dtype=np.uint8)
        moved = still.copy()
        moved[24:44, 44:56] = 255
        frame_shape = (1080, 1920, 3)
        small_box = [560, 320, 628, 489]
        large_box = [300, 200, 700, 900]

        self.assertFalse(
            credible_person_detection(small_box, frame_shape, [still, still], 0)
        )
        self.assertTrue(
            credible_person_detection(small_box, frame_shape, [still, moved], 0)
        )
        self.assertFalse(
            credible_person_detection(large_box, frame_shape, [still, still], 0)
        )
        self.assertTrue(
            credible_person_detection(large_box, frame_shape, [still, moved], 0)
        )

    def test_ir_exposure_change_is_not_person_motion(self):
        dark = np.zeros((90, 160), dtype=np.uint8)
        bright = np.full((90, 160), 255, dtype=np.uint8)
        frame_shape = (720, 1280, 3)
        false_person_box = [900, 180, 1100, 500]

        self.assertFalse(
            credible_person_detection(
                false_person_box, frame_shape, [dark, bright], 0
            )
        )

    def test_parked_vehicle_is_ignored_but_moving_vehicle_is_kept(self):
        still = np.zeros((90, 160), dtype=np.uint8)
        moved = still.copy()
        moved[25:65, 40:120] = 255
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        rng = np.random.default_rng(42)
        frame[200:600, 320:960] = rng.integers(
            0, 256, size=(400, 640, 3), dtype=np.uint8
        )
        vehicle_box = [320, 200, 960, 600]

        self.assertFalse(
            credible_vehicle_detection(vehicle_box, frame, [still, still], 0)
        )
        self.assertTrue(
            credible_vehicle_detection(vehicle_box, frame, [still, moved], 0)
        )

    def test_blurry_short_insect_detection_is_not_a_vehicle(self):
        self.assertFalse(label_is_supported("truck", 4, 30, 387, 700))
        self.assertTrue(label_is_supported("truck", 5, 30, 300, 700))
        self.assertFalse(label_is_supported("motorcycle", 1, 30, 4000, 700))
        self.assertTrue(label_is_supported("motorcycle", 2, 30, 4000, 700))

    def test_animals_and_boats_are_not_supported_targets(self):
        self.assertFalse(label_is_supported("dog", 20, 30, 4000, 700))
        self.assertFalse(label_is_supported("bird", 20, 30, 4000, 700))
        self.assertFalse(label_is_supported("boat", 20, 30, 4000, 700))

    def test_static_detection_track_is_rejected_but_moving_track_is_kept(self):
        static = [
            (index, [100 + index, 100, 300 + index, 500]) for index in range(6)
        ]
        moving = [
            (index, [100 + index * 40, 100, 300 + index * 40, 500])
            for index in range(6)
        ]
        frame_shape = (1080, 1920, 3)

        self.assertFalse(
            moving_detection_track("person", static, frame_shape, 30, 0.06)
        )
        self.assertTrue(
            moving_detection_track("person", moving, frame_shape, 30, 0.06)
        )

    def test_nearby_clips_become_one_event(self):
        now = datetime.now(timezone.utc)
        groups = merge_clips(
            [clip(1, now, {"person": 1}), clip(2, now + timedelta(seconds=80), {"person": 1})],
            120,
        )
        self.assertEqual(len(groups), 1)
        event = build_event(groups[0])
        self.assertEqual(event["kind"], "person")
        self.assertEqual(event["clip_ids"], [1, 2])
        self.assertTrue(should_keep(event, keep_unknown_motion=False))

    def test_nearby_unrelated_clips_stay_separate(self):
        now = datetime.now(timezone.utc)
        dog = clip(1, now, {"dog": 1})
        car = clip(2, now + timedelta(seconds=30), {"car": 1})
        self.assertFalse(clips_are_related(dog, car))
        self.assertEqual(len(merge_clips([dog, car], 120)), 2)

    def test_vehicle_then_person_is_one_activity_sequence(self):
        now = datetime.now(timezone.utc)
        car = clip(1, now, {"car": 1})
        person = clip(2, now + timedelta(seconds=30), {"person": 1})
        self.assertTrue(clips_are_related(car, person))
        self.assertEqual(len(merge_clips([car, person], 120)), 1)

    def test_vehicle_event_is_kept(self):
        event = build_event([clip(1, datetime.now(timezone.utc), {"motorcycle": 1})])
        self.assertEqual(event["kind"], "vehicle")
        self.assertTrue(should_keep(event, keep_unknown_motion=False))

    def test_database_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.db")
            now = datetime.now(timezone.utc)
            item = clip(1, now, {"dog": 1})
            item.pop("id")
            item["path"] = str(root / "dog.mp4")
            clip_id = db.add_clip(item)
            self.assertEqual(clip_id, 1)
            stored = db.recent_clips("Outdoor")
            self.assertEqual(stored[0]["labels"], {"dog": 1})

    def test_database_state_prefix_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.db")
            db.set_state("telegram:clip-pending:2", "two")
            db.set_state("telegram:clip-pending:1", "one")
            db.set_state("unrelated", "value")

            self.assertEqual(
                db.list_state("telegram:clip-pending:"),
                [
                    ("telegram:clip-pending:1", "one"),
                    ("telegram:clip-pending:2", "two"),
                ],
            )
            db.delete_state("telegram:clip-pending:1")
            self.assertEqual(
                db.list_state("telegram:clip-pending:"),
                [("telegram:clip-pending:2", "two")],
            )

    def test_relevant_clip_is_queued_and_sent_without_event_merge(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                service = MonitorService(
                    Settings(
                        data_dir=root,
                        video_retention_days=0,
                        telegram_bot_token="token",
                        telegram_chat_id="123",
                    )
                )
                video = root / "raw" / "1" / "clip.mp4"
                video.parent.mkdir(parents=True)
                video.write_bytes(b"video")
                item = clip(7, datetime.now(timezone.utc), {"motorcycle": 1})
                item["path"] = str(video)
                item["camera"] = "1"
                service._queue_clip_notification(item)
                delivered = []

                class Telegram:
                    configured = True

                    async def send_event(self, event):
                        delivered.append(event)
                        return True

                service.telegram = Telegram()
                sent = await service._send_pending_notifications()

                self.assertEqual(sent, 1)
                self.assertEqual(delivered[0]["kind"], "vehicle")
                self.assertEqual(delivered[0]["video_path"], str(video))
                self.assertEqual(
                    service.db.list_state("telegram:clip-pending:"), []
                )
                self.assertIsNotNone(service.db.get_state("telegram:clip-sent:7"))

        asyncio.run(run())

    def test_related_clips_send_one_merged_video_when_already_available(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                service = MonitorService(
                    Settings(
                        data_dir=root,
                        video_retention_days=0,
                        telegram_bot_token="token",
                        telegram_chat_id="123",
                    )
                )
                now = datetime.now(timezone.utc)
                first_video = root / "raw" / "1" / "first.mp4"
                second_video = root / "raw" / "1" / "second.mp4"
                merged_video = root / "events" / "1" / "merged.mp4"
                for video in (first_video, second_video, merged_video):
                    video.parent.mkdir(parents=True, exist_ok=True)
                    video.write_bytes(b"video")
                first = clip(1, now, {"person": 1})
                second = clip(2, now + timedelta(seconds=30), {"person": 1})
                first.update(path=str(first_video), camera="1")
                second.update(path=str(second_video), camera="1")
                service._queue_clip_notification(first)
                service._queue_clip_notification(second)
                service.db.replace_events(
                    "1",
                    [
                        {
                            "started_at": first["captured_at"],
                            "ended_at": second["captured_at"],
                            "kind": "person",
                            "score": 0.9,
                            "anomaly": False,
                            "anomaly_reasons": [],
                            "labels": {"person": 2},
                            "clip_ids": [1, 2],
                            "video_path": str(merged_video),
                        }
                    ],
                )
                delivered = []

                class Telegram:
                    configured = True

                    async def send_event(self, event):
                        delivered.append(event)
                        return True

                service.telegram = Telegram()
                sent = await service._send_pending_notifications()

                self.assertEqual(sent, 1)
                self.assertEqual(len(delivered), 1)
                self.assertEqual(delivered[0]["video_path"], str(merged_video))
                self.assertEqual(
                    service.db.list_state("telegram:clip-pending:"), []
                )

        asyncio.run(run())

    def test_telegram_queue_sends_oldest_clip_first(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                service = MonitorService(
                    Settings(
                        data_dir=root,
                        video_retention_days=0,
                        telegram_bot_token="token",
                        telegram_chat_id="123",
                    )
                )
                now = datetime.now(timezone.utc)
                newer = clip(1, now, {"person": 1})
                older = clip(2, now - timedelta(hours=1), {"person": 1})
                for item, name in ((newer, "newer.mp4"), (older, "older.mp4")):
                    video = root / "raw" / "1" / name
                    video.parent.mkdir(parents=True, exist_ok=True)
                    video.write_bytes(b"video")
                    item.update(path=str(video), camera="1")
                    service._queue_clip_notification(item)
                delivered = []

                class Telegram:
                    configured = True

                    async def send_event(self, event):
                        delivered.append(Path(event["video_path"]).name)
                        return True

                service.telegram = Telegram()
                sent = await service._send_pending_notifications()

                self.assertEqual(sent, 2)
                self.assertEqual(delivered, ["older.mp4", "newer.mp4"])

        asyncio.run(run())

    def test_telegram_queue_sends_while_download_backlog_remains(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                service = MonitorService(
                    Settings(
                        data_dir=root,
                        video_retention_days=0,
                        telegram_bot_token="token",
                        telegram_chat_id="123",
                    )
                )
                video = root / "raw" / "1" / "clip.mp4"
                video.parent.mkdir(parents=True)
                video.write_bytes(b"video")
                item = clip(1, datetime.now(timezone.utc), {"person": 1})
                item.update(path=str(video), camera="1")
                service._queue_clip_notification(item)
                service.downloader.backlog_remaining = True

                delivered = []

                class Telegram:
                    configured = True

                    async def send_event(self, event):
                        delivered.append(event)
                        return True

                service.telegram = Telegram()

                sent = await service._send_pending_notifications()

                self.assertEqual(sent, 1)
                self.assertEqual(len(delivered), 1)
                self.assertEqual(service.db.list_state("telegram:clip-pending:"), [])

        asyncio.run(run())

    def test_retention_deletes_only_expired_videos_and_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw" / "1"
            rejected = root / "rejected" / "1"
            events_dir = root / "events" / "1"
            for path in (raw, rejected, events_dir):
                path.mkdir(parents=True)

            old_video = raw / "20260101_010000_1.mp4"
            old_rejected = rejected / "20260101_010100_1.mp4"
            old_event = events_dir / "20260101-010000-person.mp4"
            recent_video = raw / "20260720_010000_1.mp4"
            for path in (old_video, old_rejected, old_event, recent_video):
                path.write_bytes(b"video")

            cutoff = datetime(2026, 4, 22, tzinfo=timezone.utc)
            stats = delete_expired_videos(
                (root / "raw", root / "rejected", root / "events"),
                cutoff,
                "UTC",
            )
            self.assertEqual(stats["files"], 3)
            self.assertFalse(old_video.exists())
            self.assertFalse(old_rejected.exists())
            self.assertFalse(old_event.exists())
            self.assertTrue(recent_video.exists())

            db = Database(root / "sentinel.db")
            old = clip(1, datetime(2026, 1, 1, tzinfo=timezone.utc), {"person": 1})
            old.pop("id")
            old["path"] = str(old_video)
            recent = clip(2, datetime(2026, 7, 20, tzinfo=timezone.utc), {"person": 1})
            recent.pop("id")
            recent["path"] = str(recent_video)
            db.add_clip(old)
            db.add_clip(recent)
            db.replace_events(
                "Outdoor",
                [
                    {
                        "camera": "Outdoor",
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "ended_at": "2026-01-01T00:00:10+00:00",
                        "kind": "person",
                        "score": 0.9,
                        "anomaly": False,
                        "anomaly_reasons": [],
                        "labels": {"person": 1},
                        "clip_ids": [1],
                        "video_path": str(old_event),
                    },
                    {
                        "camera": "Outdoor",
                        "started_at": "2026-07-20T00:00:00+00:00",
                        "ended_at": "2026-07-20T00:00:10+00:00",
                        "kind": "person",
                        "score": 0.9,
                        "anomaly": False,
                        "anomaly_reasons": [],
                        "labels": {"person": 1},
                        "clip_ids": [2],
                        "video_path": str(recent_video),
                    },
                ],
            )
            deleted = db.delete_before(cutoff)
            self.assertEqual(deleted["clips"], 1)
            self.assertEqual(deleted["events"], 1)
            self.assertEqual(len(db.recent_clips("Outdoor")), 1)
            self.assertEqual(len(db.list_events()), 1)

    def test_telegram_configuration_and_caption(self):
        notifier = TelegramNotifier("token", "123", "Asia/Seoul")
        self.assertTrue(notifier.configured)
        text = notifier.caption(
            {
                "camera": "1",
                "kind": "person",
                "started_at": "2026-07-18T01:00:00+00:00",
                "labels": {"person": 1},
                "anomaly": True,
                "model_votes": {
                    "yolo": {"status": "negative"},
                    "rfdetr": {"status": "positive"},
                },
            }
        )
        self.assertIn("Person", text)
        self.assertIn("Anomaly", text)
        self.assertIn("YOLO ❌", text)
        self.assertIn("RF-DETR Small ✅", text)
        self.assertIn("Positive (RF-DETR Small)", text)
        pending_text = notifier.caption(
            {
                "camera": "1",
                "kind": "person",
                "started_at": "2026-07-18T01:00:00+00:00",
                "labels": {"person": 1},
                "anomaly": False,
                "model_votes": {
                    "yolo": {"status": "positive"},
                    "rfdetr": {"status": "pending"},
                },
            }
        )
        self.assertIn("YOLO ✅", pending_text)
        self.assertIn("RF-DETR Small ⏳", pending_text)


if __name__ == "__main__":
    unittest.main()
