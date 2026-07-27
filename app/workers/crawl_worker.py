"""Phase 1+2 수집 워커: QThread ↔ Qt 비의존 엔진 브리지 (WORK_ORDER §7.2).

CrawlWorker 는 백그라운드 스레드에서 SellerCrawler.crawl 을 실행하고,
엔진 콜백을 pyqtSignal 로 UI 스레드에 중계한다. Control 은 메인 윈도우가
생성/소유하여 UI 스레드에서 취소/일시정지를 제어하므로 여기서 만들지 않는다.
수집 결과(CrawlSummary)는 self.summary 에 저장하여 스레드 종료 후 메인
윈도우가 파일 경로/취소 여부를 읽을 수 있게 한다.

주의: QThread 는 이미 내장 finished 시그널을 정의하므로, 완료 통지 시그널은
finished_crawl 로 명명하여 finished 를 가리지 않는다.
"""

from __future__ import annotations

import traceback

from PyQt6.QtCore import QThread, pyqtSignal

from app.core.base import Control
from app.core.crawler import CrawlSummary, SellerCrawler
from app.core.plan import CrawlPlan
from app.core.storage import Storage


class CrawlWorker(QThread):
    """Phase 2 판매자정보 수집을 백그라운드로 실행하는 QThread 워커.

    확정된 CrawlPlan(카테고리별 target_codes 가 이미 max_items 캡까지 고정된
    불변 계획)을 그대로 실행한다 — 실행 도중 collected_ids.json 이 바뀌어도
    이 워커가 수집하는 대상 자체는 흔들리지 않는다.
    """

    # (category_name, current, total)
    progress_category = pyqtSignal(str, int, int)
    # (goodscode, current, total)
    progress_item = pyqtSignal(str, int, int)
    log_message = pyqtSignal(str)
    item_collected = pyqtSignal(dict)
    error_occurred = pyqtSignal(str)
    # (total_success, total_failed) — QThread.finished 를 가리지 않도록 별도 명명
    finished_crawl = pyqtSignal(int, int)

    def __init__(
        self,
        storage: Storage,
        plan: CrawlPlan,
        control: Control,
        delay: float,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.storage = storage
        self.plan = plan
        # Control 은 caller(메인 윈도우) 소유 — 절대 새로 만들지 않는다.
        self.control = control
        self.delay = delay
        # 스레드 종료 후 메인 윈도우가 읽을 수집 요약 결과.
        self.summary: CrawlSummary | None = None

    def run(self) -> None:
        try:
            crawler = SellerCrawler(
                self.storage,
                control=self.control,
                on_log=self.log_message.emit,
                on_category=self.progress_category.emit,
                on_item=self.progress_item.emit,
                on_collected=self.item_collected.emit,
                on_error=self.error_occurred.emit,
                delay=self.delay,
            )
            summary = crawler.crawl(self.plan)
            self.summary = summary
            self.finished_crawl.emit(summary.total_success, summary.total_failed)
        except Exception as e:  # noqa: BLE001 - 워커 경계에서 모든 예외 포착
            self.log_message.emit(traceback.format_exc())
            self.error_occurred.emit(str(e))
            # summary 를 만들지 못한 경우에도 UI 가 복구되도록 종료 통지.
            if self.summary is None:
                self.finished_crawl.emit(0, 0)
