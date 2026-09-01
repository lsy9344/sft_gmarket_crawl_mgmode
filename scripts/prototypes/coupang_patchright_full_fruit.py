"""CLI: Patchright 과일 전량 수집의 상품 목록 단계를 실행한다."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.coupang.patchright_full_fruit import run_listing_batch

EXIT_CODES = {
    "listing_batch_completed": 0,
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
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.home() / "Desktop" / "PatchrightFruit",
    )
    args = parser.parse_args()
    try:
        result = run_listing_batch(
            output_dir=args.output_dir,
            limit=args.limit,
            on_event=_print_state,
        )
    except ValueError as error:
        parser.error(str(error))
    _print_state(result)
    return EXIT_CODES.get(result["event"], 1)


if __name__ == "__main__":
    raise SystemExit(main())
