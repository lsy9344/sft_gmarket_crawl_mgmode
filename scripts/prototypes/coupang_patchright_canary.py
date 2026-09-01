"""PROTOTYPE CLI: 앱과 같은 Patchright 2페이지 시험을 실행한다."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.coupang.patchright_canary import MAX_ITEMS, run_canary  # noqa: E402

EXIT_CODES = {
    "offline_completed": 0,
    "live_completed": 0,
    "blocked": 20,
    "guard_refused": 21,
    "cancelled": 22,
    "failed": 1,
}


def _print_state(state: dict) -> None:
    print(json.dumps(state, sort_keys=True), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Patchright 실제 Chrome 소량 시험판")
    parser.add_argument(
        "--live-category",
        metavar="CATEGORY_ID",
        help="명시할 때만 쿠팡 홈 1회와 카테고리 1페이지를 엽니다.",
    )
    parser.add_argument(
        "--limit", type=int, default=5, help=f"읽을 링크 수(1~{MAX_ITEMS})"
    )
    args = parser.parse_args()
    try:
        result = run_canary(
            live_category=args.live_category,
            limit=args.limit,
            on_event=_print_state,
        )
    except ValueError as error:
        parser.error(str(error))
    return EXIT_CODES.get(result["event"], 1)


if __name__ == "__main__":
    raise SystemExit(main())
