"""Coupang QThread 워커: 코어 엔진 ↔ Qt signal 브리지 (WORK_ORDER §9)."""

from __future__ import annotations

import traceback

from PyQt6.QtCore import QThread, pyqtSignal

from app.core.base import Control
from app.core.coupang.crawler import CoupangCrawler
from app.models.coupang_records import CoupangRunConfig, CoupangRunSummary


class CoupangWorker(QThread):
    phase_changed = pyqtSignal(str, int, int)
    progress_changed = pyqtSignal(str, int, int)
    log_message = pyqtSignal(str)
    item_collected = pyqtSignal(dict)
    stats_changed = pyqtSignal(object)
    error_occurred = pyqtSignal(str)
    finished_crawl = pyqtSignal(object)

    def __init__(
        self,
        config: CoupangRunConfig,
        control: Control,
        browser_factory=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.control = control
        self._browser_factory = browser_factory
        self.summary: CoupangRunSummary | None = None

    def run(self) -> None:
        try:
            crawler = CoupangCrawler(
                config=self.config,
                control=self.control,
                browser_factory=self._browser_factory,
                on_phase=self.phase_changed.emit,
                on_progress=self.progress_changed.emit,
                on_log=self.log_message.emit,
                on_record=self.item_collected.emit,
                on_stats=self.stats_changed.emit,
            )
            summary = crawler.run()
            self.summary = summary
            from app.core.coupang.outcome import RunOutcome, determine_outcome
            outcome = determine_outcome(summary)
            if outcome == RunOutcome.SAVE_ERROR:
                self.error_occurred.emit(summary.save_error or summary.error or "결과 저장 실패")
            elif outcome == RunOutcome.CLEANUP_ERROR:
                self.error_occurred.emit(f"브라우저 종료 실패: {summary.cleanup_error}")
            elif outcome == RunOutcome.ERROR:
                self.error_occurred.emit(summary.error)
            self.finished_crawl.emit(summary)
        except Exception as e:  # noqa: BLE001
            self.log_message.emit(traceback.format_exc())
            self.error_occurred.emit(str(e))
            fallback = CoupangRunSummary(error=str(e), termination_reason="error")
            self.summary = fallback
            self.finished_crawl.emit(fallback)
