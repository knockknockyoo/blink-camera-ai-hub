from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class RemoteVideoAnalyzer:
    """Call the macOS-native AI service for a video in the shared data folder."""

    def __init__(
        self,
        base_url: str,
        token: str,
        data_dir: Path,
        timeout_seconds: float,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.data_dir = data_dir.resolve()
        self.timeout_seconds = timeout_seconds

    def analyze(self, path: Path, captured_at: datetime) -> dict[str, Any]:
        try:
            relative_path = path.resolve().relative_to(self.data_dir)
        except ValueError as exc:
            raise RuntimeError(
                f"Video is outside the shared data directory: {path}"
            ) from exc
        request = Request(
            f"{self.base_url}/analyze",
            data=json.dumps(
                {
                    "path": relative_path.as_posix(),
                    "captured_at": captured_at.isoformat(),
                }
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-AI-Token": self.token,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.load(response)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Native AI returned HTTP {exc.code}: {detail}"
            ) from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Native AI request failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Native AI returned an invalid response.")
        return payload
