# Blink Camera AI Hub Backend on Docker

This configuration runs only the Python backend. It checks Blink videos, performs AI analysis, removes videos after 90 days, and sends event videos through Telegram. It does not run the frontend.

## First run

Run these commands from the project directory:

```bash
mkdir -p data models
cp -n yolo11n.pt models/ 2>/dev/null || true
docker compose build
docker compose up -d
```

Existing `data/blink-auth.json`, `data/sentinel.db`, videos, and `.env` settings are preserved. The AI model is downloaded to `models/` during the first analysis and reused afterward. If a model file already exists in the project directory, the `cp` command above avoids downloading it again.

The downloader, fast YOLO pool, durable Moondream2 queue, and Telegram retry notifier run independently. A completed download is atomically added to `data/raw`, and its YOLO and Moondream2 requests start together while later downloads continue. Two videos are analyzed by YOLO concurrently by default, each with an independent model instance. Moondream2 proceeds through its own concurrency-limited GPU queue and either validated model detection makes the final result positive. A YOLO positive sends immediately with Moondream2 marked pending; a later Moondream2 positive can send a YOLO-negative clip. Each unique source filename is delivered to Telegram at most once, so a second positive model only updates the existing caption. Failed deliveries and pending Moondream2 work survive process restarts. Temporary `.part` files are never analyzed.

## Status and logs

```bash
docker compose ps
docker compose logs -f backend
curl http://127.0.0.1:8787/api/health
```

The `restart: unless-stopped` policy restarts the container after an unexpected process exit.

## Use the Apple GPU

Linux containers cannot access the Mac's Metal/MPS device. Install the native
Moondream2 service once and let Docker call it through Docker Desktop's host
gateway:

```bash
bash scripts/install_native_ai.sh
bash scripts/enable_native_ai_service.sh
docker compose up -d --build
```

Check both services:

```bash
curl http://127.0.0.1:8790/health
curl http://127.0.0.1:8787/api/status
tail -f data/logs/native-ai.log
```

The native service is managed by `launchd` with `RunAtLoad` and `KeepAlive`.
It loads Moondream2 on `mps`, accepts only authenticated paths under `data/`,
and processes one GPU request at a time by default to fit an 8 GB M1. The
backend still analyzes multiple videos concurrently and runs each video's YOLO
work in parallel with its Moondream2 request. The computer must remain awake;
neither Docker nor `launchd` can run while macOS is asleep.

## Stop and restart

```bash
docker compose stop
docker compose start
docker compose restart backend
```

Rebuild the image after changing source code or configuration:

```bash
docker compose up -d --build
```

## Connect Blink for the first time

```bash
docker compose run --rm backend python -m backend.setup_blink
docker compose up -d
```

## Automatic 90-day cleanup

The default `.env` setting is:

```env
VIDEO_RETENTION_DAYS=90
```

Once per day, the backend deletes expired MP4 files from `data/raw`, `data/rejected`, and `data/events`, then removes the related SQLite records. Set the value to `0` and run `docker compose restart backend` to disable automatic cleanup.

## Notes

- Docker Desktop must be running.
- Downloads and analysis stop while the Mac is asleep.
- `.env`, `data/`, and `models/` are not included in the Docker image and remain on the Mac.
- Run `docker compose restart backend` after changing Telegram settings.
