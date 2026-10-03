"""예약 1회마다 판매자 사업자정보를 우선 처리하고, 남으면 목록을 이어간다.

사용자 결정(2026-10-01): 축산 목록은 이미 확인한 만큼으로 중단하고 완성형
판매자 데이터셋을 먼저 만든다. 그래서 이 파이프라인은 판매자 대기열이
남아 있으면 항상 판매자 단계를 돌고, 판매자가 끝난 뒤에만 목록 단계를
다시 확인한다. 둘 다 끝나면 완성형 파일을 만들고 종료 코드 30으로
실행기가 남은 예약을 자동으로 끄게 한다.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.coupang.patchright_top_sellers import (
    _work,
    build_seller_final,
    read_seller_control,
)

PIPELINE_COMPLETE = 30


def choose_action(output_dir: Path) -> tuple[str, str]:
    """네트워크 요청 없이 다음 예약 작업을 고른다. 판매자가 항상 우선이다."""
    store = _build_store(output_dir)
    store.ensure_files()

    control = read_seller_control(output_dir)
    if control["status"] in ("halted", "in_progress"):
        reason = str(control.get("reason") or "앞선 판매자 작업이 끝나지 않았습니다.")
        return "halted", reason

    pending, unmapped = _work(store, 1)
    if pending or unmapped:
        return "sellers", "남은 상품의 판매자 사업자정보를 수집합니다."

    failed_mappings = json.loads(
        store.failed_mappings_path.read_text(encoding="utf-8")
    )
    if failed_mappings:
        return "halted", "판매자를 연결하지 못한 상품이 있어 자동 진행을 멈춥니다."

    from app.core.coupang.patchright_top_thousand import read_state

    state = read_state(output_dir)
    if state["status"] == "running":
        return "category", "이어서 상품 목록을 확인합니다."
    return "complete", "상품과 판매자 데이터 수집이 모두 끝났습니다."


def _build_store(output_dir: Path):
    from app.core.coupang.patchright_top_sellers import TopSellerStore

    return TopSellerStore(output_dir)


def _last_json(text: str) -> dict:
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _append_log(output_dir: Path, value: dict) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "top_pipeline_runs.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def _run(command: list[str]) -> tuple[int, dict]:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.stdout:
        print(completed.stdout, end="", flush=True)
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr, flush=True)
    return completed.returncode, _last_json(completed.stdout)


def build_all_finals(output_dir: Path) -> dict:
    """완성형 파일 전부(판매자·상품)를 만든다."""
    from app.core.coupang.patchright_top_thousand import (
        FINAL_FAMILY_CATEGORIES,
        TopThousandStore,
        build_combined_final_dataset,
        build_final_dataset,
    )

    finals = {"sellers": str(build_seller_final(output_dir))}
    store = TopThousandStore(output_dir)
    for root in FINAL_FAMILY_CATEGORIES:
        finals[root] = str(build_final_dataset(store, root)[0])
    finals["all"] = str(build_combined_final_dataset(store)[0])
    return finals


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Patchright 판매자 우선 상위 데이터셋 예약 파이프라인"
    )
    parser.add_argument("--seller-limit", type=int, default=130)
    parser.add_argument("--listing-pages", type=int, default=8)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.home() / "Desktop" / "PatchrightTop1000",
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        required=True,
        help="목록과 판매자 단계가 함께 쓰는 안전 기록 폴더",
    )
    parser.add_argument("--status-only", action="store_true")
    args = parser.parse_args()

    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        action, reason = choose_action(args.output_dir)
    except Exception as error:  # noqa: BLE001 - 예약 경계에서 안전하게 중단
        action, reason = "halted", f"{type(error).__name__}: {error}"

    status = {"event": "pipeline_status", "action": action, "reason": reason}
    print(json.dumps(status, ensure_ascii=True), flush=True)
    if args.status_only:
        return 0

    if action == "complete":
        finals = build_all_finals(args.output_dir)
        result = {**status, "event": "pipeline_complete", "finals": finals}
        _append_log(args.output_dir, {**result, "started_at": started_at})
        print(json.dumps(result, ensure_ascii=True), flush=True)
        return PIPELINE_COMPLETE
    if action == "halted":
        result = {**status, "event": "pipeline_halted"}
        _append_log(args.output_dir, {**result, "started_at": started_at})
        print(json.dumps(result, ensure_ascii=True), flush=True)
        return 23

    if action == "sellers":
        script = Path(__file__).with_name("coupang_patchright_top_sellers.py")
        command = [
            sys.executable,
            str(script),
            "--limit",
            str(args.seller_limit),
            "--output-dir",
            str(args.output_dir),
            "--state-root",
            str(args.state_root),
            "--profile-root",
            str(args.state_root),
        ]
    else:
        script = Path(__file__).with_name("coupang_patchright_top_thousand.py")
        command = [
            sys.executable,
            str(script),
            "--pages",
            str(args.listing_pages),
            "--output-dir",
            str(args.output_dir),
            "--state-root",
            str(args.state_root),
        ]

    exit_code, child_result = _run(command)
    result = {
        "event": "pipeline_slot_finished" if exit_code == 0 else "pipeline_slot_failed",
        "action": action,
        "exit_code": exit_code,
        "child_event": child_result.get("event", ""),
        "started_at": started_at,
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _append_log(args.output_dir, result)
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
