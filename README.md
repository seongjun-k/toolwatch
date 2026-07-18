# 🔧 toolwatch — 실습실 공구 반납 감시대

카메라(YOLO 객체 검출) + RFID로 공구 거치대의 반출·반납을 자동 감시하는 저비용 임베디드 시스템.

> 임베디드시스템 4주 프로젝트 · 2026

---

## 목차

- [개요](#개요)
- [시스템 아키텍처](#시스템-아키텍처)
- [기능](#기능)
- [하드웨어 구성](#하드웨어-구성)
- [소프트웨어 스택](#소프트웨어-스택)
- [디렉터리 구조](#디렉터리-구조)
- [설치 및 실행](#설치-및-실행)
- [설정](#설정)
- [시연 시나리오](#시연-시나리오)
- [라이선스](#라이선스)

---

## 개요

실습실 공용 공구가 반납되지 않거나 분실되는 문제를 해결하기 위한 시스템이다.

| 질문 | 답 |
|---|---|
| **무엇이 몇 개?** | YOLO11n 객체 검출로 거치대 재고 자동 파악 |
| **누가?** | RFID(RC522) 태그로 대여자 식별 |
| **언제?** | 반출/반납 시각 자동 기록 (SQLite) |
| **이상 상황?** | 미반납 초과 → 부저+적색 LED / 미확인 반출 → 즉시 경고+스냅샷 저장 |

---

## 시스템 아키텍처

```
┌─ 라즈베리파이 (엣지) ─────────┐         ┌─ 노트북 (서버) ──────────────┐
│ Pi 카메라  ──캡처──┐          │  HTTP   │ /frame 수신                  │
│ RC522 RFID (SPI) ──┼─ client ──POST──→ │  → YOLO11n 추론              │
│ 3색 경광등 (릴레이) ←┘         │ ←JSON─  │  → 재고 대조 · 디바운스       │
└──────────────────────────────┘         │  → RFID 세션 귀속            │
                                          │  → SQLite 로그              │
                                          │ Flask 대시보드 (/)           │
                                          └──────────────────────────────┘
```

- **Pi → 서버**: 3초 주기로 JPEG 프레임 + RFID UID를 HTTP POST
- **서버 → Pi**: 응답 JSON에 `light`(green/yellow/red) + `buzzer`(off/overdue/unauth) 명령
- Pi는 캡처·전송·구동만 수행, 모든 판정 로직은 서버에서 처리

---

## 기능

### 핵심 기능 (S1~S5)

| # | 기능 | 설명 |
|---|---|---|
| S1 | YOLO 재고 감시 | 공구 4종(니퍼, 드라이버, 펜치, 육각렌치) 개수 기반 판정, ROI 크롭 |
| S2 | 디바운스 판정 | 3프레임(~9초) 연속 동일 관찰 시에만 OUT/IN 확정 — 손 가림·단발 오검출 방지 |
| S3 | RFID 대여자 귀속 | 카드 태그 → 30초 세션 → 세션 중 반출 시 해당 UID에 귀속 |
| S4 | 경광등·부저 경고 | 녹색(정상) / 황색(정상 대여 중) / 적색+부저(미반납 초과 or 미확인 반출) |
| S5 | 웹 대시보드 | 공구 상태 조회, 이력, 스냅샷, 재고·임계시간 설정, UID 관리, 경고 해제 |

### 확장 기능 (E1~E3)

| # | 기능 | 설명 |
|---|---|---|
| E1 | SQLite DB | `users`/`loans`/`events` 등 5테이블, 재시작 시 대여 상태 복원 |
| E2 | 학생용 모바일 웹 | `/me` — 학번+이름 로그인, 본인 대여 현황 조회 + 건별 반납 기한 설정 |
| E3 | 웹 푸시 알림 | 대여/반납 즉시 알림 + 아침/기한/연체 리마인더 (pywebpush, 서비스 워커) |
| PWA | 설치형 앱 | `/me`를 홈 화면에 추가하면 독립 앱으로 동작 (manifest + 아이콘) |
| 외부 접속 | Tailscale Funnel | `https://<기기명>.<tailnet>.ts.net` 공인 HTTPS 주소로 외부망 접속 |

---

## 하드웨어 구성

| 품목 | 용도 | 비고 |
|---|---|---|
| Raspberry Pi 4+ | 엣지 제어 | |
| Pi 카메라 모듈 | 거치대 촬영 | 정면 40~50cm 고정 |
| RFID RC522 + 태그 | 대여자 식별 | SPI 연결 |
| 3색 경광등 (적/황/녹 + 부저) | 상태 표시 | 12V, 릴레이 경유 |
| 4채널 릴레이 모듈 | 12V 스위칭 | GPIO 3.3V → 릴레이 → 경광등 |
| 12V 어댑터 + 스텝다운(→5V) | 전원 | 단일 전원에서 분기 |
| 네트망 + 폼보드 | 공구 거치대 | 300×300mm, 단색 배경 |

### GPIO 핀 배치 (기본값)

| 채널 | GPIO |
|---|---|
| 적색 LED | 5 |
| 황색 LED | 6 |
| 녹색 LED | 13 |
| 부저 | 19 |

---

## 소프트웨어 스택

| 구분 | 기술 |
|---|---|
| 객체 검출 | YOLO11n (ultralytics) |
| 서버 | Python, Flask + waitress, SQLite, pywebpush |
| Pi 클라이언트 | Python, picamera2, requests, gpiozero, mfrc522 |
| 대시보드 | Jinja2 템플릿, 반응형 CSS, PWA(학생 페이지) |
| 외부 노출 | Tailscale Funnel (TLS 종단, 서버 자체는 HTTP) |

---

## 디렉터리 구조

```
toolwatch/
├── src/
│   ├── pi/                    # 라즈베리파이 엣지 클라이언트
│   │   ├── client.py          # 메인 루프 (캡처 → POST → 경광등 구동)
│   │   ├── hw.py              # 릴레이 4채널 제어 래퍼
│   │   └── config.example.json # Pi 설정 템플릿 (복사해 config.json으로, 시크릿 포함이라 실물은 gitignore)
│   └── server/                # 추론 + 판정 + 대시보드 서버
│       ├── app.py             # Flask 앱 (추론, 디바운스, RFID 세션, HMI, 리마인더 루프)
│       ├── db.py              # SQLite 접근 계층
│       ├── push.py            # 웹 푸시 발송 + 리마인더 선별 (E3)
│       ├── config.example.json # 서버 설정 템플릿 (복사해 config.json으로)
│       ├── static/            # PWA manifest·아이콘, 서비스 워커(sw.js), 공통 CSS
│       ├── templates/
│       │   ├── dashboard.html # 관리자 대시보드
│       │   └── student.html   # 학생용 대여 현황 페이지 (PWA)
│       └── snapshots/         # 반출 시 자동 저장되는 스냅샷 (gitignore)
├── model/                     # 학습된 YOLO 가중치 (best.pt)
├── dataset/                   # 학습 데이터 (gitignore)
├── docs/                      # 계획서, 구현계획, 레퍼런스 등
├── logs/                      # 실행 로그 (gitignore)
├── requirements.txt           # 서버·학습 호스트 의존성
├── run_server.bat             # 서버 기동 배치파일 (더블클릭 실행)
└── yolo11n.pt                 # 사전학습 모델 (개발용 폴백)
```

---

## 설치 및 실행

### 1. 서버 (노트북/데스크톱)

```bash
# 저장소 클론
git clone https://github.com/seongjun-k/toolwatch.git
cd toolwatch

# 가상환경 생성
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/Mac

# GPU 사용 시 PyTorch CUDA 빌드 먼저 설치
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124

# 의존성 설치
pip install -r requirements.txt

# 설정 파일 생성 (시크릿 채우기: hmi_password, secret_key, frame_token, VAPID 키)
copy src\server\config.example.json src\server\config.json

# 학습된 모델 배치
# model/best.pt 에 YOLO 학습 결과물을 복사 (없으면 yolo11n.pt로 폴백)

# 서버 실행 (또는 run_server.bat 더블클릭)
python src/server/app.py
```

서버가 waitress로 `http://0.0.0.0:5000` 에서 시작된다. 같은 네트워크의 브라우저에서 `http://<노트북IP>:5000` 으로 대시보드에 접속 가능.

**외부망 공개 (Tailscale Funnel):** 웹 푸시·PWA는 HTTPS가 필요하며, TLS는 Funnel이 종단한다.

```bash
tailscale funnel --bg http://localhost:5000
# 발급 주소: https://<기기명>.<tailnet>.ts.net (관리 콘솔에서 Funnel·HTTPS Certificates 활성화 필요)
```

**셀프체크 (판정 로직 단위 테스트):**

```bash
python src/server/app.py --selfcheck
```

### 2. 라즈베리파이

```bash
# Pi에서 필요한 패키지 설치
pip install requests mfrc522

# picamera2, gpiozero는 Raspberry Pi OS에 기본 포함

# config.example.json을 config.json으로 복사 후 server_url·frame_token 설정
# (frame_token은 서버 config.json과 동일한 값 — /frame 인증용)
# 예: "server_url": "http://192.168.0.2:5000/frame"

# 클라이언트 실행
cd src/pi
python client.py
```

---

## 설정

### Pi 설정 (`src/pi/config.json`)

| 키 | 기본값 | 설명 |
|---|---|---|
| `server_url` | `http://<서버IP>:5000/frame` | 서버 프레임 수신 엔드포인트 (내부망) |
| `frame_token` | — | `/frame` 인증 토큰 (서버와 동일 값) |
| `capture_interval_sec` | `3` | 기본 캡처 주기 (초) — 서버가 응답 `interval`로 동적 조정 |
| `jpeg_quality` | `80` | JPEG 압축 품질 |
| `relay_pins` | `{red:5, yellow:6, green:13, buzzer:19}` | GPIO 핀 번호 |

### 서버 설정 (`src/server/config.json`)

| 키 | 기본값 | 설명 |
|---|---|---|
| `model_path` | `model/best.pt` | YOLO 모델 경로 (프로젝트 루트 기준) |
| `fallback_model` | `yolo11n.pt` | 학습 모델 없을 때 폴백 |
| `confidence_threshold` | `0.5` | 검출 신뢰도 임계값 |
| `debounce_frames` | `3` | 디바운스 프레임 수 |
| `roi` | `null` | ROI 크롭 좌표 `{x1,y1,x2,y2}` (null이면 전체) |
| `registered_stock` | `{nipper:1, driver:1, plier:1, hex_key:1}` | 공구별 등록 재고 수 |
| `rfid_session_sec` | `30` | RFID 세션 유효 시간 (초) |
| `overdue_sec` | `7200` | 미반납 임계 시간 (초, 기본 2시간) — E2 반납 기한 설정 시 기한 우선 |
| `hmi_password` | — | 대시보드 로그인 비밀번호 (직접 설정) |
| `frame_token` | — | `/frame` 인증 토큰 (Pi와 동일 값) |
| `fast_interval_sec` | `0.5` | 변화 관측 중 고속 캡처 주기 (적응형 촬영) |
| `tool_labels` | 공구별 한국어명 | 알림·학생 페이지 표시용 라벨 |
| `vapid_public_key` / `vapid_private_key_file` / `vapid_email` | — | 웹 푸시 VAPID 키 (E3) |
| `morning_notify_time` | `09:00` | 반납일 아침 리마인더 시각 |
| `overdue_grace_sec` | `1800` | 기한 경과 후 연체 알림까지 유예 (초) |

---

## 시연 시나리오

### 1. 정상 대여·반납

카드 태그 → 니퍼 반출 → 대시보드에 "니퍼 OUT / 홍길동 / 14:32" 표시 → 반납 → 자동 해제

### 2. 미반납 경고

제한 시간 초과(시연용 1분) → 부저 연속음 + 적색 LED + 대시보드 경고

### 3. 미확인 반출 (도난 감시)

태그 없이 공구 반출 → 즉시 부저 단속음 + 적색 LED + 스냅샷 저장 + 대시보드 경고

### 4. 강건성

손으로 공구판 가리기 / 공구를 다른 자리에 걸기 → 디바운스로 오경고 없음

### 5. 웹 HMI

폰으로 `http://<노트북IP>:5000` 접속 → 임계 시간 조정 / 경고 수동 해제 시연

---

## 경광등 색상 표

| 색상 | 상태 | 부저 |
|---|---|---|
| 🟢 녹색 | 전 공구 재고 정상 | 없음 |
| 🟡 황색 | 정상 대여 중 (제한 시간 이내) | 없음 |
| 🔴 적색 | 미반납 초과 또는 미확인 반출 | 연속음(미반납) / 단속음(미확인) |

> 우선순위: 적 > 황 > 녹 — 여러 공구가 서로 다른 상태일 때 가장 심각한 상태로 표시

---

## API

### `POST /frame`

Pi 클라이언트가 호출하는 유일한 엔드포인트.

**요청:**
- `X-Frame-Token` (헤더): 공유 인증 토큰 (불일치 시 401)
- `image` (multipart file): JPEG 프레임
- `uid` (form field): 최근 태그된 RFID UID (없으면 빈 문자열)

**응답:**
```json
{
  "light": "green",
  "buzzer": "off",
  "interval": 3
}
```

`interval`은 서버가 지시하는 다음 캡처 주기 — 변화 관측 중이면 0.5초로 단축(적응형 촬영).

### `GET /`

관리자 대시보드 (로그인 필요)

### `GET /me`

학생용 본인 대여 현황 페이지

---

## 라이선스

학교 프로젝트용 — 별도 라이선스 미지정
