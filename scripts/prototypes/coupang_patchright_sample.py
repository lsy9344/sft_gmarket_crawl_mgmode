"""PROTOTYPE CLI: Patchright로 쿠팡 판매자 정보를 최대 3건만 저장한다."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.coupang.patchright_sample import (  # noqa: E402
    MAX_SAMPLE_ITEMS,
    run_sample,
)

EXIT_CODES = {
    "sample_completed": 0,
    "blocked": 20,
    "guard_refused": 21,
    "cancelled": 22,
    "failed": 1,
}


def _print_state(state: dict) -> None:
    visible = dict(state)
    if "records" in visible:
        visible["records"] = f"{len(visible['records'])} record(s) saved"
    print(json.dumps(visible, sort_keys=True), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Patchright 쿠팡 판매자 최대 3건 표본 수집 시험"
    )
    parser.add_argument("--category", required=True, metavar="CATEGORY_ID")
    parser.add_argument(
        "--limit",
        type=int,
        default=MAX_SAMPLE_ITEMS,
        help=f"상품·판매자 상한(1~{MAX_SAMPLE_ITEMS})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.home() / "Desktop" / "PatchrightSample",
    )
    args = parser.parse_args()
    try:
        result = run_sample(
            category_id=args.category,
            output_dir=args.output_dir,
            limit=args.limit,
            on_event=_print_state,
        )
    except ValueError as error:
        parser.error(str(error))
    return EXIT_CODES.get(result["event"], 1)


if __name__ == "__main__":
    raise SystemExit(main())
