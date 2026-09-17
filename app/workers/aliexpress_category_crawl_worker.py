"""AliExpress 카테고리 수집 QThread 워커.

AliexpressCategoryCrawler 를 백그라운드 QThread 로 실행하며
UI 스레드에 진행 상태, 실시간 레코드, 통계, 로그를 시그널로 전달한다.

취소·예외가 나도 finished_crawl 로 항상 요약(AliexpressCrawlSummary)을
내보낸다 — UI 가 "대상 없음"과 "실패"를 구분해 안내할 수 있도록.
"""

from __future__ import annotations

import traceback

from PyQt6.QtCore import QThread, pyqtSignal

from app.core.base import CancelledError, Control
from app.core.aliexpress_category_crawler import (
    AliexpressCategoryCrawler,
    AliexpressCategoryRunConfig,
    AliexpressCrawlSummary,
    ali_category_run_dir,
)


def build_summary_for_config(config: AliexpressCategoryRunConfig) -> AliexpressCrawlSummary:
    """설정에서 결과 파일 경로만 채운 요약 — 취소·예외 시의 최소 요약."""
    safe_name = ""
    import re as _re

    safe_name = _re.sub(r'[\\/*?:"<>| ]', "_", config.category_name)
    run_dir = ali_category_run_dir(config.output_dir, config.category_name, config.category_url)
    return AliexpressCrawlSummary(
        csv_file=run_dir / f"ali_category_{safe_name}.csv",
        json_file=run_dir / f"ali_category_{safe_name}.json",
    )


class AliexpressCategoryCrawlWorker(QThread):
    """AliExpress 카테고리 2단계 수집을 백그라운드에서 실행하는 워커."""

    phase_changed = pyqtSignal(str, int, int)
    progress_changed = pyqtSignal(str, int, int)
    log_message = pyqtSignal(str)
    item_collected = pyqtSignal(dict)
    stats_changed = pyqtSignal(object)
    error_occurred = pyqtSignal(str)
    finished_crawl = pyqtSignal(object)  # AliexpressCrawlSummary — 항상 요약을 내보낸다

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
            # 크롤러가 보통 취소 요약으로 승화하지만, 그 전에 끊긴 경우의 안전망
            summary = build_summary_for_config(self.config)
            summary.cancelled = True
            summary.termination_reason = "cancelled"
            self.summary = summary
            self.log_message.emit("[Ali 카테고리] 사용자에 의해 수집이 취소되었습니다.")
            self.finished_crawl.emit(summary)
        except Exception as e:
            err_msg = f"{e}\n{traceback.format_exc()}"
            self.log_message.emit(f"[치명적 오류] {err_msg}")
            self.error_occurred.emit(str(e))
            summary = build_summary_for_config(self.config)
            summary.termination_reason = "error"
            summary.error = str(e)
            self.summary = summary
            self.finished_crawl.emit(summary)
