from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    camera TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    duration REAL NOT NULL DEFAULT 0,
    labels_json TEXT NOT NULL DEFAULT '{}',
    score REAL NOT NULL DEFAULT 0,
    motion_score REAL NOT NULL DEFAULT 0,
    anomaly INTEGER NOT NULL DEFAULT 0,
    anomaly_reasons_json TEXT NOT NULL DEFAULT '[]',
    analyzed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    anomaly INTEGER NOT NULL DEFAULT 0,
    anomaly_reasons_json TEXT NOT NULL DEFAULT '[]',
    labels_json TEXT NOT NULL DEFAULT '{}',
    clip_ids_json TEXT NOT NULL DEFAULT '[]',
    video_path TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def clip_exists(self, path: Path) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT 1 FROM clips WHERE path = ?", (str(path),)).fetchone()
        return row is not None

    def add_clip(self, clip: dict[str, Any]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO clips (
                    path, camera, captured_at, duration, labels_json, score,
                    motion_score, anomaly, anomaly_reasons_json, analyzed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clip["path"],
                    clip["camera"],
                    clip["captured_at"],
                    clip.get("duration", 0),
                    json.dumps(clip.get("labels", {}), ensure_ascii=False),
                    clip.get("score", 0),
                    clip.get("motion_score", 0),
                    int(clip.get("anomaly", False)),
                    json.dumps(clip.get("anomaly_reasons", []), ensure_ascii=False),
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def recent_clips(self, camera: str, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM clips WHERE camera = ? ORDER BY captured_at DESC LIMIT ?",
                (camera, limit),
            ).fetchall()
        return [self._decode_clip(row) for row in reversed(rows)]

    def update_clip_path(self, clip_id: int, path: Path) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE clips SET path = ? WHERE id = ?", (str(path), clip_id))

    def update_clip_analysis(self, clip_id: int, clip: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE clips
                SET duration = ?, labels_json = ?, score = ?, motion_score = ?,
                    anomaly = ?, anomaly_reasons_json = ?, analyzed_at = ?
                WHERE id = ?
                """,
                (
                    clip.get("duration", 0),
                    json.dumps(clip.get("labels", {}), ensure_ascii=False),
                    clip.get("score", 0),
                    clip.get("motion_score", 0),
                    int(clip.get("anomaly", False)),
                    json.dumps(
                        clip.get("anomaly_reasons", []),
                        ensure_ascii=False,
                    ),
                    now,
                    clip_id,
                ),
            )

    def replace_events(self, camera: str, events: list[dict[str, Any]]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute("DELETE FROM events WHERE camera = ?", (camera,))
            for event in events:
                conn.execute(
                    """
                    INSERT INTO events (
                        camera, started_at, ended_at, kind, score, anomaly,
                        anomaly_reasons_json, labels_json, clip_ids_json,
                        video_path, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        camera,
                        event["started_at"],
                        event["ended_at"],
                        event["kind"],
                        event["score"],
                        int(event["anomaly"]),
                        json.dumps(event["anomaly_reasons"], ensure_ascii=False),
                        json.dumps(event["labels"], ensure_ascii=False),
                        json.dumps(event["clip_ids"]),
                        event.get("video_path"),
                        now,
                    ),
                )

    def list_events(self, limit: int = 100, important_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE kind = 'person' OR anomaly = 1" if important_only else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM events {where} ORDER BY started_at DESC LIMIT ?",  # noqa: S608
                (limit,),
            ).fetchall()
        return [self._decode_event(row) for row in rows]

    def find_event_for_clip(self, clip_id: int) -> dict[str, Any] | None:
        for event in self.list_events(limit=5000):
            if clip_id in event["clip_ids"]:
                return event
        return None

    def counts(self) -> dict[str, int]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) total,
                    SUM(CASE WHEN kind = 'person' THEN 1 ELSE 0 END) people,
                    SUM(CASE WHEN kind = 'animal' THEN 1 ELSE 0 END) animals,
                    SUM(anomaly) anomalies
                FROM events
                """
            ).fetchone()
        return {key: int(row[key] or 0) for key in ("total", "people", "animals", "anomalies")}

    def clear_demo_events(self) -> None:
        """Remove placeholder rows once a real Blink account is active."""
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM events WHERE clip_ids_json = '[]' AND video_path IS NULL"
            )

    def clear_analysis(self) -> None:
        """Clear derived rows while preserving downloaded source videos."""
        with self.connect() as conn:
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM clips")

    def delete_before(self, cutoff: datetime) -> dict[str, int]:
        """Delete clip and event metadata older than the retention cutoff."""
        cutoff_value = cutoff.astimezone(timezone.utc).isoformat()
        with self.connect() as conn:
            events = conn.execute(
                "DELETE FROM events WHERE julianday(started_at) < julianday(?)",
                (cutoff_value,),
            ).rowcount
            clips = conn.execute(
                "DELETE FROM clips WHERE julianday(captured_at) < julianday(?)",
                (cutoff_value,),
            ).rowcount
        return {"clips": max(0, clips), "events": max(0, events)}

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def list_state(self, prefix: str) -> list[tuple[str, str]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM state WHERE key LIKE ? ORDER BY key",
                (f"{prefix}%",),
            ).fetchall()
        return [(str(row["key"]), str(row["value"])) for row in rows]

    def delete_state(self, key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM state WHERE key = ?", (key,))

    @staticmethod
    def _decode_clip(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["labels"] = json.loads(item.pop("labels_json"))
        item["anomaly_reasons"] = json.loads(item.pop("anomaly_reasons_json"))
        item["anomaly"] = bool(item["anomaly"])
        return item

    @staticmethod
    def _decode_event(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["labels"] = json.loads(item.pop("labels_json"))
        item["clip_ids"] = json.loads(item.pop("clip_ids_json"))
        item["anomaly_reasons"] = json.loads(item.pop("anomaly_reasons_json"))
        item["anomaly"] = bool(item["anomaly"])
        return item
