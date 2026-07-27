# Pi 이관 계획 — 노트북 서버 → Pi 단일 기기 (2026-07-18 수립, 2026-07-22 Pi 4 4GB로 변경)

현재는 노트북(Windows)이 서버(waitress HTTP:5000 + YOLO 추론)이고 Pi는 엣지 클라이언트다.
**대상 기기: Raspberry Pi 4 Model B 4GB** (Pi 5 8GB 안이었으나 보유 장비로 변경).
서버·추론·클라이언트를 Pi 한 대로 합쳐 노트북 상시 가동을 없앤다.
학습(재학습)만 노트북 GPU에서 계속하고, 가중치(best.pt)만 Pi로 배포한다.

## 핵심 판단 (먼저 읽을 것)

1. **이관 시점은 실사용자 생기기 전.** PWA 설치와 웹 푸시 구독은 접속 도메인에 묶인다.
   Funnel 주소가 `seongjun-desktop...` → `seongjun-pi5...`로 바뀌면 전원 재설치·재구독이라,
   학생이 쓰기 시작한 뒤에는 이관 비용이 급증한다.
2. **불확실성은 2단계(추론 속도)와 메모리 두 곳.** 서버 스택(Flask·waitress·SQLite·pywebpush)은
   순수 파이썬이라 이식 리스크 없음. 코드 수정도 사실상 config 한 줄(server_url→localhost).
3. **Pi 4는 Pi 5 대비 CPU가 대략 절반 수준**이라 PyTorch 직접 추론으로 목표 시간을 맞추기 어렵다(추정).
   NCNN 변환을 "초과 시 대응"이 아니라 **기본 전제**로 잡는다. 2단계에서 실측으로 확정.

## 0단계 — 사전 준비 (노트북, Pi 없이 지금 가능)

- [ ] 실환경 학습으로 `model/best.pt` 확보 (현재 yolo11n 폴백 상태)
- [ ] 미커밋분 커밋·푸시 → 저장소를 이관 기준점으로

## 1단계 — Pi 기본 세팅 (반나절)

- [ ] Raspberry Pi OS 64bit(Bookworm) — **32bit 이미지면 torch arm64 휠이 안 맞음, 반드시 64bit 확인**
- [ ] 유선랜 권장, 방열판+팬 필수 (Pi 4는 연속 부하 시 스로틀링 쉬움)
- [ ] Tailscale 설치·로그인 (기존 tailnet: tailf456a)
- [ ] `git clone https://github.com/seongjun-k/toolwatch.git`
- [ ] venv는 **`python3 -m venv --system-site-packages .venv`** — picamera2는 OS 패키지라
  일반 venv에서는 import 자체가 안 된다 (절차: `deploy/README.md` 0장)
- [ ] `pip install -r requirements.txt` (torch CPU arm64 자동 포함, **4GB라 설치 중 OOM 가능 → swap 2GB 선확보**)
- [ ] swap 확대: `/etc/dphys-swapfile`의 `CONF_SWAPSIZE=2048` 후 `sudo systemctl restart dphys-swapfile`
- [ ] Pi 전용: picamera2(OS 기본), `pip install mfrc522` (gpiozero 기본 포함)

## 2단계 — 추론 성능 실측 (판단 지점)

- [ ] `best.pt` CPU 추론 프레임당 시간 실측 (**단독 실측이 아니라 서버+클라이언트 동시 구동 상태에서**)
- 합격 기준: **1.5초 이하** (Pi 클라이언트 기본 주기 3초의 50%. Pi 5 기준 0.6초에서 완화)
- [ ] `yolo export model=best.pt format=ncnn` 변환 후 `model_path`를 변환본 폴더로 변경
  (ultralytics가 NCNN 로드 지원, 통상 2~4배 가속. Pi 4에서는 선택이 아니라 기본으로 간주)
- 그래도 초과 시 완화 순서:
  1. Pi config `capture_interval_sec` 3 → 5 (검출 지연 허용치 내)
  2. 추론 해상도 640 → 416 축소 (`src/server/config.json`의 `imgsz` 키로 추가됨, 코드 수정 불필요)
  3. waitress `threads=8` → 2~4 (`src/server/config.json`의 `server_threads` 키로 추가됨, 코드 수정 불필요, 4GB 메모리·4코어 대비 과다)
- [ ] 메모리 실측: 서버+클라이언트 동시 구동 시 `free -h`로 여유 확인 (torch+ultralytics만 1.5GB 내외 추정)

## 3단계 — 서버+클라이언트 동거 구성

- [ ] 서버 config: 노트북 `src/server/config.json` 복사 (시크릿 유지)
- [ ] DB 이전: 노트북 `src/server/toolwatch.db` 파일 복사 (이력·계정 전부 포함)
- [ ] Pi config: `server_url`만 `http://localhost:5000/frame`으로 변경 (frame_token 동일 유지)
- [ ] systemd 서비스 2개 등록 (부팅 자동 시작, Restart=always): `deploy/toolwatch-server.service`,
  `deploy/toolwatch-client.service` 설치 절차는 `deploy/README.md` 참조
- [ ] DB 백업 이식: run_server.bat의 날짜별 스냅샷을 `deploy/backup_db.sh`로 이식, 서버 서비스 ExecStartPre에서 실행

## 4단계 — 외부 접속 전환

- [ ] Pi에서 `tailscale funnel --bg http://localhost:5000`
- 새 주소: `https://<Pi 호스트명>.tailf456a.ts.net` (HTTPS Certificates는 tailnet에 이미 활성)
- [ ] 학생 안내: PWA 재설치 + 알림 재구독 (도메인 변경 때문 — 위 "핵심 판단 1")

## 5단계 — 병행 검증 후 절체

- [ ] Pi에서 `python src/server/app.py --selfcheck`
- [ ] 프레임 시뮬레이션 (2026-07-18 세션과 동일: 토큰+JPEG POST → OUT/경보 왕복)
- [ ] 실물: 카메라 검출 → 경광등/부저 → RFID 태그 귀속 → 반납(IN) 경로
- [ ] 통과 후 노트북 서버 영구 종료, 노트북 Funnel 등록 해제

## 리스크

- 발열: Pi 4는 연속 추론 시 스로틀링 위험이 Pi 5보다 큼. 방열판+팬 필수,
  `vcgencmd measure_temp`·`vcgencmd get_throttled`로 시연 전 확인. 스로틀링 시 추론 시간 재실측
- 메모리(4GB): torch·ultralytics 상주 + waitress 스레드가 겹치면 여유가 얇다.
  OOM 발생 시 waitress threads 축소 → NCNN 전환 → 그래도 안 되면 Pi 5 재검토
- SD 수명: DB 쓰기 빈도 낮아 실질 무해하나 백업 습관 유지
- 시연 중 정전/재부팅: systemd 자동 복구 + restore_rented_state로 대여 상태 복원됨 (검증된 경로)
