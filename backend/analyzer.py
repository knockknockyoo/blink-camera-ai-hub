from __future__ import annotations

import math
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ANIMALS = {
    "bird",
    "cat",
    "dog",
    "horse",
    "sheep",
    "cow",
    "elephant",
    "bear",
    "zebra",
    "giraffe",
}
VEHICLES = {"bicycle", "car", "motorcycle", "bus", "truck", "boat"}
INTEREST_LABELS = {"person", *ANIMALS, *VEHICLES}


def classify_event(labels: dict[str, int], motion_score: float) -> str:
    if labels.get("person", 0):
        return "person"
    if any(labels.get(name, 0) for name in ANIMALS):
        return "animal"
    if any(labels.get(name, 0) for name in VEHICLES):
        return "vehicle"
    if motion_score >= 0.02:
        return "motion"
    return "noise"


def anomaly_reasons(
    labels: dict[str, int],
    captured_at: datetime,
    motion_score: float,
    repeated_activity: bool = False,
    timezone_name: str = "UTC",
) -> list[str]:
    reasons: list[str] = []
    people = labels.get("person", 0)
    local_time = captured_at.astimezone(ZoneInfo(timezone_name))
    if people and (local_time.hour >= 22 or local_time.hour < 6):
        reasons.append("야간 사람 감지")
    if people >= 2:
        reasons.append("여러 사람 동시 감지")
    if repeated_activity:
        reasons.append("짧은 시간 반복 활동")
    # A general-purpose model can call a small utility vehicle a boat or another
    # unrelated class. Treat significant motion with no useful target as an
    # anomaly even when such an irrelevant label is present.
    if not any(labels.get(name, 0) for name in INTEREST_LABELS) and motion_score >= 0.06:
        reasons.append("큰 미분류 움직임")
    return reasons


def credible_person_detection(
    box: list[float],
    frame_shape: tuple[int, ...],
    gray_frames: list[Any],
    frame_index: int,
    min_area: float = 0.01,
    min_motion: float = 0.12,
) -> bool:
    """Reject small, static objects that a general model mistakes for people."""
    frame_height, frame_width = frame_shape[:2]
    x1, y1, x2, y2 = box
    normalized_area = max(0.0, x2 - x1) * max(0.0, y2 - y1) / (
        frame_width * frame_height
    )
    motion = box_motion_fraction(box, frame_shape, gray_frames, frame_index)
    if normalized_area >= min_area:
        # Blink records because something moved. A large but completely static
        # "person" is usually a tool, statue, or other fixed yard object.
        return motion >= min(min_motion, 0.01)

    return motion >= min_motion


def box_motion_fraction(
    box: list[float],
    frame_shape: tuple[int, ...],
    gray_frames: list[Any],
    frame_index: int,
) -> float:
    """Return the strongest adjacent-frame motion inside a detection box."""
    frame_height, frame_width = frame_shape[:2]
    x1, y1, x2, y2 = box
    gray_height, gray_width = gray_frames[0].shape[:2]
    gx1 = max(0, min(gray_width, int(x1 / frame_width * gray_width)))
    gx2 = max(0, min(gray_width, int(x2 / frame_width * gray_width)))
    gy1 = max(0, min(gray_height, int(y1 / frame_height * gray_height)))
    gy2 = max(0, min(gray_height, int(y2 / frame_height * gray_height)))
    motion_values: list[float] = []
    for before_index in (frame_index - 1, frame_index):
        if not 0 <= before_index < len(gray_frames) - 1:
            continue
        import cv2
        import numpy as np

        delta = cv2.absdiff(gray_frames[before_index], gray_frames[before_index + 1])
        # IR mode/exposure changes can alter nearly the entire image at once.
        # That is a lighting transition, not motion inside an object box.
        if float(np.mean(delta > 24)) >= 0.35:
            continue
        crop = delta[gy1:gy2, gx1:gx2]
        if crop.size:
            motion_values.append(float(np.mean(crop > 24)))
    return max(motion_values, default=0.0)


def credible_vehicle_detection(
    box: list[float],
    frame: Any,
    gray_frames: list[Any],
    frame_index: int,
    min_motion: float = 0.01,
) -> bool:
    """Ignore a parked vehicle unless the detected vehicle region moves."""
    return box_motion_fraction(box, frame.shape, gray_frames, frame_index) >= min_motion


def detection_sharpness(box: list[float], frame: Any) -> float:
    """Measure focus inside a detection box using Laplacian variance."""
    import cv2

    frame_height, frame_width = frame.shape[:2]
    x1, y1, x2, y2 = box
    ix1 = max(0, min(frame_width, int(x1)))
    ix2 = max(0, min(frame_width, int(x2)))
    iy1 = max(0, min(frame_height, int(y1)))
    iy2 = max(0, min(frame_height, int(y2)))
    crop = frame[iy1:iy2, ix1:ix2]
    if not crop.size:
        return 0.0
    gray_crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray_crop, cv2.CV_64F).var())


def label_is_supported(
    label: str,
    count: int,
    frame_count: int,
    vehicle_sharpness: float,
    min_vehicle_sharpness: float,
) -> bool:
    if label in VEHICLES:
        # A blurry insect usually disappears within a few frames. A low-focus
        # vehicle must persist, while a sharp fast target may appear once.
        required = min(5, max(2, math.ceil(frame_count * 0.18)))
        return count >= required or vehicle_sharpness >= min_vehicle_sharpness
    if label in INTEREST_LABELS:
        return count >= 1
    required = 1 if frame_count <= 2 else max(2, math.ceil(frame_count * 0.18))
    return count >= required


class VideoAnalyzer:
    """Samples video frames and keeps detections that persist across frames."""

    def __init__(
        self,
        model_name: str,
        confidence: float,
        sample_fps: float,
        timezone_name: str = "UTC",
        person_min_area: float = 0.01,
        person_min_box_motion: float = 0.12,
        vehicle_min_box_motion: float = 0.01,
        vehicle_min_sharpness: float = 700.0,
    ):
        self.model_name = model_name
        self.confidence = confidence
        self.sample_fps = sample_fps
        self.timezone_name = timezone_name
        self.person_min_area = person_min_area
        self.person_min_box_motion = person_min_box_motion
        self.vehicle_min_box_motion = vehicle_min_box_motion
        self.vehicle_min_sharpness = vehicle_min_sharpness
        self._model: Any | None = None

    def _load_model(self) -> Any:
        if self._model is None:
            try:
                from ultralytics import YOLO
            except ImportError as exc:
                raise RuntimeError(
                    "AI 패키지가 설치되지 않았습니다. bash scripts/setup.sh를 먼저 실행하세요."
                ) from exc
            self._model = YOLO(self.model_name)
        return self._model

    def analyze(self, path: Path, captured_at: datetime) -> dict[str, Any]:
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("OpenCV가 설치되지 않았습니다.") from exc

        capture = cv2.VideoCapture(str(path))
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 15.0)
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        duration = frame_count / fps if fps else 0.0
        every = max(1, int(fps / max(self.sample_fps, 0.25)))
        frames: list[Any] = []
        gray_frames: list[Any] = []
        index = 0
        while capture.isOpened():
            ok, frame = capture.read()
            if not ok:
                break
            if index % every == 0:
                frames.append(frame)
                gray_frames.append(cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90)))
            index += 1
        capture.release()

        if not frames:
            return {
                "duration": duration,
                "labels": {},
                "score": 0.0,
                "motion_score": 0.0,
                "anomaly": False,
                "anomaly_reasons": [],
            }

        motion_values: list[float] = []
        for before, after in zip(gray_frames, gray_frames[1:]):
            delta = cv2.absdiff(before, after)
            motion_values.append(float(np.mean(delta > 24)))
        motion_score = float(sum(motion_values) / len(motion_values)) if motion_values else 0.0

        model = self._load_model()
        results = model.predict(frames, conf=self.confidence, verbose=False)
        per_frame: list[set[str]] = []
        max_instances: Counter[str] = Counter()
        vehicle_sharpness: dict[str, float] = {}
        max_confidence = 0.0
        for frame_index, result in enumerate(results):
            names = result.names
            frame_labels: list[str] = []
            if result.boxes is not None:
                for box, cls_id, confidence in zip(
                    result.boxes.xyxy.tolist(),
                    result.boxes.cls.tolist(),
                    result.boxes.conf.tolist(),
                ):
                    label = str(names[int(cls_id)])
                    if label == "person" and not credible_person_detection(
                        box,
                        frames[frame_index].shape,
                        gray_frames,
                        frame_index,
                        self.person_min_area,
                        self.person_min_box_motion,
                    ):
                        continue
                    if label in VEHICLES and not credible_vehicle_detection(
                        box,
                        frames[frame_index],
                        gray_frames,
                        frame_index,
                        self.vehicle_min_box_motion,
                    ):
                        continue
                    if label in VEHICLES:
                        vehicle_sharpness[label] = max(
                            vehicle_sharpness.get(label, 0.0),
                            detection_sharpness(box, frames[frame_index]),
                        )
                    frame_labels.append(label)
                    max_confidence = max(max_confidence, float(confidence))
            for label, count in Counter(frame_labels).items():
                max_instances[label] = max(max_instances[label], count)
            per_frame.append(set(frame_labels))

        occurrences = Counter(label for labels in per_frame for label in labels)
        labels = {
            label: max_instances[label]
            for label, count in occurrences.items()
            if label_is_supported(
                label,
                count,
                len(per_frame),
                vehicle_sharpness.get(label, 0.0),
                self.vehicle_min_sharpness,
            )
        }

        reasons = anomaly_reasons(
            labels,
            captured_at,
            motion_score,
            timezone_name=self.timezone_name,
        )
        return {
            "duration": round(duration, 2),
            "labels": labels,
            "score": round(max_confidence, 3),
            "motion_score": round(motion_score, 4),
            "anomaly": bool(reasons),
            "anomaly_reasons": reasons,
        }
