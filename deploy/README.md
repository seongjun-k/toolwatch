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

## 1. placeholder 치환

Pi에서, 저장소 클론 디렉토리 기준:

```
cd ~/toolwatch
sed -i "s|__USER__|$(whoami)|g; s|__REPO__|$(pwd)|g" deploy/toolwatch-server.service deploy/toolwatch-client.service
```

## 2. 서비스 등록

```
sudo cp deploy/toolwatch-server.service deploy/toolwatch-client.service /etc/systemd/system/
chmod +x deploy/backup_db.sh
sudo systemctl daemon-reload
sudo systemctl enable --now toolwatch-server
sudo systemctl enable --now toolwatch-client
```

## 3. 로그 확인

```
journalctl -u toolwatch-server -f
journalctl -u toolwatch-client -f
```

## 4. 클라이언트 사용자 권한

카메라/GPIO/SPI 접근을 위해 서비스 User를 아래 그룹에 추가 후 재로그인(또는 재부팅):

```
sudo usermod -aG video,gpio,spi $(whoami)
```

## 5. Pi 단일 기기 config

Pi는 서버와 클라이언트가 한 기기에 있으므로 `src/pi/config.json`의 `server_url`을
`http://localhost:5000/frame`으로 변경한다 (frame_token은 노트북 config.json과 동일하게 유지).

## 6. 추론이 느릴 때 대응 순서

`docs/Pi-이관계획.md` 2단계 참조. 순서: NCNN 변환 → `imgsz` 축소(config) → `capture_interval_sec` 완화(Pi config) → `server_threads` 축소(config).
