# Blink Camera AI Hub

Blink Camera AI Hub periodically downloads new motion clips from Blink Outdoor cameras and analyzes them locally with AI. It detects people and genuinely moving vehicles, joins related clips into event videos, and delivers the results through a local dashboard and Telegram.

> This is an unofficial open-source project and is not affiliated with Amazon, Blink, or Immedia Semiconductor. Keep camera footage, Blink credentials, and Telegram tokens local. Never commit them to Git.

## Features

- Checks a Blink Sync Module for new clips every five minutes by default
- Starts AI analysis immediately after each completed download using a parallel worker pool
- Runs fast YOLO workers independently from a durable Moondream2 GPU queue; either model may confirm a positive detection
- Detects people and moving land vehicles; animal detections are ignored
- Combines detections across frames with motion and sharpness checks to reduce insect and parked-vehicle false positives
- Merges only time-correlated clips from the same camera into an event
- Flags people at night, multiple people, and repeated target activity as anomalies
- Provides a local web dashboard and Telegram MP4 notifications, with at most one video delivery per unique source filename
- Stores metadata in SQLite and applies a 90-day default video-retention policy
- Includes Apple Silicon launchers and a backend-only Docker configuration

## Requirements

- macOS or Linux
- Python 3.10–3.13 (3.13 recommended)
- Node.js 22 or newer for the dashboard
- Optional: Docker Desktop for the backend-only container

## Quick start

```bash
git clone https://github.com/knockknockyoo/blink-camera-ai-hub.git
cd blink-camera-ai-hub
bash scripts/setup.sh
bash scripts/run.sh
```

The application starts with demo data until a Blink account is connected. Open `http://localhost:3000` for the dashboard. The backend API listens locally at `http://127.0.0.1:8787`.

Detailed download and AI-analysis logs are written to the terminal and `data/logs/blink-camera-ai-hub.log`.

```bash
tail -f data/logs/blink-camera-ai-hub.log
```

## Connect a Blink account

Run the following command on your own computer, then enter your Blink email address, password, and two-factor authentication code.

```bash
bash scripts/connect_blink.sh
bash scripts/run.sh
```

Reusable authentication data is stored locally in `data/blink-auth.json`. This file and the entire `data/` directory are excluded from Git.

## Telegram video notifications

Create a bot with Telegram's `@BotFather`. Send `/start` in a private chat with the bot, or in a group to which the bot has been invited, then run:

```bash
bash scripts/connect_telegram.sh
```

The token is hidden while you type and is stored only in the local `.env` file. A completed download enters the parallel AI pool immediately. Each relevant result is sent to Telegram as soon as its analysis finishes. The downloaded source filename is the durable delivery identity: YOLO and Moondream2 can both detect the clip, but Telegram receives the video at most once. The slower model updates the existing message caption with its final vote. Failed notifications remain in a durable queue and are retried automatically. No public URL or router port forwarding is required.

`TELEGRAM_PROTECT_CONTENT=true` limits forwarding and saving of Telegram messages by default. Existing events are skipped during the initial connection, and failed new notifications are retried on a later scan.

## Apple Silicon Mac launchers

When using a release ZIP, extract it and double-click these files in order:

1. `M1-1-Install.command`
2. `M1-2-Connect-Blink.command`
3. Optional: `M1-3-Connect-Telegram.command`
4. `M1-4-Start.command`

Use `M1-5-View-Logs.command` to follow the detailed log. The first installation downloads Python and Node packages from the internet. A release archive must not include credentials or existing camera footage.

## Run only the backend with Docker

Use Docker when you want Blink scanning, AI analysis, and Telegram notifications without the dashboard:

```bash
mkdir -p data models
cp -n yolo11n.pt models/ 2>/dev/null || true
docker compose up -d --build
docker compose logs -f backend
```

If no model is present, Ultralytics downloads it during the first analysis. The container restarts after an unexpected exit, while `data/` and `models/` remain on the host. Videos and related database records older than 90 days are cleaned once per day by default. See [DOCKER-M1.md](DOCKER-M1.md) for details.

### Apple GPU analysis with Docker

Docker Desktop cannot expose Apple Metal/MPS to Linux containers. The supported
GPU layout therefore keeps Blink downloads, SQLite, and Telegram in Docker while
running Moondream2 as a small native macOS service:

```bash
bash scripts/install_native_ai.sh
bash scripts/enable_native_ai_service.sh
docker compose up -d --build
curl http://127.0.0.1:8790/health
curl http://127.0.0.1:8787/api/status
```

The native installer also installs libvips and downloads the Moondream2 weights
from Hugging Face during the first launch. The
Docker container submits only a relative path for a video already present in the
shared `data/` directory; it does not upload the video over the internet.
Requests are authenticated with a generated local token. Two fast YOLO workers
analyze downloaded videos without waiting for the slower native queue.
Moondream2 follows on the Mac, while the native service limits GPU concurrency
to avoid exhausting unified memory.

## Test with existing videos

You can analyze an MP4 before connecting a Blink account. Place it under a camera-specific raw directory:

```text
data/raw/Outdoor/example.mp4
```

Then run `bash scripts/run.sh`. The default YOLO11n model is downloaded automatically during the first AI analysis.

## How it works

1. An independent downloader checks Blink for new clips every five minutes by default.
2. Each clip is written as a temporary file and atomically published to the durable raw-video queue when complete.
3. The completed clip is immediately submitted to a pool of independent AI workers while later downloads continue.
4. For each video, YOLO and Moondream2 start together. Fast YOLO workers save their result without waiting while Moondream2 continues through its separate durable, concurrency-limited GPU queue.
5. A YOLO positive sends the video immediately with Moondream2 marked pending. A later Moondream2 positive can send a YOLO-negative video. If the video was already sent, only its existing Telegram caption is updated; the unique source filename prevents a second video delivery.
6. Object detections are correlated across sampled frames and checked for box motion and sharpness.
7. Related activity from the same camera is merged within a two-minute window for the dashboard.
8. Unimportant source clips are preserved under `data/rejected/` rather than deleted.
9. Event metadata is stored in SQLite and exposed to the dashboard.

## Configuration

The setup script copies `.env.example` to `.env`. The most important settings are:

| Variable | Default | Purpose |
| --- | ---: | --- |
| `SCAN_INTERVAL_SECONDS` | `300` | Interval between checks for new Blink clips |
| `ANALYSIS_INTERVAL_SECONDS` | `300` | Recovery interval for videos left unqueued after a crash or analysis failure |
| `AI_WORKER_COUNT` | `2` | Number of videos analyzed concurrently with independent model instances |
| `BLINK_CLIP_TIMEOUT_SECONDS` | `90` | Maximum time one Sync Module clip may block a scan |
| `BLINK_METADATA_TIMEOUT_SECONDS` | `120` | Maximum time for each Blink login, refresh, or manifest operation |
| `BLINK_DOWNLOAD_RETRIES` | `1` | Attempts per clip in one scan; failures retry on the next scan |
| `BLINK_MAX_CLIPS_PER_SCAN` | `20` | Maximum clips processed per scan, newest first; `0` disables the limit |
| `BLINK_DOWNLOAD_DELAY_SECONDS` | `5` | Pause between clip downloads to reduce Blink throttling |
| `BLINK_BACKLOG_RETRY_SECONDS` | `15` | Delay before fetching the next batch when older clips remain |
| `BLINK_SCAN_OVERLAP_SECONDS` | `900` | Previous time window rechecked on each scan for late Sync Module clips |
| `MERGE_WINDOW_SECONDS` | `120` | Time window for joining related clips |
| `VIDEO_RETENTION_DAYS` | `90` | Retention period; `0` disables automatic deletion |
| `MODEL_NAME` | `yolo11n.pt` | Ultralytics model name or local path |
| `DETECTION_CONFIDENCE` | `0.15` | Minimum object-detection confidence |
| `SAMPLE_FPS` | `5` | Number of video frames analyzed per second |
| `CAMERA_TIMEZONE` | `Asia/Seoul` | Time zone used for camera capture times |
| `KEEP_UNKNOWN_MOTION` | `false` | Whether to keep unclassified motion events |
| `NATIVE_AI_URL` | empty | Native macOS AI endpoint used by Docker; the native installer sets it automatically |
| `NATIVE_AI_BACKEND` | `moondream2` | Native detector (`moondream2` or `yolo`) |
| `MOONDREAM_MAX_FRAMES` | `4` | Representative frames analyzed per video; four preserves multi-frame validation on an M1 |
| `NATIVE_AI_CONCURRENCY` | `1` | Maximum concurrent native GPU requests; use `1` on an 8 GB M1 |
| `AI_DEVICE` | `mps` | PyTorch device used by the native service |

`PERSON_MIN_AREA` and `PERSON_MIN_BOX_MOTION` reject small, static person false positives. `VEHICLE_MIN_BOX_MOTION` and `VEHICLE_MIN_SHARPNESS` reduce false alerts from parked vehicles and out-of-focus insects. If distant real subjects are missed, lower these values gradually and test again.

`bash scripts/reanalyze.sh --hours 12 --yes` preserves Telegram delivery history by default, so a previously delivered source filename is not sent again. Add `--resend-telegram` only when an intentional duplicate delivery is required.

## Development and validation

```bash
.venv/bin/python -m unittest tests.test_core
npm test
docker compose config -q
```

Read [CONTRIBUTING.md](CONTRIBUTING.md) before contributing. Do not attach real camera footage, credentials, or identifying logs to an issue or pull request.

## Limitations and security

- Blink does not provide a public official API. The unofficial BlinkPy integration can break when the Blink service changes.
- Excessively short polling intervals can trigger Blink or Telegram rate limits.
- The application can process only recorded motion clips; it cannot reconstruct periods that were never recorded.
- General-purpose YOLO models are not perfect for every scene. Fine-tuning with representative false-positive samples provides the best accuracy improvement.
- Moondream2 analyzes representative still frames rather than video natively. Increasing `MOONDREAM_MAX_FRAMES` can improve recall but also increases latency.
- Treat `data/blink-auth.json` like a password and never expose API port 8787 to the internet.
- Follow [SECURITY.md](SECURITY.md) when reporting a vulnerability or sharing logs, and redact all account, camera, network, and Sync Module identifiers.

## License

Blink Camera AI Hub is released under the [GNU AGPL-3.0](LICENSE). Ultralytics code and YOLO model weights are offered under AGPL-3.0 or a separate Enterprise License. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for additional notices.
