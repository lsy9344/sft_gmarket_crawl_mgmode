"""PROTOTYPE CLI: Patchright 과일 카테고리를 한 묶음씩 이어서 수집한다."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.coupang.patchright_fruit import run_fruit_step  # noqa: E402

EXIT_CODES = {
    "fruit_step_completed": 0,
    "job_completed": 0,
    "blocked": 20,
    "guard_refused": 21,
    "cancelled": 22,
    "failed": 1,
}


def _print_state(state: dict) -> None:
    visible = dict(state)
    if "records" in visible:
        visible["records"] = f"{len(visible['records'])} record(s) saved"
    print(json.dumps(visible, ensure_ascii=True, sort_keys=True), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Patchright 과일 12개 하위 카테고리 순차 수집"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.home() / "Desktop" / "PatchrightFruit",
    )
    args = parser.parse_args()
    try:
        result = run_fruit_step(output_dir=args.output_dir, on_event=_print_state)
    except ValueError as error:
        parser.error(str(error))
    _print_state(result)
    return EXIT_CODES.get(result["event"], 1)


if __name__ == "__main__":
    raise SystemExit(main())
