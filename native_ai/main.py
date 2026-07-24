from __future__ import annotations

import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from backend.analyzer import VideoAnalyzer
from backend.config import ROOT, Settings
from backend.rfdetr_analyzer import RFDETRVideoAnalyzer


load_dotenv(ROOT / ".env")
LOGGER = logging.getLogger("blink-camera-ai-hub.native-ai")
SETTINGS = Settings()
TOKEN = os.getenv("NATIVE_AI_TOKEN", "")
BACKEND = os.getenv("NATIVE_AI_BACKEND", "rfdetr").strip().lower()
DEVICE = os.getenv("AI_DEVICE", "mps").strip().lower()
MAX_FRAMES = max(1, int(os.getenv("RFDETR_MAX_FRAMES", "16")))
CONCURRENCY = max(1, int(os.getenv("NATIVE_AI_CONCURRENCY", "1")))
MODEL_NAME = (
    os.getenv("RFDETR_MODEL_SIZE", "small")
    if BACKEND == "rfdetr"
    else SETTINGS.model_name
)


def build_analyzer() -> VideoAnalyzer:
    analyzer_type = RFDETRVideoAnalyzer if BACKEND == "rfdetr" else VideoAnalyzer
    return analyzer_type(
        MODEL_NAME,
        SETTINGS.confidence,
        SETTINGS.sample_fps,
        SETTINGS.camera_timezone,
        SETTINGS.person_min_area,
        SETTINGS.person_min_box_motion,
        SETTINGS.vehicle_min_box_motion,
        SETTINGS.vehicle_min_sharpness,
        device=DEVICE,
        max_frames=MAX_FRAMES if BACKEND == "rfdetr" else 0,
    )


ANALYZER = build_analyzer()
INFERENCE_LIMIT = asyncio.Semaphore(CONCURRENCY)


class AnalyzeRequest(BaseModel):
    path: str
    captured_at: datetime


def resolve_video_path(relative_path: str) -> Path:
    root = SETTINGS.data_dir.resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HTTPException(403, "Video path is outside DATA_DIR.") from exc
    if candidate.suffix.lower() != ".mp4":
        raise HTTPException(400, "Only MP4 video files are supported.")
    if not candidate.is_file():
        raise HTTPException(404, "Video file was not found.")
    return candidate


def authorize(supplied: str | None) -> None:
    if not TOKEN:
        raise HTTPException(503, "NATIVE_AI_TOKEN is not configured.")
    if supplied is None or not hmac.compare_digest(supplied, TOKEN):
        raise HTTPException(401, "Invalid native AI token.")


@asynccontextmanager
async def lifespan(_: FastAPI):
    if DEVICE == "mps":
        import torch

        if not torch.backends.mps.is_available():
            raise RuntimeError(
                "Apple MPS is not available to this Python process. "
                "The native AI service must run directly on Apple Silicon macOS."
            )
    LOGGER.info(
        "Loading native AI model: backend=%s model=%s device=%s concurrency=%d",
        BACKEND,
        MODEL_NAME,
        DEVICE,
        CONCURRENCY,
    )
    await asyncio.to_thread(ANALYZER._load_model)
    LOGGER.info("Native AI model is ready")
    yield


app = FastAPI(
    title="Blink Camera AI Hub Native AI",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, Any]:
    import torch

    return {
        "status": "ok",
        "backend": BACKEND,
        "model": MODEL_NAME,
        "device": DEVICE,
        "mps_available": torch.backends.mps.is_available(),
        "concurrency": CONCURRENCY,
    }


@app.post("/analyze")
async def analyze(
    payload: AnalyzeRequest,
    x_ai_token: str | None = Header(None),
) -> dict[str, Any]:
    authorize(x_ai_token)
    path = resolve_video_path(payload.path)
    async with INFERENCE_LIMIT:
        LOGGER.info("Analyzing on %s: %s", DEVICE, payload.path)
        return await asyncio.to_thread(ANALYZER.analyze, path, payload.captured_at)
