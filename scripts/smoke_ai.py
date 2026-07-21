from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from backend.analyzer import VideoAnalyzer
from backend.events import concatenate


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "motion.mp4"
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 12, (320, 192))
        for index in range(36):
            frame = np.zeros((192, 320, 3), dtype=np.uint8)
            x = 20 + index * 5
            cv2.rectangle(frame, (x, 70), (x + 45, 125), (190, 220, 190), -1)
            writer.write(frame)
        writer.release()
        result = VideoAnalyzer("yolo11n.pt", 0.42, 2).analyze(path, datetime.now(timezone.utc))
        assert "motion_score" in result
        merged = concatenate([path, path], Path(directory) / "merged.mp4")
        assert merged is not None and merged.exists() and merged.stat().st_size > path.stat().st_size
        print(result)


if __name__ == "__main__":
    main()
