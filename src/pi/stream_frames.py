"""Pi 카메라 MJPEG 프레임을 stdout으로 흘려보낸다. 각 프레임 = [4바이트 길이(BE)] + JPEG.
노트북의 capture_pi.py 가 SSH로 이 스크립트를 실행해 미리보기/촬영에 쓴다."""
import struct
import sys
import time

from picamera2 import Picamera2
from picamera2.encoders import JpegEncoder
from picamera2.outputs import Output


class FrameOut(Output):
    def outputframe(self, frame, *args, **kwargs):
        out = sys.stdout.buffer
        out.write(struct.pack(">I", len(frame)))
        out.write(frame)
        out.flush()


picam = Picamera2()
picam.configure(picam.create_video_configuration(main={"size": (1280, 720)}))
picam.start_recording(JpegEncoder(q=80), FrameOut())
try:
    while True:
        time.sleep(1)
except (KeyboardInterrupt, BrokenPipeError):
    pass
finally:
    picam.stop_recording()
