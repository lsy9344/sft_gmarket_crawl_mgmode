"""
Coupang 키워드 검색 수집기 — CLI 어댑터
=========================================
Qt 비의존 코어 엔진(app.core.coupang.search_crawler)을 호출하는 CLI.

Usage:
    xvfb-run -a python coupang_search_crawler.py --keyword 뷰티 [--exclude-rocket] \
        [--page-delay-min 15] [--page-delay-max 20] [--output FILE]

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
    parser = argparse.ArgumentParser(description="Coupang keyword search crawler")
    parser.add_argument("--keyword", required=True, help="검색어 (예: 뷰티)")
    parser.add_argument("--exclude-rocket", action="store_true", default=True,
                        help="로켓배송 상품 제외 (기본: 제외)")
    parser.add_argument("--include-rocket", dest="exclude_rocket", action="store_false",
                        help="로켓배송 상품 포함")
    parser.add_argument("--page-delay-min", type=float, default=15.0)
    parser.add_argument("--page-delay-max", type=float, default=20.0)
    parser.add_argument("--warmup-time", type=float, default=20.0)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    ts = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    output_prefix = args.output or f"coupang_search_{ts}"

    config = SearchRunConfig(
        output_dir=OUTPUT_DIR,
        output_prefix=output_prefix,
        keyword=args.keyword,
        sorters=DEFAULT_SORTERS,
        exclude_rocket=args.exclude_rocket,
        page_delay_min=args.page_delay_min,
        page_delay_max=args.page_delay_max,
        warmup_time=args.warmup_time,
        batch_size=args.batch_size,
    )

    print(f"{'=' * 60}")
    print("Coupang 키워드 검색 수집기")
    print(f"Time: {ts}")
    print(f"Keyword: {args.keyword}")
    print(f"Sorters: {', '.join(config.sorters)} (랭킹순 제외)")
    print(f"Rocket: {'제외' if config.exclude_rocket else '포함'}")
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
