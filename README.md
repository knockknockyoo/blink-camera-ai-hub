# Blink Camera AI Hub

Blink Outdoor 카메라의 새 모션 클립을 5분마다 내려받아 사람·동물 중심으로 정리하는 로컬 프로그램입니다. 가까운 시간에 이어진 클립은 하나의 사건 영상으로 합치고, 야간 사람·여러 사람·반복 활동·큰 미분류 움직임을 이상징후로 표시합니다.

> 이 프로젝트는 Amazon, Blink 또는 Immedia Semiconductor와 관계없는 비공식
> 오픈소스 프로젝트입니다. 영상, Blink 인증정보와 Telegram 토큰은 로컬에만
> 보관하며 Git 저장소에 올리지 마세요.

## 다른 M1 Mac에 설치

`Blink-Camera-AI-Hub-M1.zip`을 M1 Mac으로 복사해 압축을 푼 뒤 다음 파일을 순서대로 더블클릭합니다.

1. `M1-1-Install.command`
2. `M1-2-Connect-Blink.command`
3. 선택 사항: `M1-3-Connect-Telegram.command`
4. `M1-4-Start.command`

상세 로그는 `M1-5-View-Logs.command`로 볼 수 있습니다. Python 3.10~3.13(3.13 권장)이 필요하며, 첫 설치 때 Python 패키지와 Node 패키지를 인터넷에서 내려받습니다. ZIP에는 Blink·Telegram 인증정보와 기존 카메라 영상이 포함되지 않습니다.

## 처음 실행

macOS 또는 Linux 터미널에서 프로젝트 폴더로 이동한 뒤 실행합니다.

```bash
bash scripts/setup.sh
bash scripts/run.sh
```

`bash scripts/run.sh` 한 번으로 백엔드와 화면이 모두 실행됩니다. 스크립트는 백엔드 상태 확인에 성공한 뒤 화면을 시작합니다. 브라우저에서 `http://localhost:3000`을 열면 먼저 데모 화면이 표시됩니다. 실행 중인 터미널을 닫거나 `Ctrl+C`를 누르면 둘 다 종료됩니다.

상세 다운로드·AI 분석 로그는 실행 터미널과 `data/logs/blink-camera-ai-hub.log`에 함께 기록됩니다. 다른 터미널에서 `tail -f data/logs/blink-camera-ai-hub.log`를 실행하거나 브라우저에서 `http://127.0.0.1:8787/api/logs`를 열어 확인할 수 있습니다.

## Docker로 백엔드만 실행

화면 없이 Blink 확인, AI 분석, Telegram 발송만 실행하려면 Docker를 사용할 수 있습니다.

```bash
mkdir -p data models
cp -n yolo11n.pt models/ 2>/dev/null || true
docker compose up -d --build
docker compose logs -f backend
```

컨테이너는 비정상 종료 시 자동 재시작되며 `data/`와 `models/`는 M1에 계속 보존됩니다.
90일이 지난 영상과 관련 DB 기록은 하루에 한 번 자동으로 정리됩니다. 자세한 내용은
`DOCKER-M1.md`를 참고하세요.

## Telegram 영상 알림

Telegram의 `@BotFather`에서 Bot을 만든 뒤 Bot에게 `/start`를 보내고 아래 명령을 실행합니다.

```bash
bash scripts/connect_telegram.sh
```

토큰은 입력할 때 화면이나 셸 기록에 표시되지 않고 로컬 `.env`에만 저장됩니다. 실행 중인 백엔드가 있으면 설정이 즉시 적용되며, 꺼져 있으면 `bash scripts/run.sh`로 시작하세요. 이후 새 사람·동물·이상징후 이벤트의 MP4가 Telegram으로 직접 업로드됩니다. 공개 URL이나 공유기 포트 개방은 필요하지 않습니다. 기존 이벤트는 발송하지 않고, 실패한 새 이벤트는 다음 스캔에서 재시도합니다. 기본값인 `TELEGRAM_PROTECT_CONTENT=true`는 메시지 전달과 저장을 제한합니다.

## Blink 계정 연결

다음 명령을 실행한 뒤 본인 컴퓨터에서 Blink 이메일, 비밀번호, 2단계 인증번호를 입력합니다.

```bash
bash scripts/connect_blink.sh
bash scripts/run.sh
```

인증정보는 `data/blink-auth.json`에 로컬로 저장되며 웹 브라우저로 전달되지 않습니다. 이 파일과 `data/` 폴더는 Git에서 제외됩니다.

## 기존 영상으로 먼저 시험하기

Blink 연결 전에 MP4 파일을 `data/raw/Outdoor/`에 복사한 뒤 `bash scripts/run.sh`를 실행해도 됩니다. 첫 AI 분석 시 오픈소스 YOLO11n 모델이 자동으로 내려받아집니다.

## 작동 방식

1. BlinkPy로 5분마다 새 클립 확인
2. 영상에서 초당 2개 프레임을 샘플링
3. 여러 프레임에 지속해서 나타나고 실제 움직임이 확인되는 사람·동물·차량만 인정
4. 2분 안에 연결된 활동을 하나의 사건으로 병합
5. 중요하지 않은 원본은 삭제하지 않고 `data/rejected/`로 이동
6. SQLite에 결과 저장 후 한국어 타임라인에 표시

설정은 `.env`에서 바꿀 수 있습니다. 기본 검사 간격은 `SCAN_INTERVAL_SECONDS=300`, 사건 병합 간격은 `MERGE_WINDOW_SECONDS=120`입니다. 기본값은 사람·동물·이상징후만 타임라인에 남기며, 일반 움직임도 남기려면 `KEEP_UNKNOWN_MOTION=true`로 바꾸세요.

작은 고정 물체의 사람 오인은 `PERSON_MIN_AREA`와 `PERSON_MIN_BOX_MOTION`으로 걸러냅니다. 멀리 있는 실제 사람을 놓치면 두 값을 조금 낮출 수 있습니다.

## 주의사항

- Blink는 공식 공개 API를 제공하지 않으므로 BlinkPy 연동은 Blink 변경에 따라 수정이 필요할 수 있습니다.
- BlinkPy는 빠른 API 호출을 권장하지 않습니다. 기본 5분 간격을 과도하게 줄이지 마세요.
- 실제로 촬영되지 않은 시간은 복원할 수 없습니다. 프로그램은 저장된 모션 클립만 연결합니다.
- YOLO11n 기본 모델은 사람·개·고양이·새 등 일반 객체를 구분합니다. 곤충 오탐이 남으면 실제 오탐 샘플로 추가 학습하는 것이 가장 정확합니다.
- Ultralytics 코드와 YOLO 모델은 AGPL-3.0 또는 별도의 Enterprise License로 제공됩니다. 이 프로젝트를 공개·배포하거나 상업적으로 사용하기 전에 해당 조건을 검토하세요.
- 취약점이나 로그를 공개할 때는 `SECURITY.md`를 따르고 계정·카메라·네트워크 식별자를 모두 가리세요.

서드파티 라이선스 안내는 `THIRD_PARTY_NOTICES.md`, 기여 방법은
`CONTRIBUTING.md`를 참고하세요.
