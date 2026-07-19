"""학습된 모델로 미라벨 사진에 YOLO 포맷 라벨(txt)을 자동 생성하는 부트스트랩 도구.

시드 학습(수동 라벨 30~50장) 후 나머지 사진을 이 스크립트로 예측해 라벨을 만들고,
Roboflow에 사진+txt를 함께 올려 틀린 박스만 검수한다. 사용법: docs/라벨링-가이드.md.

실행 위치: 호스트 PC, 저장소 루트.
  python tools/autolabel.py dataset/raw/20260720_103000 --model model/best.pt
"""
import argparse
from pathlib import Path

from ultralytics import YOLO


def main():
    parser = argparse.ArgumentParser(description="사진 폴더에 YOLO 라벨 txt 자동 생성")
    parser.add_argument("images", help="사진 폴더 경로")
    parser.add_argument("--model", default="model/best.pt", help="예측에 쓸 가중치")
    parser.add_argument("--conf", type=float, default=0.35,
                        help="확신도 임계값 — 검수 단계가 있으므로 낮게 잡아 누락을 줄인다")
    args = parser.parse_args()

    img_dir = Path(args.images)
    model = YOLO(args.model)

    count = 0
    for r in model.predict(source=str(img_dir), conf=args.conf, stream=True, verbose=False):
        lines = []
        for box in r.boxes:
            cls = int(box.cls)
            x, y, w, h = box.xywhn[0].tolist()
            lines.append(f"{cls} {x:.6f} {y:.6f} {w:.6f} {h:.6f}")
        Path(r.path).with_suffix(".txt").write_text("\n".join(lines), encoding="ascii")
        count += 1

    # Roboflow가 클래스 번호↔이름을 알 수 있게 data.yaml을 폴더에 같이 둔다
    names = [model.names[i] for i in sorted(model.names)]
    (img_dir / "data.yaml").write_text(
        "nc: %d\nnames: %s\n" % (len(names), names), encoding="utf-8"
    )
    print(f"{count}장 라벨 생성 완료 → {img_dir} (클래스: {', '.join(names)})")


if __name__ == "__main__":
    main()
