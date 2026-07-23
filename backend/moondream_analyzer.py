from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from .analyzer import (
    VEHICLES,
    VideoAnalyzer,
    anomaly_reasons,
    credible_person_detection,
    credible_vehicle_detection,
    detection_sharpness,
    label_is_supported,
    moving_detection_track,
)


class MoondreamVideoAnalyzer(VideoAnalyzer):
    """Detect people and moving vehicles in representative video frames."""

    VEHICLE_PROMPT = "road vehicle, car, truck, bus, motorcycle, or bicycle"

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            import torch
            from transformers import AutoModelForCausalLM
        except ImportError as exc:
            raise RuntimeError(
                "Moondream dependencies are missing. "
                "Install requirements-native.txt on the Mac host."
            ) from exc

        device = self.device or "mps"
        if device == "mps" and not torch.backends.mps.is_available():
            raise RuntimeError(
                "Apple MPS is unavailable. Run the native AI service with an "
                "Apple Silicon Python build outside Docker."
            )
        self._model = AutoModelForCausalLM.from_pretrained(
            self.model_name,
            revision="2025-01-09",
            trust_remote_code=True,
            dtype=torch.bfloat16,
            device_map=device,
        )
        self._model.eval()
        return self._model

    @staticmethod
    def _boxes(
        model: Any,
        image: Any,
        prompt: str,
        width: int,
        height: int,
    ) -> list[list[float]]:
        response = model.detect(image, prompt, settings={"max_objects": 10})
        return [
            [
                float(item["x_min"]) * width,
                float(item["y_min"]) * height,
                float(item["x_max"]) * width,
                float(item["y_max"]) * height,
            ]
            for item in response.get("objects", [])
        ]

    def analyze(self, path: Path, captured_at: datetime) -> dict[str, Any]:
        try:
            import cv2
            import numpy as np
            import torch
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError("Moondream video dependencies are missing.") from exc

        capture = cv2.VideoCapture(str(path))
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 15.0)
        source_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        duration = source_count / fps if fps else 0.0
        target_count = max(1, self.max_frames or 6)
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
                    cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90))
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
            float(sum(motion_values) / len(motion_values)) if motion_values else 0.0
        )

        model = self._load_model()
        per_frame: list[set[str]] = []
        max_instances: Counter[str] = Counter()
        sharpness: dict[str, float] = {}
        detection_boxes: dict[str, list[tuple[int, list[float]]]] = {}

        with torch.inference_mode():
            for frame_index, frame in enumerate(frames):
                height, width = frame.shape[:2]
                image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                detected: list[str] = []

                for box in self._boxes(model, image, "person", width, height):
                    if not credible_person_detection(
                        box,
                        frame.shape,
                        gray_frames,
                        frame_index,
                        self.person_min_area,
                        self.person_min_box_motion,
                    ):
                        continue
                    detected.append("person")
                    detection_boxes.setdefault("person", []).append(
                        (frame_index, box)
                    )

                for box in self._boxes(
                    model, image, self.VEHICLE_PROMPT, width, height
                ):
                    if not credible_vehicle_detection(
                        box,
                        frame,
                        gray_frames,
                        frame_index,
                        self.vehicle_min_box_motion,
                    ):
                        continue
                    label = "car"
                    detected.append(label)
                    detection_boxes.setdefault(label, []).append((frame_index, box))
                    sharpness[label] = max(
                        sharpness.get(label, 0.0),
                        detection_sharpness(box, frame),
                    )

                for label, count in Counter(detected).items():
                    max_instances[label] = max(max_instances[label], count)
                per_frame.append(set(detected))

        occurrences = Counter(label for labels in per_frame for label in labels)
        labels = {
            label: max_instances[label]
            for label, count in occurrences.items()
            if (label == "person" or label in VEHICLES)
            and label_is_supported(
                label,
                count,
                len(per_frame),
                sharpness.get(label, 0.0),
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
            "score": 1.0 if labels else 0.0,
            "motion_score": round(motion_score, 4),
            "anomaly": bool(reasons),
            "anomaly_reasons": reasons,
        }
