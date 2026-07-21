from __future__ import annotations

import mimetypes
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse

from .config import settings
from .demo import seed_demo
from .service import MonitorService


service = MonitorService(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.demo_mode:
        seed_demo(service.db)
    else:
        service.db.clear_demo_events()
    await service.start()
    yield
    await service.stop()


app = FastAPI(title="Blink Camera AI Hub API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/status")
def status():
    return service.status()


@app.get("/api/logs", response_class=PlainTextResponse)
def logs(lines: int = Query(200, ge=1, le=2000)):
    path = settings.data_dir / "logs" / "blink-camera-ai-hub.log"
    if not path.exists():
        return "아직 로그가 없습니다.\n"
    return "".join(path.read_text(encoding="utf-8").splitlines(keepends=True)[-lines:])


@app.post("/api/telegram/reload")
def reload_telegram():
    return {"configured": service.reload_telegram_settings()}


@app.get("/api/events")
def events(
    limit: int = Query(100, ge=1, le=500),
    important_only: bool = False,
):
    return {"events": service.db.list_events(limit=limit, important_only=important_only)}


@app.post("/api/scan")
async def scan_now(hours: float | None = Query(None, gt=0, le=72)):
    since = datetime.now(timezone.utc) - timedelta(hours=hours) if hours else None
    return await service.scan(since_override=since)


@app.post("/api/demo")
def demo():
    seed_demo(service.db)
    return {"created": True}


@app.get("/media/{event_id}")
def media(event_id: int):
    match = next((item for item in service.db.list_events(limit=500) if item["id"] == event_id), None)
    if not match or not match.get("video_path"):
        raise HTTPException(404, "영상 파일이 없습니다.")
    path = Path(match["video_path"]).resolve()
    try:
        path.relative_to(settings.data_dir.resolve())
    except ValueError as exc:
        raise HTTPException(403, "허용되지 않은 경로입니다.") from exc
    if not path.exists():
        raise HTTPException(404, "영상 파일을 찾을 수 없습니다.")
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "video/mp4")
