"""CLI: 오늘 오전 9시까지 Patchright 확대 검증 한 단계를 실행한다."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.coupang.patchright_automation import run_automation_stage


def main() -> int:
    parser = argparse.ArgumentParser(description="Patchright 예약 확대 검증")
    parser.add_argument(
        "--stage",
        required=True,
        choices=("recovery8", "recovery24", "recovery600", "audit"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.home() / "Desktop" / "PatchrightFruit",
    )
    args = parser.parse_args()
    result = run_automation_stage(output_dir=args.output_dir, stage=args.stage)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True), flush=True)
    return 0 if result["event"] != "automation_halted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
