"""Foodspring QThread 워커: 코어 엔진 ↔ Qt signal 브리지."""

from __future__ import annotations

import traceback

from PyQt6.QtCore import QThread, pyqtSignal

from app.core.base import Control
from app.core.foodspring.engine import FoodSpringCrawler
from app.models.foodspring_records import FoodSpringRunConfig, FoodSpringRunSummary


class FoodSpringWorker(QThread):
    phase_changed = pyqtSignal(str, int, int)
    progress_changed = pyqtSignal(str, int, int)
    log_message = pyqtSignal(str)
    item_collected = pyqtSignal(dict)
    stats_changed = pyqtSignal(object)
    error_occurred = pyqtSignal(str)
    finished_crawl = pyqtSignal(object)

    def __init__(
        self,
        config: FoodSpringRunConfig,
        control: Control,
        fetcher=None,
        exporter=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.control = control
        self._fetcher = fetcher
        self._exporter = exporter
        self.summary: FoodSpringRunSummary | None = None

    def run(self) -> None:
        try:
            crawler = FoodSpringCrawler(
                config=self.config,
                control=self.control,
                fetcher=self._fetcher,
                exporter=self._exporter,
                on_phase=self.phase_changed.emit,
                on_progress=self.progress_changed.emit,
                on_log=self.log_message.emit,
                on_record=self.item_collected.emit,
                on_stats=self.stats_changed.emit,
            )
            summary = crawler.run()
            self.summary = summary
            self.finished_crawl.emit(summary)
        except Exception as e:  # noqa: BLE001
            self.log_message.emit(traceback.format_exc())
            self.error_occurred.emit(str(e))
            fallback = FoodSpringRunSummary(error=str(e), termination_reason="error")
            self.summary = fallback
            self.finished_crawl.emit(fallback)