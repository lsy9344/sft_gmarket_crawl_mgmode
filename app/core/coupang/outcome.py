"""공용 종료 판정 — CLI·worker·UI가 동일한 우선순위를 사용한다."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.coupang_records import CoupangRunSummary


class RunOutcome(Enum):
    SAVE_ERROR = "save_error"
    CLEANUP_ERROR = "cleanup_error"
    CANCELLED = "cancelled"
    ERROR = "error"
    NO_RECORDS = "no_records"
    SUCCESS = "success"


def determine_outcome(summary: CoupangRunSummary) -> RunOutcome:
    """우선순위: save_error > cleanup_error > cancelled > no_records > error > success."""
    if getattr(summary, "save_error", None):
        return RunOutcome.SAVE_ERROR
    if getattr(summary, "cleanup_error", None):
        return RunOutcome.CLEANUP_ERROR
    if summary.cancelled:
        return RunOutcome.CANCELLED
    if summary.termination_reason in ("no_items", "template_not_captured"):
        return RunOutcome.NO_RECORDS
    if summary.error:
        return RunOutcome.ERROR
    if not summary.records:
        return RunOutcome.NO_RECORDS
    return RunOutcome.SUCCESS
