"""공용 종료 판정 — worker·UI가 동일한 우선순위를 사용한다."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.foodspring_records import FoodSpringRunSummary


class RunOutcome(Enum):
    SAVE_ERROR = "save_error"
    CANCELLED = "cancelled"
    ERROR = "error"
    NO_RECORDS = "no_records"
    SUCCESS = "success"


def determine_outcome(summary: FoodSpringRunSummary) -> RunOutcome:
    """우선순위: save_error > cancelled > error > no_records > success."""
    if getattr(summary, "save_error", None):
        return RunOutcome.SAVE_ERROR
    if summary.cancelled:
        return RunOutcome.CANCELLED
    if summary.error:
        return RunOutcome.ERROR
    if summary.termination_reason in ("no_items", "no_sellers"):
        return RunOutcome.NO_RECORDS
    if not summary.xlsx_path:
        return RunOutcome.NO_RECORDS
    return RunOutcome.SUCCESS