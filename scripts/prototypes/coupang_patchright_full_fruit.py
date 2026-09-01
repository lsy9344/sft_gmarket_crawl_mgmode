"""CLI: Patchright 과일 전량 수집의 상품 목록 단계를 실행한다."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.coupang.patchright_canary import (
    authorize_block_recovery,
    record_recovery_hold,
)
from app.core.coupang.patchright_full_fruit import (
    run_listing_batch,
    run_listing_category,
    run_listing_pages,
)

EXIT_CODES = {
    "listing_batch_completed": 0,
    "listing_pages_completed": 0,
    "listing_category_completed": 0,
    "blocked": 20,
    "guard_refused": 21,
    "cancelled": 22,
    "failed": 1,
    "incomplete_limit_reached": 1,
}


def _print_state(state: dict) -> None:
    print(json.dumps(state, ensure_ascii=True, sort_keys=True), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Patchright 과일 상품 목록 단계")
    parser.add_argument("--limit", type=int, default=24)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--pages",
        type=int,
        default=1,
        help="한 Chrome에서 처리할 페이지 수(1~10)",
    )
    mode.add_argument(
        "--category",
        action="store_true",
        help="한 Chrome에서 현재 하위 카테고리 종료 조건까지 처리",
    )
    mode.add_argument(
        "--recovery-probe",
        action="store_true",
        help="차단 1시간 뒤 현재 위치의 상품 8개만 한 번 확인",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.home() / "Desktop" / "PatchrightFruit",
    )
    args = parser.parse_args()
    try:
        if args.recovery_probe:
            allowed, reason = authorize_block_recovery()
            if not allowed:
                result = {"event": "guard_refused", "reason": reason}
            else:
                result = run_listing_batch(
                    output_dir=args.output_dir,
                    limit=8,
                    on_event=_print_state,
                )
                record_recovery_hold()
        elif args.category:
            result = run_listing_category(
                output_dir=args.output_dir,
                on_event=_print_state,
            )
        elif args.pages == 1:
            result = run_listing_batch(
                output_dir=args.output_dir,
                limit=args.limit,
                on_event=_print_state,
            )
        else:
            result = run_listing_pages(
                output_dir=args.output_dir,
                page_count=args.pages,
                on_event=_print_state,
            )
    except ValueError as error:
        parser.error(str(error))
    _print_state(result)
    return EXIT_CODES.get(result["event"], 1)


if __name__ == "__main__":
    raise SystemExit(main())
