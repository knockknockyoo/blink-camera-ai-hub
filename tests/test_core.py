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
from backend.events import build_event, clips_are_related, merge_clips, should_keep
from backend.retention import delete_expired_videos
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
        self.assertIn("야간 사람 감지", anomaly_reasons({"person": 1}, night, 0.1))
        self.assertIn("여러 사람 동시 감지", anomaly_reasons({"person": 2}, night, 0.1))
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
        self.assertIn("사람", text)
        self.assertIn("이상징후", text)


if __name__ == "__main__":
    unittest.main()
