from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from .analyzer import (
    INTEREST_LABELS,
    VEHICLES,
    VideoAnalyzer,
    anomaly_reasons,
    credible_person_detection,
    credible_vehicle_detection,
    detection_sharpness,
    label_is_supported,
    moving_detection_track,
)


class RFDETRVideoAnalyzer(VideoAnalyzer):
    """Analyze representative video frames with RF-DETR on Apple MPS."""

    MODEL_CLASSES = {
        "nano": "RFDETRNano",
        "small": "RFDETRSmall",
        "medium": "RFDETRMedium",
        "large": "RFDETRLarge",
    }

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            import rfdetr
        except ImportError as exc:
            raise RuntimeError(
                "RF-DETR is not installed. Run bash scripts/install_native_ai.sh."
            ) from exc

        size = self.model_name.strip().lower()
        class_name = self.MODEL_CLASSES.get(size)
        if class_name is None:
            choices = ", ".join(self.MODEL_CLASSES)
            raise RuntimeError(
                f"Unsupported RF-DETR model size '{self.model_name}'. "
                f"Choose one of: {choices}."
            )
        self._model = getattr(rfdetr, class_name)()
        return self._model

    def analyze(self, path: Path, captured_at: datetime) -> dict[str, Any]:
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("RF-DETR video dependencies are missing.") from exc

        capture = cv2.VideoCapture(str(path))
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 15.0)
        source_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        duration = source_count / fps if fps else 0.0
        target_count = max(1, self.max_frames or 16)
        indexes = {
            round(index * max(0, source_count - 1) / max(1, target_count - 1))
            for index in range(min(target_count, max(1, source_count)))
        }
        frames: list[Any] = []
        gray_frames: list[Any] = []
        index = 0
        while capture.isOpened():
            ok, frame = capture.read()
            if not ok:
                break
            if index in indexes:
                frames.append(frame)
                gray_frames.append(
                    cv2.resize(
                        cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY),
                        (160, 90),
                    )
                )
            index += 1
        capture.release()

        if not frames:
            return {
                "duration": round(duration, 2),
                "labels": {},
                "score": 0.0,
                "motion_score": 0.0,
                "anomaly": False,
                "anomaly_reasons": [],
            }

        motion_values = [
            float(np.mean(cv2.absdiff(before, after) > 24))
            for before, after in zip(gray_frames, gray_frames[1:])
        ]
        motion_score = (
            float(sum(motion_values) / len(motion_values))
            if motion_values
            else 0.0
        )

        rgb_frames = [
            cv2.cvtColor(frame, cv2.COLOR_BGR2RGB) for frame in frames
        ]
        detections = self._load_model().predict(
            rgb_frames,
            threshold=self.confidence,
            include_source_image=False,
        )
        if not isinstance(detections, list):
            detections = [detections]

        per_frame: list[set[str]] = []
        max_instances: Counter[str] = Counter()
        vehicle_sharpness: dict[str, float] = {}
        detection_boxes: dict[str, list[tuple[int, list[float]]]] = {}
        max_confidence = 0.0

        for frame_index, frame_detections in enumerate(detections):
            names = frame_detections.data.get("class_name", [])
            confidences = frame_detections.confidence
            if confidences is None:
                confidences = [1.0] * len(frame_detections.xyxy)
            frame_labels: list[str] = []
            for box, raw_label, confidence in zip(
                frame_detections.xyxy.tolist(),
                list(names),
                list(confidences),
            ):
                label = str(raw_label).strip().lower()
                if label == "motorbike":
                    label = "motorcycle"
                if label not in INTEREST_LABELS:
                    continue
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
                detection_boxes.setdefault(label, []).append(
                    (frame_index, box)
                )
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
            and moving_detection_track(
                label,
                detection_boxes.get(label, []),
                frames[0].shape,
                len(per_frame),
                (
                    self.person_min_box_motion
                    if label == "person"
                    else self.vehicle_min_box_motion
                ),
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
