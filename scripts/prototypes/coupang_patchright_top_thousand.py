"""CLI: Patchright 카테고리 상위 데이터셋(3P 1,000개) 목록 단계를 실행한다."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.coupang.patchright_top_thousand import TOP_CATEGORIES, run_top_pages

EXIT_CODES = {
    "top_pages_completed": 0,
    "top_category_completed": 0,
    "top_collection_complete": 30,
    "blocked": 20,
    "guard_refused": 21,
    "cancelled": 22,
    "failed": 1,
}


def _print_state(state: dict) -> None:
    print(json.dumps(state, ensure_ascii=True, sort_keys=True), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Patchright 카테고리 3P 상위 데이터셋 수집"
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=8,
        help="한 Chrome에서 처리할 페이지 수(1~10)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.home() / "Desktop" / "PatchrightTop1000",
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        required=True,
        help="이전 단계와 함께 쓰는 공통 안전 기록 폴더",
    )
    parser.add_argument(
        "--status-only",
        action="store_true",
        help="접속 없이 현재 상태만 출력",
    )
    args = parser.parse_args()
    if args.status_only:
        from app.core.coupang.patchright_top_thousand import read_state

        state = read_state(args.output_dir)
        _print_state(
            {
                "event": "top_status",
                "categories": [
                    {
                        "category_id": category_id,
                        "category_name": name,
                        "record": state["categories"][category_id],
                    }
                    for category_id, name in TOP_CATEGORIES
                ],
                "category_index": state["category_index"],
                "page_number": state["page_number"],
                "status": state["status"],
            }
        )
        return 0
    try:
        result = run_top_pages(
            output_dir=args.output_dir,
            page_count=args.pages,
            on_event=_print_state,
            state_root=args.state_root,
        )
    except ValueError as error:
        parser.error(str(error))
    _print_state(result)
    return EXIT_CODES.get(result["event"], 1)


if __name__ == "__main__":
    raise SystemExit(main())
