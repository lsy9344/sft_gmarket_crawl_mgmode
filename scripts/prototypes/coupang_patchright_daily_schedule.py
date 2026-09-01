"""CLI: 2026-09-03 Patchright 일일 수집 예약 한 단계를 실행한다."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.coupang.patchright_daily_schedule import run_daily_stage


def main() -> int:
    parser = argparse.ArgumentParser(description="Patchright 일일 수집 예약")
    parser.add_argument(
        "--stage",
        required=True,
        choices=("pages10_a", "pages10_b", "daily_remainder", "audit"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.home() / "Desktop" / "PatchrightFruit",
    )
    args = parser.parse_args()
    result = run_daily_stage(output_dir=args.output_dir, stage=args.stage)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True), flush=True)
    return 1 if result["event"] == "daily_stage_halted" else 0


if __name__ == "__main__":
    raise SystemExit(main())
