"""
Coupang 수집기 — CLI 어댑터 (기본: 카테고리 모드)
=====================================================
Qt 비의존 코어 엔진(app.core.coupang.search_crawler)을 호출하는 CLI.

컨셉 전환(2026-08-24): 기본 수집 경로는 카테고리 리스팅 페이지 순회.
키워드 모드(층1/2)는 페이지네이션 비로그인 미제공으로 하위 호환만 유지.

Usage (카테고리 모드 — 권장):
    xvfb-run -a python coupang_search_crawler.py --category-id 221934 \
        --category-name 출산_유아동 [--max-pages 17] [--output FILE]

Usage (키워드 모드 — 레거시):
    xvfb-run -a python coupang_search_crawler.py --keyword 뷰티 [--category-id 176522]

운영 규율(docs/coupang/EXTERNAL_RESEARCH.md):
    - 세션 간격 ≥ 30분, 일일 ≤ 5세션
    - 차단 감지 시 즉시 중단(자동 재시도 없음) — 수시간 쿨다운 후 재실행
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.base import Control
from app.core.coupang.outcome import RunOutcome, determine_outcome
from app.core.coupang.search_crawler import (
    DEFAULT_SORTERS,
    SearchCrawler,
    SearchRunConfig,
)

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_RECORDS = 2
EXIT_SAVE_ERROR = 3
EXIT_CANCELLED = 130
EXIT_BLOCKED = 4

_OUTCOME_TO_EXIT = {
    RunOutcome.SAVE_ERROR: EXIT_SAVE_ERROR,
    RunOutcome.CLEANUP_ERROR: EXIT_ERROR,
    RunOutcome.CANCELLED: EXIT_CANCELLED,
    RunOutcome.ERROR: EXIT_ERROR,
    RunOutcome.NO_RECORDS: EXIT_NO_RECORDS,
    RunOutcome.SUCCESS: EXIT_OK,
}


def determine_exit_code(summary) -> int:
    code = _OUTCOME_TO_EXIT[determine_outcome(summary)]
    if summary.termination_reason == "blocked":
        return EXIT_BLOCKED
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="Coupang category/keyword crawler")
    parser.add_argument("--keyword", default="",
                        help="검색어 (레거시 키워드 모드. 카테고리 모드에서는 생략)")
    parser.add_argument("--category-id", default="",
                        help="카테고리 ID (예: 출산/유아동=221934, 뷰티=176522) — 권장 경로")
    parser.add_argument("--category-name", default="",
                        help="카테고리 이름 (로그/출력 파일명 표시용)")
    parser.add_argument("--exclude-rocket", action="store_true", default=True,
                        help="로켓배송 상품 제외 (기본: 제외)")
    parser.add_argument("--include-rocket", dest="exclude_rocket", action="store_false",
                        help="로켓배송 상품 포함")
    parser.add_argument("--page-delay-min", type=float, default=15.0)
    parser.add_argument("--page-delay-max", type=float, default=20.0)
    parser.add_argument("--no-price-bands", action="store_true",
                        help="층2 가격 밴드 수집 비활성화 (키워드 모드 전용)")
    parser.add_argument("--max-pages", type=int, default=17,
                        help="카테고리 PLP 최대 페이지 (기본 17, 실측 상한)")
    parser.add_argument("--warmup-time", type=float, default=20.0)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    if not args.keyword.strip() and not args.category_id.strip():
        parser.error("--category-id 또는 --keyword 중 하나는 필수입니다 "
                     "(권장: --category-id)")

    ts = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    category_only = not args.keyword.strip()
    if args.output:
        output_prefix = args.output
    elif category_only:
        import re as _re
        safe = _re.sub(r"[^\w가-힣]+", "_", args.category_name)[:30].strip("_") \
            or args.category_id
        output_prefix = f"coupang_category_{safe}_{ts}"
    else:
        output_prefix = f"coupang_search_{ts}"

    config = SearchRunConfig(
        output_dir=OUTPUT_DIR,
        output_prefix=output_prefix,
        keyword=args.keyword,
        category_id=args.category_id,
        category_name=args.category_name,
        sorters=DEFAULT_SORTERS,
        exclude_rocket=args.exclude_rocket,
        page_delay_min=args.page_delay_min,
        page_delay_max=args.page_delay_max,
        include_price_bands=not args.no_price_bands,
        max_pages=args.max_pages,
        warmup_time=args.warmup_time,
        batch_size=args.batch_size,
    )

    print(f"{'=' * 60}")
    if category_only:
        label = args.category_name or args.category_id
        print("Coupang 카테고리 수집기")
        print(f"Time: {ts}")
        print(f"Category: {label} ({args.category_id}) max_pages={args.max_pages}")
        print(f"Rocket: {'제외' if config.exclude_rocket else '포함'}")
        print(f"Delay: {args.page_delay_min}~{args.page_delay_max}s")
    else:
        print("Coupang 키워드 검색 수집기 (레거시)")
        print(f"Time: {ts}")
        print(f"Keyword: {args.keyword}")
        print(f"Sorters: {', '.join(config.sorters)} (랭킹순 제외)")
        print(f"Rocket: {'제외' if config.exclude_rocket else '포함'}")
        print(f"Price bands: {'활성' if config.include_price_bands else '비활성'}")
        print(f"Category: {config.category_id or '(없음 — SRP+밴드만)'} max_pages={config.max_pages}")
        print(f"Delay: {args.page_delay_min}~{args.page_delay_max}s")
    print(f"{'=' * 60}")

    control = Control()
    crawler = SearchCrawler(
        config=config,
        control=control,
        on_log=lambda msg: print(msg),
        on_phase=lambda name, cur, tot: print(f"\n[Phase {cur}/{tot}] {name}"),
    )

    summary = crawler.run()

    print(f"\n{'=' * 60}")
    print("RESULTS")
    print(f"{'=' * 60}")
    print(f"  Products scraped: {summary.products_seen}")
    print(f"  Unique vendors: {summary.unique_vendors}")
    print(f"  With business info: {summary.business_info_success}")
    print(f"  Request errors: {summary.request_errors}")
    print(f"  Termination: {summary.termination_reason}")
    if summary.json_path:
        print(f"  JSON: {summary.json_path}")
    if summary.csv_path:
        print(f"  CSV:  {summary.csv_path}")

    return determine_exit_code(summary)


if __name__ == "__main__":
    raise SystemExit(main())
