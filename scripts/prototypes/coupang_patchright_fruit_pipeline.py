"""예약 1회마다 판매자 데이터 또는 다음 과일 카테고리 하나를 처리한다."""

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

from app.core.coupang.patchright_full_fruit import FullFruitStore, _read_state
from app.core.coupang.patchright_full_sellers import (
    _read_control,
    _work,
    authorize_http_503_retry,
)

PIPELINE_COMPLETE = 30


def choose_action(output_dir: Path) -> tuple[str, str]:
    """네트워크 요청 없이 다음 예약 작업을 고른다."""
    store = FullFruitStore(output_dir)
    store.ensure_files()
    store.validate()

    control = _read_control(output_dir)
    if control["status"] in ("halted", "in_progress"):
        reason = str(control.get("reason") or "앞선 판매자 작업이 끝나지 않았습니다.")
        return "halted", reason

    pending, unmapped = _work(store, 1)
    if pending or unmapped:
        return "sellers", "남은 상품의 판매자 정보를 수집합니다."

    failed_mappings = json.loads(
        store.failed_mappings_path.read_text(encoding="utf-8")
    )
    if failed_mappings:
        return "halted", "판매자를 연결하지 못한 상품이 있어 자동 진행을 멈춥니다."

    state = _read_state(output_dir)
    if state["status"] == "incomplete_limit_reached":
        return "halted", "카테고리 페이지 상한에 도달해 자동 진행을 멈춥니다."
    if state["phase"] == "products" and state["status"] == "running":
        return "category", "다음 과일 카테고리 상품을 수집합니다."
    if state["phase"] in ("sellers", "complete") or state["status"] == "completed":
        return "complete", "모든 상품과 판매자 데이터 수집이 끝났습니다."
    return "halted", "수집 상태를 판단할 수 없어 자동 진행을 멈춥니다."


def choose_scheduled_action(
    output_dir: Path, *, now: float | None = None
) -> tuple[str, str]:
    """503 중단은 3시간 뒤 한 번만 열고 다음 예약 작업을 고른다."""
    control = _read_control(output_dir)
    if control["status"] == "halted" and "HTTP 503" in str(
        control.get("reason") or ""
    ):
        allowed, reason = authorize_http_503_retry(output_dir, now=now)
        if not allowed:
            return "halted", reason
    return choose_action(output_dir)


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
    with (output_dir / "fruit_pipeline_runs.jsonl").open(
        "a", encoding="utf-8"
    ) as handle:
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Patchright 과일 목록·판매자 데이터 예약 파이프라인"
    )
    parser.add_argument("--seller-limit", type=int, default=130)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.home() / "Desktop" / "PatchrightFruit",
    )
    parser.add_argument(
        "--state-root",
        "--seller-state-root",
        dest="state_root",
        type=Path,
        required=True,
        help="카테고리와 판매자 단계가 함께 쓰는 안전 기록 폴더",
    )
    parser.add_argument("--profile-root", type=Path, required=True)
    parser.add_argument("--status-only", action="store_true")
    args = parser.parse_args()

    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        action, reason = choose_scheduled_action(args.output_dir)
    except Exception as error:  # noqa: BLE001 - 예약 경계에서 안전하게 중단
        action, reason = "halted", f"{type(error).__name__}: {error}"

    status = {"event": "pipeline_status", "action": action, "reason": reason}
    print(json.dumps(status, ensure_ascii=True), flush=True)
    if args.status_only:
        return 0

    if action == "complete":
        result = {**status, "event": "pipeline_complete"}
        _append_log(args.output_dir, {**result, "started_at": started_at})
        print(json.dumps(result, ensure_ascii=True), flush=True)
        return PIPELINE_COMPLETE
    if action == "halted":
        result = {**status, "event": "pipeline_halted"}
        _append_log(args.output_dir, {**result, "started_at": started_at})
        print(json.dumps(result, ensure_ascii=True), flush=True)
        return 23

    if action == "sellers":
        script = Path(__file__).with_name("coupang_patchright_full_sellers.py")
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
            str(args.profile_root),
        ]
    else:
        script = Path(__file__).with_name("coupang_patchright_full_fruit.py")
        command = [
            sys.executable,
            str(script),
            "--pages",
            "8",
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
