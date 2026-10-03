"""병렬 매니저 라이브 스모크 — 직접 1 + Decodo 1 인스턴스로 세션 1회씩.

앱 편입(M3) 뒤 관리자·포팅 코어의 실접속 경로를 GUI 없이 검증한다.
- 가족 2개(각 카테고리 1개) → 인스턴스 1(직접)/2(Decodo)에 각각 배분
- run_due 1회 = 인스턴스별 목록 세션 1회(8페이지)만 실행하고 종료
- 상태 루트는 %LOCALAPPDATA%\SellerCollector\coupang_parallel\{1,2}(기존 운용과 분리)
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


def main() -> int:
    def on_event(event: dict) -> None:
        print(json.dumps(event, ensure_ascii=True, default=str), flush=True)

    config = ParallelRunConfig(
        families=[
            [("185671", "주방용품/냄비/프라이팬")],
            [("185735", "주방용품/그릇/홈세트")],
        ],
        output_dir=Path.home() / "Desktop" / "CoupangParallelSmoke",
        instance_count=2,
    )
    manager = ParallelCoupangManager(config, Control(), on_event=on_event)
    lines = {
        key: value.config.line for key, value in manager.instances.items()
    }
    print(f"인스턴스 구성: {lines}", flush=True)
    manager.run_due(time.time())
    print(f"SMOKE_DONE all_done={manager.all_done()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
