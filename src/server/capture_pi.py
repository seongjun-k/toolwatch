"""Pi 카메라 데이터셋 촬영 (노트북에서 실행). 미리보기 창: SPACE=저장, Q/ESC=종료.

SSH로 Pi의 stream_frames.py 를 실행해 프레임을 받아 미리보기하고,
SPACE 누른 순간의 프레임을 노트북 dataset/raw/<실행시각>/ 에 저장한다.
사용자가 직접 SSH 접속할 필요 없음 (키 인증 이미 설정됨).

사용:  python src/server/capture_pi.py
"""
import struct
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
PI_HOST = "toolwatch@100.70.117.78"
REMOTE_CMD = "python3 -u ~/toolwatch/src/pi/stream_frames.py"

out_dir = ROOT / "dataset" / "raw" / datetime.now().strftime("%Y%m%d_%H%M%S")
out_dir.mkdir(parents=True, exist_ok=True)

proc = subprocess.Popen(
    ["ssh", "-o", "ConnectTimeout=8", PI_HOST, REMOTE_CMD],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
)


def recvall(n):
    """stdout에서 정확히 n바이트 읽기. 스트림 끊기면 None."""
    chunks = b""
    while len(chunks) < n:
        part = proc.stdout.read(n - len(chunks))
        if not part:
            return None
        chunks += part
    return chunks


print(f"저장 폴더: {out_dir}")
print("SPACE=촬영, Q 또는 ESC=종료")

count = 0
while True:
    header = recvall(4)
    if header is None:
        err = proc.stderr.read().decode(errors="replace")
        print("스트림 종료.", err[-500:] if err else "")
        break
    (length,) = struct.unpack(">I", header)
    data = recvall(length)
    if data is None:
        print("프레임 수신 중 스트림 끊김")
        break

    frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        continue

    view = frame.copy()
    cv2.putText(view, f"saved: {count}   SPACE=shot  Q=quit",
                (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
    cv2.imshow("pi camera", view)

    key = cv2.waitKey(1) & 0xFF
    if key in (ord("q"), 27):
        break
    if key == 32:
        count += 1
        path = out_dir / f"{count:06d}.jpg"
        cv2.imwrite(str(path), frame)
        print(f"저장 {path.name}")

proc.terminate()
cv2.destroyAllWindows()
print(f"총 {count}장 -> {out_dir}")
