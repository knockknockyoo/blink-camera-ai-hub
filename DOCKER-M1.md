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

The downloader, AI analyzer, and Telegram retry notifier run as independent workers. A completed download is added to the durable `data/raw` queue immediately. Every five minutes by default, the AI worker processes the completed-video queue while downloads continue separately. Each relevant result is sent to Telegram immediately after its analysis finishes; failed deliveries remain queued across container restarts. Temporary `.part` files are never analyzed.

## Status and logs

```bash
docker compose ps
docker compose logs -f backend
curl http://127.0.0.1:8787/api/health
```

The `restart: unless-stopped` policy restarts the container after an unexpected process exit.

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
