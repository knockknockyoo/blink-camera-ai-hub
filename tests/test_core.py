from __future__ import annotations

import tempfile
import unittest
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

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
from backend.events import build_event, clips_are_related, merge_clips, should_keep
from backend.retention import delete_expired_videos
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
            downloader = BlinkDownloader(root / "auth.json", root / "raw")
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
            }
        )
        self.assertIn("Person", text)
        self.assertIn("Anomaly", text)


if __name__ == "__main__":
    unittest.main()
