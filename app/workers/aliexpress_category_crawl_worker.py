"""AliExpress 카테고리 수집 QThread 워커.

AliexpressCategoryCrawler 를 백그라운드 QThread 로 실행하며
UI 스레드에 진행 상태, 실시간 레코드, 통계, 로그를 시그널로 전달한다.
"""

from __future__ import annotations

import traceback

from PyQt6.QtCore import QThread, pyqtSignal

from app.core.base import CancelledError, Control
from app.core.aliexpress_category_crawler import (
    AliexpressCategoryCrawler,
    AliexpressCategoryRunConfig,
    AliexpressCrawlSummary,
)


class AliexpressCategoryCrawlWorker(QThread):
    """AliExpress 카테고리 2단계 수집을 백그라운드에서 실행하는 워커."""

    phase_changed = pyqtSignal(str, int, int)
    progress_changed = pyqtSignal(str, int, int)
    log_message = pyqtSignal(str)
    item_collected = pyqtSignal(dict)
    stats_changed = pyqtSignal(object)
    error_occurred = pyqtSignal(str)
    finished_crawl = pyqtSignal(object)  # AliexpressCrawlSummary | None

    # 별칭 호환성 시그널
    log_emitted = log_message
    record_collected = item_collected
    stats_updated = stats_changed
    crawl_finished = finished_crawl

    def __init__(
        self,
        config: AliexpressCategoryRunConfig,
        control: Control,
        start_fresh: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        if start_fresh and not config.start_fresh:
            import dataclasses
            config = dataclasses.replace(config, start_fresh=True)
        self.config = config
        self.control = control
        self.summary: AliexpressCrawlSummary | None = None

    def run(self) -> None:
        try:
            crawler = AliexpressCategoryCrawler(
                config=self.config,
                control=self.control,
                on_log=self.log_message.emit,
                on_phase=self.phase_changed.emit,
                on_progress=self.progress_changed.emit,
                on_collected=self.item_collected.emit,
                on_stats=self.stats_changed.emit,
                on_error=self.error_occurred.emit,
            )
            summary = crawler.crawl()
            self.summary = summary
            self.finished_crawl.emit(summary)
        except CancelledError:
            self.log_message.emit("[Ali 카테고리] 사용자에 의해 수집이 취소되었습니다.")
            self.finished_crawl.emit(None)
        except Exception as e:
            err_msg = f"{e}\n{traceback.format_exc()}"
            self.log_message.emit(f"[치명적 오류] {err_msg}")
            self.error_occurred.emit(str(e))
            self.finished_crawl.emit(None)
