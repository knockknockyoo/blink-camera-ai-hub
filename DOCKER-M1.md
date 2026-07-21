# Blink Camera AI Hub 백엔드 Docker 실행

이 구성은 프론트엔드를 실행하지 않습니다. Blink 영상 확인, AI 분석, 90일 자동 삭제,
Telegram 영상 발송을 담당하는 Python 백엔드만 실행합니다.

## 최초 실행

프로젝트 폴더에서 다음을 실행합니다.

```bash
mkdir -p data models
cp -n yolo11n.pt models/ 2>/dev/null || true
docker compose build
docker compose up -d
```

기존 `data/blink-auth.json`, `data/sentinel.db`, 영상과 `.env` 설정은 그대로 사용됩니다.
AI 모델은 최초 분석 때 `models/`에 자동으로 내려받아지고 이후 재사용됩니다.
기존 모델 파일이 프로젝트 폴더에 있으면 위 `cp` 명령으로 재다운로드를 피할 수 있습니다.

## 상태와 로그

```bash
docker compose ps
docker compose logs -f backend
curl http://127.0.0.1:8787/api/health
```

컨테이너 프로세스가 비정상 종료되면 `restart: unless-stopped` 정책으로 자동 재시작됩니다.

## 중지와 재시작

```bash
docker compose stop
docker compose start
docker compose restart backend
```

설정 또는 소스 변경 후 이미지를 다시 만들려면:

```bash
docker compose up -d --build
```

## Blink를 처음 연결할 때

```bash
docker compose run --rm backend python -m backend.setup_blink
docker compose up -d
```

## 90일 자동 삭제

`.env`의 기본 설정은 다음과 같습니다.

```env
VIDEO_RETENTION_DAYS=90
```

백엔드는 하루에 한 번 `data/raw`, `data/rejected`, `data/events`에서 보관 기간을 넘긴
MP4를 삭제하고 관련 SQLite 기록도 정리합니다. 자동 삭제를 끄려면 값을 `0`으로 바꾼 뒤
`docker compose restart backend`를 실행합니다.

## 주의사항

- Docker Desktop이 실행 중이어야 합니다.
- Mac이 잠든 동안에는 다운로드와 분석이 실행되지 않습니다.
- `.env`, `data/`, `models/`는 Docker 이미지에 포함되지 않고 M1에만 보관됩니다.
- Telegram 설정을 바꾼 뒤에는 `docker compose restart backend`를 실행합니다.
