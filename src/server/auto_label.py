"""로컬 자동 라벨링. 폴더별 클래스가 이미 정해져 있으니(nipper/ driver/)
YOLO-World로 위치만 잡고 클래스는 폴더명으로 지정한다.
결과: dataset/yolo/{images,labels}/{train,val} + data.yaml
"""
import random
import shutil
from pathlib import Path

from ultralytics import YOLOWorld

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "dataset" / "raw"
OUT = ROOT / "dataset" / "yolo"
CLASSES = ["nipper", "driver"]                      # id 0, 1
# 폴더로 클래스가 이미 정해져 있어 위치만 잡으면 됨. 프롬프트는 넉넉히.
PROMPTS = ["screwdriver", "pliers", "wire cutter", "hand tool", "tool", "pen"]
VAL_RATIO = 0.15
CONF = 0.005                                        # 통제된 촬영이라 최대한 낮게

random.seed(0)
for sub in ("images/train", "images/val", "labels/train", "labels/val"):
    d = OUT / sub
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)

model = YOLOWorld("yolov8x-worldv2.pt")
model.set_classes(PROMPTS)

misses = []
n_ok = 0
for cid, cname in enumerate(CLASSES):
    imgs = sorted((RAW / cname).glob("*.jpg"))
    random.shuffle(imgs)
    n_val = int(len(imgs) * VAL_RATIO)
    for i, img in enumerate(imgs):
        split = "val" if i < n_val else "train"
        r = model.predict(str(img), conf=CONF, verbose=False)[0]
        if len(r.boxes) == 0:
            misses.append(img.name)
            continue
        # 가장 확신 높은 박스 1개만 사용
        b = r.boxes[r.boxes.conf.argmax()]
        x1, y1, x2, y2 = b.xyxyn[0].tolist()
        xc, yc, w, h = (x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1
        shutil.copy(img, OUT / "images" / split / img.name)
        (OUT / "labels" / split / f"{img.stem}.txt").write_text(
            f"{cid} {xc:.6f} {yc:.6f} {w:.6f} {h:.6f}\n"
        )
        n_ok += 1

(OUT / "data.yaml").write_text(
    f"path: {OUT.as_posix()}\ntrain: images/train\nval: images/val\n"
    f"nc: {len(CLASSES)}\nnames: {CLASSES}\n",
    encoding="utf-8",
)
print(f"라벨 생성 {n_ok}장, 미탐지 {len(misses)}장")
if misses:
    print("미탐지:", ", ".join(misses[:30]), "..." if len(misses) > 30 else "")
