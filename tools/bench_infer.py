"""Pi 이관 2단계 판단 지점 — 실제 추론 경로(core.detect_tools) 프레임당 시간 실측.

사용 (저장소 루트에서):
    python tools/bench_infer.py [이미지경로] [횟수]

합격 기준은 docs/Pi-이관계획.md 2단계 참조 (Pi 4 4GB: 1.5초 이하).
단독 수치는 낙관적이다 — 서버·클라이언트를 동시에 띄운 상태에서 재실측할 것.
"""

import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.server import core  # noqa: E402


def find_sample():
    for pattern in ("src/server/snapshots/*.jpg", "dataset/**/*.jpg", "dataset/**/*.png"):
        for path in sorted(ROOT.glob(pattern)):
            return path
    return None


def main():
    sample = Path(sys.argv[1]) if len(sys.argv) > 1 else find_sample()
    if sample is None or not sample.exists():
        sys.exit("샘플 이미지를 찾지 못했습니다. 경로를 인자로 넘기세요.")
    runs = int(sys.argv[2]) if len(sys.argv) > 2 else 20

    frame = sample.read_bytes()
    print(f"샘플: {sample}  ({len(frame) / 1024:.0f} KB), 반복: {runs}회")

    t0 = time.perf_counter()
    result = core.detect_tools(frame)  # 첫 회는 모델 로드 포함이라 통계에서 제외
    print(f"모델 로드+첫 추론: {time.perf_counter() - t0:.2f}초, 검출: {result}")

    times = []
    for _ in range(runs):
        t = time.perf_counter()
        core.detect_tools(frame)
        times.append(time.perf_counter() - t)

    times.sort()
    print(
        f"중앙값 {statistics.median(times):.3f}초 / 평균 {statistics.fmean(times):.3f}초 "
        f"/ 최소 {times[0]:.3f} / 최대 {times[-1]:.3f}"
    )


if __name__ == "__main__":
    main()
