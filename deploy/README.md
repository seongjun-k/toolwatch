# Pi 4 systemd 배포 절차

대상: Raspberry Pi 4 4GB, 서버+클라이언트 단일 기기 구성. 상세 판단 근거는 `docs/Pi-이관계획.md` 참조.

## 0. venv 생성 (서비스 파일이 `.venv/bin/python`을 가리킴)

picamera2는 pip이 아니라 OS 패키지로 설치돼 있어 **일반 venv에서는 import되지 않는다.**
반드시 시스템 패키지를 상속하는 옵션으로 만들 것:

```
cd ~/toolwatch
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install mfrc522
```

venv 디렉토리 이름을 `.venv` 외로 바꾸면 두 service 파일의 ExecStart 경로도 같이 고쳐야 한다.

## 1. 클라이언트 사용자 권한

서비스 등록보다 **먼저** 해야 한다. 그룹이 빠진 상태로 클라이언트를 띄우면 picamera2/MFRC522가
권한 오류로 죽고 `Restart=always`가 재시작을 반복하다 start-limit에 걸린다.
그룹 반영에는 재로그인(또는 재부팅)이 필요하다:

```
sudo usermod -aG video,gpio,spi $(whoami)
```

## 2. 서비스 등록 + placeholder 치환

placeholder 치환은 `/etc/systemd/system/`에 복사한 **사본**에 한다. 저장소의 service 파일을
직접 sed하면 작업트리가 계속 dirty 상태가 되고 다음 `git pull`에서 충돌한다.

```
cd ~/toolwatch
sudo cp deploy/toolwatch-server.service deploy/toolwatch-client.service /etc/systemd/system/
sudo sed -i "s|__USER__|$(whoami)|g; s|__REPO__|$(pwd)|g" \
  /etc/systemd/system/toolwatch-server.service /etc/systemd/system/toolwatch-client.service
sudo systemctl daemon-reload
sudo systemctl enable --now toolwatch-server
sudo systemctl enable --now toolwatch-client
```

## 3. 로그 확인

```
journalctl -u toolwatch-server -f
journalctl -u toolwatch-client -f
```

## 4. Pi 단일 기기 config

Pi는 서버와 클라이언트가 한 기기에 있으므로 `src/pi/config.json`의 `server_url`을
`http://localhost:5000/frame`으로 변경한다 (frame_token은 노트북 config.json과 동일하게 유지).

**경고**: `src/pi/config.json`의 `capture_size`를 바꾸면 프레임 픽셀 좌표계가 바뀌므로
서버 config의 `roi`를 반드시 다시 측정해야 한다. roi가 프레임 범위를 벗어나도 예외 없이
검은 여백으로 크롭되어 검출 0개가 되고, 등록된 공구 전체가 대여 중으로 확정된다(유령 반출).

## 5. 추론이 느릴 때 대응 순서

`docs/Pi-이관계획.md` 2단계 참조. 순서: NCNN 변환 → `imgsz` 축소(config) → `capture_interval_sec` 완화(Pi config) → `server_threads` 축소(config).
