from __future__ import annotations

import shutil
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from .analyzer import ANIMALS, VEHICLES, classify_event


def _target_families(labels: dict[str, int]) -> set[str]:
    families: set[str] = set()
    if labels.get("person", 0):
        families.add("person")
    if any(labels.get(name, 0) for name in ANIMALS):
        families.add("animal")
    if any(labels.get(name, 0) for name in VEHICLES):
        families.add("vehicle")
    return families


def clips_are_related(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    """Decide whether nearby clips represent the same activity sequence."""
    if previous.get("camera") != current.get("camera"):
        return False
    previous_labels = previous.get("labels", {})
    current_labels = current.get("labels", {})
    if set(previous_labels) & set(current_labels):
        return True

    previous_families = _target_families(previous_labels)
    current_families = _target_families(current_labels)
    if previous_families & current_families:
        return True

    # A person immediately before/after a vehicle is a useful arrival or
    # departure sequence even if YOLO sees the two in different clips.
    return (
        "person" in previous_families
        and "vehicle" in current_families
        or "vehicle" in previous_families
        and "person" in current_families
    )


def merge_clips(clips: list[dict[str, Any]], window_seconds: int) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    for clip in sorted(clips, key=lambda item: item["captured_at"]):
        if not groups:
            groups.append([clip])
            continue
        previous = groups[-1][-1]
        gap = (
            datetime.fromisoformat(clip["captured_at"])
            - datetime.fromisoformat(previous["captured_at"])
        ).total_seconds()
        if gap <= window_seconds and clips_are_related(previous, clip):
            groups[-1].append(clip)
        else:
            groups.append([clip])
    return groups


def build_event(group: list[dict[str, Any]]) -> dict[str, Any]:
    labels: Counter[str] = Counter()
    reasons: list[str] = []
    for clip in group:
        labels.update(clip["labels"])
        reasons.extend(clip["anomaly_reasons"])
    if len(group) >= 3:
        reasons.append("짧은 시간 반복 활동")
    labels_dict = dict(labels)
    kind = classify_event(labels_dict, max(clip["motion_score"] for clip in group))
    return {
        "started_at": group[0]["captured_at"],
        "ended_at": group[-1]["captured_at"],
        "kind": kind,
        "score": max(clip["score"] for clip in group),
        "anomaly": bool(reasons),
        "anomaly_reasons": list(dict.fromkeys(reasons)),
        "labels": labels_dict,
        "clip_ids": [clip["id"] for clip in group],
        "source_paths": [clip["path"] for clip in group],
    }


def should_keep(event: dict[str, Any], keep_unknown_motion: bool) -> bool:
    if event["kind"] in {"person", "animal", "vehicle"}:
        return True
    if event["anomaly"]:
        return True
    return keep_unknown_motion and event["kind"] == "motion"


def concatenate(paths: list[Path], output: Path) -> Path | None:
    existing = [path for path in paths if path.exists()]
    if not existing:
        return None
    output.parent.mkdir(parents=True, exist_ok=True)
    if len(existing) == 1:
        if output.exists():
            output.unlink()
        try:
            output.hardlink_to(existing[0])
        except OSError:
            shutil.copy2(existing[0], output)
        return output

    try:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return existing[0]

    list_file = output.with_suffix(".txt")
    lines = [f"file '{str(path.resolve()).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'" for path in existing]
    list_file.write_text("\n".join(lines), encoding="utf-8")
    copy_command = [
        ffmpeg,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c",
        "copy",
        str(output),
    ]
    result = subprocess.run(copy_command, capture_output=True, check=False)
    if result.returncode != 0:
        transcode_command = copy_command[:-3] + [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-c:a",
            "aac",
            str(output),
        ]
        result = subprocess.run(transcode_command, capture_output=True, check=False)
    list_file.unlink(missing_ok=True)
    return output if result.returncode == 0 and output.exists() else existing[0]


def primary_animal(labels: dict[str, int]) -> str | None:
    matches = [(count, label) for label, count in labels.items() if label in ANIMALS]
    return max(matches)[1] if matches else None
