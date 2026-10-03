"""CLI: Patchright 카테고리 상위 데이터셋(3P 1,000개) 목록 단계를 실행한다."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.coupang.patchright_top_thousand import (
    TOP_CATEGORIES,
    load_categories_file,
    run_top_pages,
)

EXIT_CODES = {
    "top_pages_completed": 0,
    "top_category_completed": 0,
    "top_collection_complete": 30,
    "blocked": 20,
    "guard_refused": 21,
    "cancelled": 22,
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
    parser.add_argument(
        "--proxy-session-id",
        default="",
        help="Decodo 스티키 세션 ID(인스턴스별 고정, 예: b01)",
    )
    parser.add_argument(
        "--categories-file",
        type=Path,
        default=None,
        help=(
            "이 인스턴스가 수집할 카테고리 가족 정의 JSON "
            "(병렬 확장 설계 §3.1). 없으면 기본 A 가족(현행 동작)"
        ),
    )
    args = parser.parse_args()
    # 가족 파일은 상태 출력·수집 양쪽에서 같은 목록을 쓰게 여기서 한 번만 읽는다.
    try:
        categories = (
            load_categories_file(args.categories_file)
            if args.categories_file
            else None
        )
    except ValueError as error:
        parser.error(str(error))
    if args.status_only:
        from app.core.coupang.patchright_top_thousand import read_state

        state = read_state(args.output_dir, categories)
        _print_state(
            {
                "event": "top_status",
                "categories": [
                    {
                        "category_id": category_id,
                        "category_name": name,
                        "record": state["categories"][category_id],
                    }
                    for category_id, name in (
                        categories if categories is not None else TOP_CATEGORIES
                    )
                ],
                "category_index": state["category_index"],
                "page_number": state["page_number"],
                "status": state["status"],
            }
        )
        return 0
    proxy = None
    if args.proxy_session_id:
        proxy = _resolve_proxy(args.proxy_session_id)
        if proxy is None:
            # 브라우저를 열기 전에 거부한다 — 자격 없이 세션을 만들 수 없다.
            _print_state({"event": "proxy_credentials_missing"})
            return EXIT_CODES["proxy_credentials_missing"]
    try:
        result = run_top_pages(
            output_dir=args.output_dir,
            page_count=args.pages,
            on_event=_print_state,
            state_root=args.state_root,
            proxy=proxy,
            categories=categories,
        )
    except ValueError as error:
        parser.error(str(error))
    _print_state(result)
    return EXIT_CODES.get(result["event"], 1)


if __name__ == "__main__":
    raise SystemExit(main())
