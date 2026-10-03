"""CLI: 저장된 과일 상품의 판매자 공개 사업자정보를 이어서 수집한다."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.coupang.patchright_canary import _read_guard, record_block
from app.core.coupang.patchright_full_sellers import (
    MAX_SESSION_PRODUCTS,
    authorize_http_503_retry,
    run_seller_batch,
)

EXIT_CODES = {
    "seller_batch_completed": 0,
    "seller_collection_complete": 0,
    "blocked": 20,
    "guard_refused": 21,
    "cancelled": 22,
    "seller_halted": 23,
    "failed": 1,
}


def _print_state(state: dict) -> None:
    print(json.dumps(state, ensure_ascii=True, sort_keys=True), flush=True)


def _append_run_log(output_dir: Path, result: dict) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    value = {**result, "logged_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    with (output_dir / "fruit_seller_runs.jsonl").open(
        "a", encoding="utf-8"
    ) as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Patchright 과일 판매자 공개 사업자정보 수집"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=MAX_SESSION_PRODUCTS,
        help=f"한 실행의 상품 상한(1~{MAX_SESSION_PRODUCTS})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.home() / "Desktop" / "PatchrightFruit",
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        default=None,
        help="판매자 작업 전용 간격·수량 기록 폴더",
    )
    parser.add_argument(
        "--profile-root",
        type=Path,
        default=None,
        help="기존 Chrome 방문 기록을 둔 폴더",
    )
    parser.add_argument(
        "--resume-http-503",
        action="store_true",
        help="HTTP 503 뒤 3시간 이상 지난 예약 재시도 한 번을 허용",
    )
    args = parser.parse_args()
    try:
        shared_guard = _read_guard() if args.state_root is not None else {}
        if shared_guard.get("blocked"):
            result = {
                "event": "guard_refused",
                "reason": "공용 Patchright 차단 기록이 남아 있습니다.",
            }
        else:
            retry_allowed = True
            retry_reason = ""
            if args.resume_http_503:
                retry_allowed, retry_reason = authorize_http_503_retry(
                    args.output_dir
                )
            if not retry_allowed:
                result = {"event": "guard_refused", "reason": retry_reason}
            else:
                result = run_seller_batch(
                    output_dir=args.output_dir,
                    limit=args.limit,
                    on_event=_print_state,
                    state_root=args.state_root,
                    browser_profile_root=args.profile_root,
                )
    except ValueError as error:
        parser.error(str(error))
    if args.state_root is not None and result.get("event") == "blocked":
        record_block(reference=str(result.get("reference") or ""))
    _append_run_log(args.output_dir, result)
    _print_state(result)
    return EXIT_CODES.get(result["event"], 1)


if __name__ == "__main__":
    raise SystemExit(main())
