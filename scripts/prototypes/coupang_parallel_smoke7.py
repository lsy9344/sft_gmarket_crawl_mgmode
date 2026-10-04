"""7-인스턴스 병렬 라이브 실측 — 패션/뷰티 가족 7개, 세션 1회씩 순차 실행.

UI/엔진 상한 8(7-병렬 실측 반영) 검증용. 직접 회선 1 + Decodo 스티키 6(i2~i7)이
각각 다른 가족을 1세션(목록 8페이지)씩 수집한다. 실행은 순차(설계상 브라우저 1개)
— 스케줄 interleaving으로 병렬성 확보가 이 엔진의 원리다.
상태 루트: %LOCALAPPDATA%\\SellerCollector\\coupang_parallel\\{1..7}(기존 운용과 분리).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.base import Control  # noqa: E402
from app.core.coupang.parallel_manager import (  # noqa: E402
    ParallelCoupangManager,
    ParallelRunConfig,
)

FAMILIES = [
    [("186764", "패션의류/잡화/여성패션")],
    [("187069", "패션의류/잡화/남성패션")],
    [("502993", "패션의류/잡화/남녀 공용 의류")],
    [("565063", "패션의류/잡화/속옷/잠옷")],
    [("213201", "패션의류/잡화/유아동패션")],
    [("176530", "뷰티/스킨케어")],
    [("486551", "뷰티/클렌징/필링")],
]


def main() -> int:
    def on_event(event: dict) -> None:
        kind = event.get("type") or ""
        if kind in ("progress",) and "result" not in event:
            return  # 세션 중간 스냅숏은 생략 — 요약만
        print(json.dumps(event, ensure_ascii=True, default=str), flush=True)

    config = ParallelRunConfig(
        families=FAMILIES,
        output_dir=Path.home() / "Desktop" / "CoupangParallelSmoke7",
        instance_count=7,
    )
    manager = ParallelCoupangManager(config, Control(), on_event=on_event)
    lines = {key: value.config.line for key, value in manager.instances.items()}
    print(f"인스턴스 구성({len(lines)}개): {lines}", flush=True)
    if len(lines) != 7:
        print("강등 발생 — 기대한 7개 미만. 종료.", flush=True)
        return 1
    manager.run_due(time.time())
    print(f"SMOKE7_DONE all_done={manager.all_done()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
