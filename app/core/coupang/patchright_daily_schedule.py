"""2026-09-03 과일 목록 수집의 보수적인 일일 일정."""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from app.core.coupang.patchright_canary import (
    MAX_DAILY_ITEMS,
    _guard_path,
    _read_guard,
)
from app.core.coupang.patchright_full_fruit import (
    STATE_FILENAME,
    FullFruitStore,
    run_listing_pages,
)

DAILY_STATE_FILENAME = "patchright_daily_schedule_20260903.json"
DAILY_LOG_FILENAME = "patchright_daily_schedule_20260903.jsonl"
LIVE_STAGE_ORDER = ("pages10_a", "pages10_b", "daily_remainder")
DEADLINE_TS = time.mktime((2026, 9, 3, 9, 0, 0, 0, 0, -1))

SCHEDULE_ROWS = (
    ("pages10_a", (2026, 9, 3, 0, 10, 0, 0, 0, -1), 10, 600, "live"),
    ("pages10_b", (2026, 9, 3, 2, 15, 0, 0, 0, -1), 10, 600, "live"),
    ("daily_remainder", (2026, 9, 3, 4, 20, 0, 0, 0, -1), 5, 300, "live"),
    ("audit", (2026, 9, 3, 6, 30, 0, 0, 0, -1), 0, 0, "audit"),
)


def build_schedule() -> list[dict]:
    """하루 1,500개 봉투를 채우는 예약표를 반환한다."""
    return [
        {
            "stage": stage,
            "timestamp": time.mktime(at),
            "scheduled_at": time.strftime("%Y-%m-%d %H:%M:%S", at),
            "page_limit": page_limit,
            "item_budget": item_budget,
            "mode": mode,
        }
        for stage, at, page_limit, item_budget, mode in SCHEDULE_ROWS
    ]


def _atomic_write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def _read_state(output_dir: Path) -> dict:
    path = output_dir / DAILY_STATE_FILENAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "version": 1,
            "date": "2026-09-03",
            "completed_stages": [],
            "halted": False,
        }
    except (OSError, ValueError) as error:
        raise ValueError(f"일일 예약 기록을 읽지 못했습니다: {error}") from error
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or not isinstance(value.get("completed_stages"), list)
        or not isinstance(value.get("halted"), bool)
    ):
        raise ValueError("일일 예약 기록 형식이 잘못됐습니다.")
    return value


def _append_log(output_dir: Path, value: dict) -> None:
    with (output_dir / DAILY_LOG_FILENAME).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def _backup(
    output_dir: Path,
    stage: str,
    state_root: Path | None,
    current: float,
) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(current))
    backup_dir = output_dir / "daily_backups" / f"{stamp}_{stage}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    for path in output_dir.iterdir():
        if path.is_file() and path.name != DAILY_LOG_FILENAME:
            shutil.copy2(path, backup_dir / path.name)
    guard_path = _guard_path(state_root)
    if guard_path.exists():
        shutil.copy2(guard_path, backup_dir / guard_path.name)
    return str(backup_dir)


def _remaining_page_count(guard: dict) -> int:
    used = (
        guard.get("daily_items_reserved", 0)
        if guard.get("daily_date") == "2026-09-03"
        else 0
    )
    if not isinstance(used, int) or used < 0:
        return 0
    return min(10, max(0, (MAX_DAILY_ITEMS - used) // 60))


def _audit(output_dir: Path, state_root: Path | None) -> dict:
    guard = _read_guard(state_root)
    collection_state = json.loads(
        (output_dir / STATE_FILENAME).read_text(encoding="utf-8")
    )
    FullFruitStore(output_dir).validate()
    daily_state = _read_state(output_dir)
    return {
        "event": "daily_audit_completed",
        "blocked": bool(guard.get("blocked")),
        "daily_sessions": guard.get("daily_sessions", 0),
        "daily_items_reserved": guard.get("daily_items_reserved", 0),
        "collection_status": collection_state.get("status"),
        "category_index": collection_state.get("category_index"),
        "page_number": collection_state.get("page_number"),
        "next_offset": collection_state.get("next_offset"),
        "unique_products": collection_state.get("unique_products"),
        "completed_stages": daily_state["completed_stages"],
        "halted": daily_state["halted"],
    }


def run_daily_stage(
    *,
    output_dir: Path,
    stage: str,
    state_root: Path | None = None,
    now: float | None = None,
    runners: dict[str, Callable[[], dict]] | None = None,
) -> dict:
    """예약 한 단계만 실행하고 앞 단계 실패 뒤에는 접속하지 않는다."""
    plan = {item["stage"]: item for item in build_schedule()}
    if stage not in plan:
        raise ValueError(f"알 수 없는 일일 예약 단계입니다: {stage}")
    current = time.time() if now is None else now
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(current))
    state = _read_state(output_dir)

    if stage == "audit":
        result = _audit(output_dir, state_root)
        result["started_at"] = started_at
        _append_log(output_dir, result)
        return result
    if current > DEADLINE_TS:
        result = {
            "event": "daily_stage_skipped",
            "stage": stage,
            "reason": "오전 9시 일일 예약 종료 시각이 지났습니다.",
            "started_at": started_at,
        }
        _append_log(output_dir, result)
        return result
    if current < plan[stage]["timestamp"]:
        result = {
            "event": "daily_stage_skipped",
            "stage": stage,
            "reason": "예약 시각 전입니다.",
            "started_at": started_at,
        }
        _append_log(output_dir, result)
        return result
    if state["halted"]:
        result = {
            "event": "daily_stage_skipped",
            "stage": stage,
            "reason": "앞 단계가 실패하거나 차단되어 건너뜁니다.",
            "started_at": started_at,
        }
        _append_log(output_dir, result)
        return result
    expected_index = len(state["completed_stages"])
    expected = (
        LIVE_STAGE_ORDER[expected_index]
        if expected_index < len(LIVE_STAGE_ORDER)
        else None
    )
    if stage != expected:
        result = {
            "event": "daily_stage_skipped",
            "stage": stage,
            "reason": f"다음 순서는 {expected or '없음'}입니다.",
            "started_at": started_at,
        }
        _append_log(output_dir, result)
        return result

    guard = _read_guard(state_root)
    if guard.get("blocked") or guard.get("recovery_ramp_limit") is not None:
        reason = "차단 또는 복구 확대 기록이 남아 있어 수집을 시작하지 않습니다."
        state.update(halted=True, halted_at=started_at, halt_reason=reason)
        _atomic_write_json(output_dir / DAILY_STATE_FILENAME, state)
        result = {
            "event": "daily_stage_halted",
            "stage": stage,
            "reason": reason,
            "started_at": started_at,
        }
        _append_log(output_dir, result)
        return result

    backup_dir = _backup(output_dir, stage, state_root, current)
    if runners is None:
        if stage in ("pages10_a", "pages10_b"):
            runner = lambda: run_listing_pages(
                output_dir=output_dir, page_count=10, state_root=state_root
            )
        else:
            page_count = _remaining_page_count(guard)
            if page_count:
                runner = lambda: run_listing_pages(
                    output_dir=output_dir,
                    page_count=page_count,
                    state_root=state_root,
                )
            else:
                runner = lambda: {
                    "event": "guard_refused",
                    "reason": "오늘 남은 상품 한도가 60개 미만입니다.",
                }
    else:
        runner = runners[stage]
    try:
        stage_result = runner()
    except Exception as error:  # noqa: BLE001 - 예약 실패를 기록하고 뒤 단계를 막는다
        stage_result = {
            "event": "failed",
            "error": f"{type(error).__name__}: {error}",
        }

    event = stage_result.get("event")
    if event == "listing_pages_completed":
        state["completed_stages"].append(stage)
        state.update(last_completed_at=started_at, last_completed_stage=stage)
        result_event = "daily_stage_completed"
    else:
        reason = str(
            stage_result.get("reason")
            or stage_result.get("error")
            or f"예상하지 않은 실행 결과: {event}"
        )
        state.update(halted=True, halted_at=started_at, halt_reason=reason)
        result_event = "daily_stage_halted"

    _atomic_write_json(output_dir / DAILY_STATE_FILENAME, state)
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
