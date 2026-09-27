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
    PARTIAL = "partial"
    SUCCESS = "success"


def determine_outcome(summary: CoupangRunSummary) -> RunOutcome:
    """우선순위: save_error > cleanup_error > cancelled > no_records > error > partial > success."""
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
    if summary.termination_reason == "empty_category" and summary.json_path:
        return RunOutcome.SUCCESS
    if not summary.records:
        return RunOutcome.NO_RECORDS
    # resume_shifted: 재개 뒤 목록 변동 미확인 / resume_pending: 미확인 판매자
    # 잔존 / mapping_pending: 매핑 누락 vendorItemId 잔존 — 누락 증명이 없으므로
    # '완료'가 아니라 일부 수집으로 표시한다
    # (WORK_ORDER_CATEGORY_RESUME §3.4·§5.6).
    if summary.termination_reason in (
        "page_limit", "resume_shifted", "resume_pending", "mapping_pending",
    ):
        return RunOutcome.PARTIAL
    return RunOutcome.SUCCESS
