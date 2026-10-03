"""CLI: 저장된 상위 데이터셋 상품의 판매자 공개 사업자정보를 이어서 수집한다."""

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
from app.core.coupang.patchright_top_sellers import (
    MAX_SESSION_PRODUCTS,
    TOP_SELLER_RUNS_FILENAME,
    authorize_http_503_retry,
    run_top_seller_batch,
)

EXIT_CODES = {
    "seller_batch_completed": 0,
    "seller_collection_complete": 0,
    "blocked": 20,
    "guard_refused": 21,
    "cancelled": 22,
    "seller_halted": 23,
    "proxy_credentials_missing": 24,
    "failed": 1,
}


def _print_state(state: dict) -> None:
    print(json.dumps(state, ensure_ascii=True, sort_keys=True), flush=True)


def _resolve_proxy(session_id: str) -> dict | None:
    """세션 ID로 스티키 프록시 dict를 조립한다. 자격 미완비면 None."""
    if not session_id:
        return None
    from app.core.decodo import credentials_ready, load_settings, sticky_proxy_dict

    settings = load_settings()
    if not credentials_ready(settings):
        return None
    return sticky_proxy_dict(settings, session_id)


def _append_run_log(output_dir: Path, result: dict) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    value = {**result, "logged_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    with (output_dir / TOP_SELLER_RUNS_FILENAME).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Patchright 상위 데이터셋 판매자 공개 사업자정보 수집"
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
        default=Path.home() / "Desktop" / "PatchrightTop1000",
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        default=None,
        help="목록 단계와 함께 쓰는 공통 안전 기록 폴더",
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
    parser.add_argument(
        "--proxy-session-id",
        default="",
        help="Decodo 스티키 세션 ID(인스턴스별 고정, 예: b01)",
    )
    args = parser.parse_args()
    proxy = None
    if args.proxy_session_id:
        proxy = _resolve_proxy(args.proxy_session_id)
        if proxy is None:
            # 브라우저를 열기 전에 거부한다 — 자격 없이 세션을 만들 수 없다.
            _print_state({"event": "proxy_credentials_missing"})
            return EXIT_CODES["proxy_credentials_missing"]
    try:
        shared_guard = _read_guard(args.state_root) if args.state_root else {}
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
                result = run_top_seller_batch(
                    output_dir=args.output_dir,
                    limit=args.limit,
                    on_event=_print_state,
                    state_root=args.state_root,
                    browser_profile_root=args.profile_root,
                    proxy=proxy,
                )
    except ValueError as error:
        parser.error(str(error))
    if args.state_root and result.get("event") == "blocked":
        record_block(args.state_root, reference=str(result.get("reference") or ""))
    _append_run_log(args.output_dir, result)
    _print_state(result)
    return EXIT_CODES.get(result["event"], 1)


if __name__ == "__main__":
    raise SystemExit(main())
