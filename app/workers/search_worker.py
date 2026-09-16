"""Coupang 검색 QThread 워커: 검색 코어 엔진 ↔ Qt signal 브리지."""

from __future__ import annotations

import traceback

from PyQt6.QtCore import QThread, pyqtSignal

from app.core.base import Control
from app.core.coupang.decodo_run import run_category_attempts
from app.core.coupang.search_crawler import SearchCrawler, SearchRunConfig
from app.core.decodo import DecodoSettings


class SearchWorker(QThread):
    phase_changed = pyqtSignal(str, int, int)
    progress_changed = pyqtSignal(str, int, int)
    log_message = pyqtSignal(str)
    item_collected = pyqtSignal(dict)
    stats_changed = pyqtSignal(object)
    error_occurred = pyqtSignal(str)
    finished_crawl = pyqtSignal(object)
    attempt_started = pyqtSignal(int, int)

    def __init__(
        self,
        config: SearchRunConfig,
        control: Control,
        decodo_settings: DecodoSettings,
        browser_factory=None,
        parent=None,
        start_fresh: bool = False,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.control = control
        self._browser_factory = browser_factory
        self.decodo_settings = decodo_settings
        self.start_fresh = start_fresh
        self.summary = None

    def run(self) -> None:
        try:
            def factory(cfg):
                return SearchCrawler(
                    config=cfg,
                    control=self.control,
                    browser_factory=self._browser_factory,
                    on_phase=self.phase_changed.emit,
                    on_progress=self.progress_changed.emit,
                    on_log=self.log_message.emit,
                    on_record=self.item_collected.emit,
                    on_stats=self.stats_changed.emit,
                )

            summary = run_category_attempts(
                self.config,
                self.decodo_settings,
                self.control,
                crawler_factory=factory,
                on_log=self.log_message.emit,
                on_attempt_start=self.attempt_started.emit,
                start_fresh=self.start_fresh,
            )
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
        except Exception as e:  # noqa: BLE001 - 워커 최상위 예외 격리
            self.log_message.emit(traceback.format_exc())
            self.error_occurred.emit(f"{type(e).__name__}: {e}")
