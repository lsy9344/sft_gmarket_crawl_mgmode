"""PROTOTYPE: 과일 하위 카테고리를 작은 묶음으로 이어서 수집한다."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path

from app.core.base import Control
from app.core.coupang.patchright_sample import (
    MAX_SAMPLE_SCAN_ITEMS,
    run_sample,
)

FRUIT_CATEGORIES = (
    ("194284", "사과/배"),
    ("194288", "귤/한라봉/감귤류"),
    ("194294", "감/홍시/곶감"),
    ("194300", "키위/참다래"),
    ("194306", "토마토/자두/복숭아/포도"),
    ("194315", "수박/메론/참외"),
    ("194320", "딸기/블루베리/베리류"),
    ("194326", "바나나/오렌지/파인애플"),
    ("194331", "자몽/레몬/라임/석류"),
    ("194337", "망고/체리/아보카도/기타"),
    ("194358", "냉동과일/간편과일"),
    ("194368", "과일선물세트"),
)
INITIAL_BATCH_LIMIT = 8
INCREASED_BATCH_LIMIT = 12
MAX_CATEGORY_PAGES = 50
EMPTY_PAGE_TOLERANCE = 2
JOB_FILENAME = "patchright_fruit_job.json"


def _job_path(output_dir: Path) -> Path:
    return output_dir / JOB_FILENAME


def _new_job() -> dict:
    return {
        "version": 1,
        "root_category_id": "194282",
        "category_index": 0,
        "page_number": 1,
        "consecutive_empty_pages": 0,
        "batch_limit": INITIAL_BATCH_LIMIT,
        "successful_batches": 0,
        "products_processed": 0,
        "records_saved": 0,
        "completed_categories": [],
        "status": "running",
    }


def _read_job(output_dir: Path) -> dict:
    path = _job_path(output_dir)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _new_job()
    except (OSError, ValueError) as error:
        raise ValueError(f"과일 수집 기록을 읽지 못했습니다: {error}") from error
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("과일 수집 기록 형식이 잘못됐습니다.")
    integer_fields = (
        "category_index",
        "page_number",
        "consecutive_empty_pages",
        "batch_limit",
        "successful_batches",
        "products_processed",
        "records_saved",
    )
    if any(not isinstance(value.get(field), int) for field in integer_fields):
        raise ValueError("과일 수집 기록 형식이 잘못됐습니다.")
    if not 0 <= value["category_index"] <= len(FRUIT_CATEGORIES):
        raise ValueError("과일 수집 기록의 카테고리 위치가 잘못됐습니다.")
    if not 1 <= value["page_number"] <= MAX_CATEGORY_PAGES + 1:
        raise ValueError("과일 수집 기록의 페이지 위치가 잘못됐습니다.")
    if value["batch_limit"] not in (INITIAL_BATCH_LIMIT, INCREASED_BATCH_LIMIT):
        raise ValueError("과일 수집 기록의 묶음 크기가 잘못됐습니다.")
    completed = value.get("completed_categories")
    if not isinstance(completed, list) or any(
        not isinstance(item, str) for item in completed
    ):
        raise ValueError("과일 수집 기록 형식이 잘못됐습니다.")
    return value


def _save_job(output_dir: Path, job: dict) -> str:
    path = _job_path(output_dir)
    temporary = path.with_name(f"{path.name}.tmp")
    value = dict(job)
    value["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return str(path)


def _finish_category(job: dict, category_id: str) -> None:
    job["completed_categories"] = [
        *job["completed_categories"],
        category_id,
    ]
    job["category_index"] += 1
    job["page_number"] = 1
    job["consecutive_empty_pages"] = 0
    if job["category_index"] == len(FRUIT_CATEGORIES):
        job["status"] = "completed"


def run_fruit_step(
    *,
    output_dir: Path,
    control: Control | None = None,
    on_event: Callable[[dict], None] | None = None,
    state_root: Path | None = None,
    browser_scope_factory: Callable | None = None,
    sample_runner: Callable = run_sample,
) -> dict:
    """한 번에 카테고리 한 페이지의 작은 묶음 하나만 처리한다."""
    job = _read_job(output_dir)
    if job["status"] == "completed" or job["category_index"] == len(
        FRUIT_CATEGORIES
    ):
        return {
            "event": "job_completed",
            "records_saved": job["records_saved"],
            "products_processed": job["products_processed"],
            "completed_category_count": len(job["completed_categories"]),
            "job_path": str(_job_path(output_dir)),
        }

    category_id, category_name = FRUIT_CATEGORIES[job["category_index"]]
    page_number = job["page_number"]
    result = sample_runner(
        category_id=category_id,
        page_number=page_number,
        output_dir=output_dir,
        limit=job["batch_limit"],
        control=control,
        on_event=on_event,
        state_root=state_root,
        browser_scope_factory=browser_scope_factory,
    )
    event = result.get("event")
    if event not in ("sample_completed", "page_exhausted"):
        return {
            **result,
            "fruit_category_name": category_name,
            "job_path": str(_job_path(output_dir)),
        }

    if event == "sample_completed":
        processed = max(0, int(result.get("next_offset", 0)) - int(result["offset"]))
        job["successful_batches"] += 1
        job["products_processed"] += processed
        job["records_saved"] += int(result.get("record_count", 0))
        if job["batch_limit"] == INITIAL_BATCH_LIMIT:
            job["batch_limit"] = INCREASED_BATCH_LIMIT
        if int(result.get("next_offset", 0)) >= MAX_SAMPLE_SCAN_ITEMS:
            job["page_number"] += 1
            job["consecutive_empty_pages"] = 0
    else:
        if int(result.get("offset", 0)) == 0:
            job["consecutive_empty_pages"] += 1
        else:
            job["consecutive_empty_pages"] = 0
        job["page_number"] += 1

    if (
        job["consecutive_empty_pages"] >= EMPTY_PAGE_TOLERANCE
        or job["page_number"] > MAX_CATEGORY_PAGES
    ):
        _finish_category(job, category_id)

    try:
        job_path = _save_job(output_dir, job)
    except OSError as error:
        return {
            **result,
            "event": "failed",
            "error": f"표본은 처리했지만 과일 수집 기록 저장에 실패했습니다: {error}",
        }

    return {
        "event": "fruit_step_completed",
        "sample_event": event,
        "category_id": category_id,
        "fruit_category_name": category_name,
        "page_number": page_number,
        "batch_limit_used": result["limit"],
        "next_batch_limit": job["batch_limit"],
        "sample_offset": result["offset"],
        "sample_next_offset": result.get("next_offset"),
        "record_count": result.get("record_count", 0),
        "json_path": result.get("json_path", ""),
        "csv_path": result.get("csv_path", ""),
        "products_processed": job["products_processed"],
        "records_saved": job["records_saved"],
        "completed_category_count": len(job["completed_categories"]),
        "next_category_index": job["category_index"],
        "next_page_number": job["page_number"],
        "job_status": job["status"],
        "job_path": job_path,
    }
