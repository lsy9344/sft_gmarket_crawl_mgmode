"""
Coupang /np/omp Seller Business Info Crawler v3.0 — CLI adapter
================================================================
Qt 비의존 코어 엔진(app.core.coupang)을 호출하는 CLI 호환 어댑터.

Usage:
    xvfb-run -a python coupang_omp_crawler.py [--max-scroll-pages N] [--output FILE]
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.base import Control
from app.core.coupang.crawler import CoupangCrawler
from app.core.coupang.outcome import RunOutcome, determine_outcome
from app.models.coupang_records import CoupangRunConfig

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_RECORDS = 2
EXIT_SAVE_ERROR = 3
EXIT_CANCELLED = 130

_OUTCOME_TO_EXIT = {
    RunOutcome.SAVE_ERROR: EXIT_SAVE_ERROR,
    RunOutcome.CLEANUP_ERROR: EXIT_ERROR,
    RunOutcome.CANCELLED: EXIT_CANCELLED,
    RunOutcome.ERROR: EXIT_ERROR,
    RunOutcome.NO_RECORDS: EXIT_NO_RECORDS,
    RunOutcome.SUCCESS: EXIT_OK,
}


def determine_exit_code(summary) -> int:
    """WORK_ORDER §9 종료 코드 계약 — 공용 outcome 판정 사용."""
    return _OUTCOME_TO_EXIT[determine_outcome(summary)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Coupang /np/omp Seller Crawler v3")
    parser.add_argument("--max-scroll-pages", type=int, default=10,
                        help="Max getPromotion pages to fetch (50 items each)")
    parser.add_argument("--batch-size", type=int, default=10,
                        help="VendorItemIds per individualInfo batch")
    parser.add_argument("--warmup-time", type=int, default=20)
    parser.add_argument("--delay-min", type=float, default=1.0)
    parser.add_argument("--delay-max", type=float, default=2.5)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    ts = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    output_prefix = args.output or f"coupang_omp_sellers_{ts}"

    config = CoupangRunConfig(
        output_dir=OUTPUT_DIR,
        output_prefix=output_prefix,
        max_scroll_pages=args.max_scroll_pages,
        batch_size=args.batch_size,
        warmup_time=float(args.warmup_time),
        delay_min=args.delay_min,
        delay_max=args.delay_max,
    )

    print(f"{'=' * 60}")
    print("Coupang /np/omp Seller Crawler v3.0")
    print(f"Time: {ts}")
    print(f"Max scroll pages: {args.max_scroll_pages}")
    print(f"{'=' * 60}")

    control = Control()
    crawler = CoupangCrawler(
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
    print(f"  Brand sellers skipped: {summary.brand_seller_skipped}")
    print(f"  Request errors: {summary.request_errors}")
    print(f"  Power sellers: {summary.power_sellers}")
    print(f"  Termination: {summary.termination_reason}")
    if summary.json_path:
        print(f"  JSON: {summary.json_path}")
    if summary.csv_path:
        print(f"  CSV:  {summary.csv_path}")

    return determine_exit_code(summary)


if __name__ == "__main__":
    raise SystemExit(main())
