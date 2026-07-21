# Blink Camera AI Hub

[English](README.md) | **한국어**

Blink Outdoor 카메라의 새 모션 클립을 주기적으로 내려받아 로컬 AI로 분석하는 프로그램입니다. 사람·동물·실제로 움직이는 차량을 감지하고, 가까운 시간에 이어진 관련 클립을 하나의 사건 영상으로 합친 뒤 대시보드와 Telegram으로 알려줍니다.

> 이 프로젝트는 Amazon, Blink 또는 Immedia Semiconductor와 관계없는 비공식 오픈소스 프로젝트입니다. 카메라 영상, Blink 인증정보와 Telegram 토큰은 로컬에만 보관하고 Git 저장소에 올리지 마세요.

## 주요 기능

- 기본 5분 간격으로 Blink Sync Module의 새 클립 확인
- YOLO 기반 사람·동물·차량 감지
- 여러 프레임의 검출과 실제 움직임을 함께 확인해 벌레·주차 차량 오탐 감소
- 시간과 카메라가 연관된 클립만 하나의 사건으로 병합
- 야간 사람, 여러 사람, 반복 활동과 큰 미분류 움직임을 이상징후로 표시
- 로컬 웹 대시보드와 Telegram MP4 알림
- SQLite 기록과 90일 기본 영상 보관 정책
- Apple Silicon Mac용 실행 파일과 백엔드 전용 Docker 구성

## 요구 사항

- macOS 또는 Linux
- Python 3.10~3.13 (3.13 권장)
- Node.js 22 이상 — 대시보드를 사용할 때 필요
- Docker Desktop — 백엔드 전용 컨테이너 실행 시 선택 사항

## 빠른 시작

```bash
git clone https://github.com/knockknockyoo/blink-camera-ai-hub.git
cd blink-camera-ai-hub
bash scripts/setup.sh
bash scripts/run.sh
```

Blink를 아직 연결하지 않았다면 데모 데이터로 시작합니다. 브라우저에서 `http://localhost:3000`을 여세요. 백엔드 API는 로컬 주소 `http://127.0.0.1:8787`에서 실행됩니다.

상세 로그는 실행 중인 터미널과 `data/logs/blink-camera-ai-hub.log`에 기록됩니다.

```bash
tail -f data/logs/blink-camera-ai-hub.log
```

## Blink 계정 연결

본인 컴퓨터에서 다음 명령을 실행하고 Blink 이메일, 비밀번호와 2단계 인증번호를 입력합니다.

```bash
bash scripts/connect_blink.sh
bash scripts/run.sh
```

재사용 가능한 인증정보는 `data/blink-auth.json`에 로컬로 저장됩니다. 이 파일과 전체 `data/` 폴더는 Git에서 제외됩니다.

## Telegram 영상 알림

Telegram의 `@BotFather`에서 Bot을 만들고, Bot과의 개인 대화 또는 초대한 그룹에서 `/start`를 보낸 뒤 실행합니다.

```bash
bash scripts/connect_telegram.sh
```

토큰은 화면이나 셸 기록에 표시되지 않고 로컬 `.env`에만 저장됩니다. 이후 새 사람·동물·이상징후 이벤트의 MP4가 Telegram으로 직접 업로드됩니다. 공개 URL이나 공유기 포트 개방은 필요하지 않습니다.

기본값인 `TELEGRAM_PROTECT_CONTENT=true`는 Telegram 메시지의 전달과 저장을 제한합니다. 기존 이벤트는 처음 연결할 때 발송하지 않으며, 실패한 새 이벤트는 다음 스캔에서 다시 시도합니다.

## Apple Silicon Mac에서 실행

배포 ZIP을 사용한다면 압축을 푼 뒤 다음 파일을 순서대로 더블클릭합니다.

1. `M1-1-Install.command`
2. `M1-2-Connect-Blink.command`
3. 선택 사항: `M1-3-Connect-Telegram.command`
4. `M1-4-Start.command`

`M1-5-View-Logs.command`로 상세 로그를 볼 수 있습니다. 처음 설치할 때 Python 및 Node 패키지를 인터넷에서 내려받습니다. 배포 ZIP에는 인증정보와 카메라 영상이 포함되지 않아야 합니다.

## Docker로 백엔드만 실행

대시보드 없이 Blink 확인, AI 분석과 Telegram 발송만 실행할 수 있습니다.

```bash
mkdir -p data models
cp -n yolo11n.pt models/ 2>/dev/null || true
docker compose up -d --build
docker compose logs -f backend
```

YOLO 모델이 없다면 첫 분석 시 자동으로 내려받습니다. 컨테이너는 비정상 종료 후 자동 재시작되며 `data/`와 `models/`는 호스트 컴퓨터에 보존됩니다. 90일이 지난 영상과 관련 DB 기록은 기본적으로 하루에 한 번 정리됩니다. 자세한 내용은 [DOCKER-M1.md](DOCKER-M1.md)를 참고하세요.

## 기존 영상으로 시험하기

Blink 연결 전에 MP4를 카메라별 원본 폴더에 넣어 분석할 수도 있습니다.

```text
data/raw/Outdoor/example.mp4
```

그런 다음 `bash scripts/run.sh`를 실행합니다. 첫 AI 분석 시 기본 YOLO11n 모델이 자동으로 내려받아집니다.

## 작동 방식

1. BlinkPy로 기본 5분마다 새 클립 확인
2. 기본 초당 5개 프레임 샘플링
3. 여러 프레임의 객체 검출과 박스 움직임·선명도 확인
4. 관련된 카메라 활동을 기본 2분 범위에서 사건으로 병합
5. 중요하지 않은 원본을 삭제하지 않고 `data/rejected/`로 이동
6. SQLite에 결과 저장 후 대시보드와 Telegram에 전달

## 설정

처음 설치하면 `.env.example`이 `.env`로 복사됩니다. 주요 값은 다음과 같습니다.

| 변수 | 기본값 | 설명 |
| --- | ---: | --- |
| `SCAN_INTERVAL_SECONDS` | `300` | 새 Blink 클립 확인 간격 |
| `MERGE_WINDOW_SECONDS` | `120` | 관련 클립을 하나의 사건으로 묶는 시간 범위 |
| `VIDEO_RETENTION_DAYS` | `90` | 영상과 관련 기록 보관 기간, `0`은 자동 삭제 안 함 |
| `MODEL_NAME` | `yolo11n.pt` | Ultralytics 모델 이름 또는 로컬 경로 |
| `DETECTION_CONFIDENCE` | `0.15` | 객체 검출 최소 신뢰도 |
| `SAMPLE_FPS` | `5` | 분석할 초당 프레임 수 |
| `CAMERA_TIMEZONE` | `Asia/Seoul` | 카메라 촬영 시각의 시간대 |
| `KEEP_UNKNOWN_MOTION` | `false` | 미분류 움직임 사건 유지 여부 |

작은 고정 물체의 사람 오인은 `PERSON_MIN_AREA`와 `PERSON_MIN_BOX_MOTION`으로 걸러냅니다. 이미 주차된 차량과 초점 밖 벌레 오탐은 `VEHICLE_MIN_BOX_MOTION`과 `VEHICLE_MIN_SHARPNESS`로 줄입니다. 멀리 있는 실제 대상을 놓친다면 값을 조금씩 낮춰 시험하세요.

## 개발 및 검사

```bash
.venv/bin/python -m unittest tests.test_core
npm test
docker compose config -q
```

기여하기 전에 [CONTRIBUTING.md](CONTRIBUTING.md)를 읽어주세요. 실제 카메라 영상, 인증정보 또는 식별 가능한 로그를 이슈나 Pull Request에 첨부하지 마세요.

## 제한 사항과 보안

- Blink는 공식 공개 API를 제공하지 않습니다. 비공식 BlinkPy 연동은 Blink 서비스 변경에 따라 중단될 수 있습니다.
- API 호출 간격을 과도하게 줄이면 Blink 또는 Telegram의 속도 제한이 발생할 수 있습니다.
- 이 프로그램은 이미 저장된 모션 클립만 처리하며 촬영되지 않은 시간은 복원할 수 없습니다.
- 일반 목적 YOLO 모델은 모든 장면에서 완벽하지 않습니다. 실제 오탐 샘플을 이용한 추가 학습이 가장 정확한 개선 방법입니다.
- `data/blink-auth.json`은 비밀번호처럼 취급하고 API 포트 8787을 인터넷에 공개하지 마세요.
- 취약점이나 로그를 공개할 때는 [SECURITY.md](SECURITY.md)를 따르고 모든 계정·카메라·네트워크 식별자를 가리세요.

## 라이선스

Blink Camera AI Hub는 [GNU AGPL-3.0](LICENSE)으로 배포됩니다. Ultralytics 코드와 YOLO 모델은 AGPL-3.0 또는 별도의 Enterprise License 조건을 따릅니다. 서드파티 안내는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)를 참고하세요.
