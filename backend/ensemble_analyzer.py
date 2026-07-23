from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable


LOGGER = logging.getLogger("blink-camera-ai-hub")
MODEL_ORDER = ("yolo", "moondream2")


class EnsembleVideoAnalyzer:
    """Run YOLO and Moondream2 concurrently and accept either positive result."""

    def __init__(self, yolo: Any, moondream2: Any):
        self.analyzers = {
            "yolo": yolo,
            "moondream2": moondream2,
        }

    @staticmethod
    def _vote(result: dict[str, Any]) -> dict[str, Any]:
        labels = result.get("labels", {})
        return {
            "status": "positive" if labels else "negative",
            "labels": labels,
            "score": float(result.get("score", 0.0)),
        }

    @staticmethod
    def _combine(
        results: dict[str, dict[str, Any]],
        votes: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        if not results:
            raise RuntimeError("Both AI models failed.")

        labels: dict[str, int] = {}
        reasons: list[str] = []
        for name in MODEL_ORDER:
            result = results.get(name)
            if not result:
                continue
            for label, count in result.get("labels", {}).items():
                labels[label] = max(labels.get(label, 0), int(count))
            reasons.extend(result.get("anomaly_reasons", []))

        detected_by = [
            name
            for name in MODEL_ORDER
            if votes.get(name, {}).get("status") == "positive"
        ]
        return {
            "duration": round(
                max(float(result.get("duration", 0.0)) for result in results.values()),
                2,
            ),
            "labels": labels,
            "score": round(
                max(float(result.get("score", 0.0)) for result in results.values()),
                3,
            ),
            "motion_score": round(
                max(
                    float(result.get("motion_score", 0.0))
                    for result in results.values()
                ),
                4,
            ),
            "anomaly": any(
                bool(result.get("anomaly", False)) for result in results.values()
            ),
            "anomaly_reasons": list(dict.fromkeys(reasons)),
            "model_votes": votes,
            "detected_by": detected_by,
        }

    @staticmethod
    def _log_failure(name: str, path: Path, exc: BaseException) -> None:
        LOGGER.error(
            "[AI ensemble] %s failed for %s; using the other model: %s",
            name,
            path.name,
            exc,
        )

    def analyze(self, path: Path, captured_at: datetime) -> dict[str, Any]:
        results: dict[str, dict[str, Any]] = {}
        votes: dict[str, dict[str, Any]] = {}

        with ThreadPoolExecutor(
            max_workers=len(self.analyzers),
            thread_name_prefix="model-vote",
        ) as executor:
            futures = {
                executor.submit(analyzer.analyze, path, captured_at): name
                for name, analyzer in self.analyzers.items()
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    votes[name] = {"status": "error", "labels": {}, "score": 0.0}
                    self._log_failure(name, path, exc)
                    continue
                results[name] = result
                votes[name] = self._vote(result)

        if not results:
            raise RuntimeError(f"Both AI models failed for {path.name}.")
        return self._combine(results, votes)

    async def analyze_async(
        self,
        path: Path,
        captured_at: datetime,
        on_first_positive: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Analyze concurrently and report the first positive before its peer ends."""
        results: dict[str, dict[str, Any]] = {}
        votes: dict[str, dict[str, Any]] = {}
        tasks = {
            asyncio.create_task(
                asyncio.to_thread(analyzer.analyze, path, captured_at),
                name=f"{name}-{path.name}",
            ): name
            for name, analyzer in self.analyzers.items()
        }
        notified = False

        while tasks:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                name = tasks.pop(task)
                try:
                    result = task.result()
                except Exception as exc:
                    votes[name] = {"status": "error", "labels": {}, "score": 0.0}
                    self._log_failure(name, path, exc)
                    continue
                results[name] = result
                votes[name] = self._vote(result)

            has_positive = any(
                vote.get("status") == "positive" for vote in votes.values()
            )
            if has_positive and not notified and on_first_positive is not None:
                partial_votes = dict(votes)
                for name in tasks.values():
                    partial_votes[name] = {
                        "status": "pending",
                        "labels": {},
                        "score": 0.0,
                    }
                await on_first_positive(self._combine(results, partial_votes))
                notified = True

        if not results:
            raise RuntimeError(f"Both AI models failed for {path.name}.")
        return self._combine(results, votes)
