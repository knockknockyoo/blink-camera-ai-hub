from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .db import Database


def seed_demo(db: Database) -> None:
    if db.list_events(limit=1):
        return
    now = datetime.now(timezone.utc)
    events = [
        {
            "started_at": (now - timedelta(minutes=18)).isoformat(),
            "ended_at": (now - timedelta(minutes=16)).isoformat(),
            "kind": "person",
            "score": 0.94,
            "anomaly": True,
            "anomaly_reasons": ["짧은 시간 반복 활동"],
            "labels": {"person": 1},
            "clip_ids": [],
            "video_path": None,
        },
        {
            "started_at": (now - timedelta(hours=2, minutes=7)).isoformat(),
            "ended_at": (now - timedelta(hours=2, minutes=7)).isoformat(),
            "kind": "animal",
            "score": 0.89,
            "anomaly": False,
            "anomaly_reasons": [],
            "labels": {"dog": 1},
            "clip_ids": [],
            "video_path": None,
        },
        {
            "started_at": (now - timedelta(hours=5)).isoformat(),
            "ended_at": (now - timedelta(hours=5)).isoformat(),
            "kind": "motion",
            "score": 0.0,
            "anomaly": True,
            "anomaly_reasons": ["큰 미분류 움직임"],
            "labels": {},
            "clip_ids": [],
            "video_path": None,
        },
    ]
    db.replace_events("Outdoor", events)
