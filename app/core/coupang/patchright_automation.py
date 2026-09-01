"""오늘 오전 9시까지 Patchright 확대 검증을 순서대로 실행한다."""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from app.core.coupang.patchright_canary import (
    _guard_path,
    _read_guard,
    advance_recovery_ramp,
    authorize_recovery_resume,
)
from app.core.coupang.patchright_full_fruit import (
    STATE_FILENAME,
    FullFruitStore,
    run_listing_pages,
)

AUTOMATION_STATE_FILENAME = "patchright_automation_state.json"
AUTOMATION_LOG_FILENAME = "patchright_automation_log.jsonl"
AUTOMATION_DEADLINE = "2026-09-02 09:00:00"
AUTOMATION_DEADLINE_TS = time.mktime((2026, 9, 2, 9, 0, 0, 0, 0, -1))
LIVE_STAGE_ORDER = ("page1", "pages3", "pages10")
STAGE_LIMITS = {"page1": 60, "pages3": 180, "pages10": 600}
SUCCESS_EVENTS = {
    "page1": {"listing_pages_completed"},
    "pages3": {"listing_pages_completed"},
    "pages10": {"listing_pages_completed"},
}


def _atomic_write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def _read_automation_state(output_dir: Path) -> dict:
    path = output_dir / AUTOMATION_STATE_FILENAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "version": 1,
            "deadline": AUTOMATION_DEADLINE,
            "completed_stages": [],
            "halted": False,
        }
    except (OSError, ValueError) as error:
        raise ValueError(f"자동 실행 기록을 읽지 못했습니다: {error}") from error
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or not isinstance(value.get("completed_stages"), list)
        or not isinstance(value.get("halted"), bool)
    ):
        raise ValueError("자동 실행 기록 형식이 잘못됐습니다.")
    return value


def _append_log(output_dir: Path, value: dict) -> None:
    path = output_dir / AUTOMATION_LOG_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def _backup_before_stage(
    output_dir: Path,
    stage: str,
    state_root: Path | None,
    current: float,
) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(current))
    backup_dir = output_dir / "automation_backups" / f"{stamp}_{stage}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    for path in output_dir.iterdir():
        if path.is_file() and path.name != AUTOMATION_LOG_FILENAME:
            shutil.copy2(path, backup_dir / path.name)
    guard_path = _guard_path(state_root)
    if guard_path.exists():
        shutil.copy2(guard_path, backup_dir / guard_path.name)
    return str(backup_dir)


def _audit(output_dir: Path, state_root: Path | None) -> dict:
    guard = _read_guard(state_root)
    collection_state_path = output_dir / STATE_FILENAME
    collection_state = json.loads(collection_state_path.read_text("utf-8"))
    store = FullFruitStore(output_dir)
    store.validate()
    automation_state = _read_automation_state(output_dir)
    return {
        "event": "automation_audit_completed",
        "blocked": bool(guard.get("blocked")),
        "recovery_hold": bool(guard.get("recovery_hold")),
        "recovery_ramp_limit": guard.get("recovery_ramp_limit"),
        "daily_sessions": guard.get("daily_sessions", 0),
        "daily_items_reserved": guard.get("daily_items_reserved", 0),
        "collection_status": collection_state.get("status"),
        "category_index": collection_state.get("category_index"),
        "page_number": collection_state.get("page_number"),
        "next_offset": collection_state.get("next_offset"),
        "unique_products": collection_state.get("unique_products"),
        "completed_stages": automation_state["completed_stages"],
        "automation_halted": automation_state["halted"],
    }


def run_automation_stage(
    *,
    output_dir: Path,
    stage: str,
    state_root: Path | None = None,
    now: float | None = None,
    runners: dict[str, Callable[[], dict]] | None = None,
) -> dict:
    """예약된 한 단계만 실행하며 앞 단계 실패 시 브라우저를 열지 않는다."""
    if stage not in (*LIVE_STAGE_ORDER, "audit"):
        raise ValueError(f"알 수 없는 자동 실행 단계입니다: {stage}")
    current = time.time() if now is None else now
    output_dir.mkdir(parents=True, exist_ok=True)
    state = _read_automation_state(output_dir)
    started_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(current))

    if stage == "audit":
        result = _audit(output_dir, state_root)
        result["started_at"] = started_at
        _append_log(output_dir, result)
        return result

    if current > AUTOMATION_DEADLINE_TS:
        result = {
            "event": "automation_skipped",
            "stage": stage,
            "reason": "오늘 오전 9시 자동 실행 종료 시각이 지났습니다.",
            "started_at": started_at,
        }
        _append_log(output_dir, result)
        return result
    if state["halted"]:
        result = {
            "event": "automation_skipped",
            "stage": stage,
            "reason": "앞 단계가 실패하거나 차단되어 건너뜁니다.",
            "started_at": started_at,
        }
        _append_log(output_dir, result)
        return result
    expected_index = len(state["completed_stages"])
    expected_stage = (
        LIVE_STAGE_ORDER[expected_index]
        if expected_index < len(LIVE_STAGE_ORDER)
        else None
    )
    if stage != expected_stage:
        result = {
            "event": "automation_skipped",
            "stage": stage,
            "reason": f"다음 순서는 {expected_stage or '없음'}입니다.",
            "started_at": started_at,
        }
        _append_log(output_dir, result)
        return result

    backup_dir = _backup_before_stage(output_dir, stage, state_root, current)
    if stage == "page1":
        allowed, reason = authorize_recovery_resume(state_root, now=current)
        if not allowed:
            state.update(
                halted=True,
                halted_at=started_at,
                halt_reason=reason,
            )
            _atomic_write_json(output_dir / AUTOMATION_STATE_FILENAME, state)
            result = {
                "event": "automation_halted",
                "stage": stage,
                "reason": reason,
                "started_at": started_at,
            }
            _append_log(output_dir, result)
            return result
    if runners is None:
        runners = {
            "page1": lambda: run_listing_pages(
                output_dir=output_dir, page_count=1, state_root=state_root
            ),
            "pages3": lambda: run_listing_pages(
                output_dir=output_dir, page_count=3, state_root=state_root
            ),
            "pages10": lambda: run_listing_pages(
                output_dir=output_dir, page_count=10, state_root=state_root
            ),
        }
    try:
        stage_result = runners[stage]()
    except Exception as error:  # noqa: BLE001 - 예약 작업은 실패를 기록하고 중단한다
        stage_result = {
            "event": "failed",
            "error": f"{type(error).__name__}: {error}",
        }

    event = stage_result.get("event")
    if event in SUCCESS_EVENTS[stage]:
        advanced, reason = advance_recovery_ramp(
            STAGE_LIMITS[stage], state_root, now=current
        )
        if advanced:
            state["completed_stages"].append(stage)
            state.update(
                last_completed_at=started_at,
                last_completed_stage=stage,
            )
            result_event = "automation_stage_completed"
        else:
            state.update(
                halted=True,
                halted_at=started_at,
                halt_reason=reason,
            )
            result_event = "automation_halted"
    else:
        reason = str(
            stage_result.get("reason")
            or stage_result.get("error")
            or f"예상하지 않은 실행 결과: {event}"
        )
        state.update(
            halted=True,
            halted_at=started_at,
            halt_reason=reason,
        )
        result_event = "automation_halted"

    _atomic_write_json(output_dir / AUTOMATION_STATE_FILENAME, state)
    result = {
        "event": result_event,
        "stage": stage,
        "stage_event": event,
        "started_at": started_at,
        "backup_dir": backup_dir,
        "completed_stages": state["completed_stages"],
        "halted": state["halted"],
    }
    if state["halted"]:
        result["reason"] = state.get("halt_reason", "")
    for key in (
        "completed_page_attempts",
        "total_product_count",
        "total_products_added",
        "unique_products",
        "home_status",
        "category_status",
        "reference",
    ):
        if key in stage_result:
            result[key] = stage_result[key]
    _append_log(output_dir, result)
    return result
