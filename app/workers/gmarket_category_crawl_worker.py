"""Gmarket 카테고리 수집 QThread 워커: Phase A(리스팅) + Phase B(판매자정보).

Phase A — GmarketCategoryLister: Bright Data Web Unlocker 로 대상 카테고리
(대/중/소) `/n/list` 리스팅을 페이지 순회하며 goodscode 를 모은다(브라우저
불필요, docs/gmarket/ACCESS_ROUTES_RESEARCH_20260908.md §6).
Phase B — 기존 SellerCrawler 재사용: build_crawl_plan 으로 계획을 확정하고
crawl() 로 판매자정보 수집·저장(체크포인트/collected_ids/승격 내구성 전부 기존 규율).

실패/취소 시에도 UI 가 복구되도록 finished_crawl 은 항상 emit 한다
(summary 가 없으면 None — 판매자정보 저장 전에 중단된 경우).
"""

from __future__ import annotations

import traceback

from PyQt6.QtCore import QThread, pyqtSignal

from app.core.base import CancelledError, Control
from app.core.crawler import (
    CrawlSummary,
    SellerCrawler,
    reconcile_leftover_checkpoints,
)
from app.core.gmarket_category_crawler import (
    GmarketCategoryLister,
    GmarketCategoryRunConfig,
    build_result_from_codes,
)
from app.core.plan import build_crawl_plan
from app.core.storage import LoadStatus, Storage
from app.models.records import STATUS_BLOCKED

PHASE_LISTING = "카테고리 리스팅"
PHASE_SELLER = "판매자정보 수집"


class GmarketCategoryCrawlWorker(QThread):
    """카테고리 선택 → goodscode 수집 → 판매자정보 수집·저장을 순차 실행."""

    phase_changed = pyqtSignal(str, int, int)
    progress_changed = pyqtSignal(str, int, int)
    log_message = pyqtSignal(str)
    item_collected = pyqtSignal(dict)
    stats_changed = pyqtSignal(object)
    error_occurred = pyqtSignal(str)
    finished_crawl = pyqtSignal(object)  # CrawlSummary | None

    def __init__(self, config: GmarketCategoryRunConfig, control: Control,
                 parent=None) -> None:
        super().__init__(parent)
        self.config = config
        # Control 은 caller(메인 윈도우) 소유 — 절대 새로 만들지 않는다.
        self.control = control
        # 스레드 종료 후 메인 윈도우가 읽을 수집 요약 결과.
        self.summary: CrawlSummary | None = None

    def run(self) -> None:
        try:
            storage = Storage(self.config.output_dir)

            # ── Phase A: 리스팅(goodscode 수집) ──────────────────────────
            self.phase_changed.emit(PHASE_LISTING, 1, 2)
            self.log_message.emit(
                f"[카테고리] 리스팅 시작 — {len(self.config.targets)}개 카테고리, "
                f"최대 {self.config.max_pages}페이지 (Web Unlocker API 사용)"
            )
            lister = GmarketCategoryLister(
                control=self.control,
                on_log=self.log_message.emit,
                on_progress=self.progress_changed.emit,
                max_pages=self.config.max_pages,
                page_delay_min=self.config.page_delay_min,
                page_delay_max=self.config.page_delay_max,
            )
            outcomes = lister.collect(list(self.config.targets))
            if not outcomes:
                self.log_message.emit("[카테고리] 수집 대상 카테고리가 없습니다.")
                self._finish_with(None)
                return

            # ── 계획 확정 전 안전장치 (기존 Gmarket 탭과 동일 규율) ───────
            ids_status, collected_ids = storage.load_collected_ids_status()
            if ids_status == LoadStatus.QUARANTINE_FAILED:
                raise RuntimeError(
                    "collected_ids.json 이 손상됐고 격리(백업)에도 실패해 원본이 "
                    "위험한 상태로 남아 있어 수집을 시작할 수 없습니다. 저장 경로를 "
                    "직접 확인해 수동으로 백업한 뒤 다시 시도하세요."
                )
            result = reconcile_leftover_checkpoints(storage, collected_ids, self.log_message.emit)
            if result.blocking:
                raise RuntimeError(
                    "체크포인트 문제로 신규 수집을 시작하지 않았습니다. 로그를 확인하고 "
                    "문제를 해결한 뒤 다시 시도하세요."
                )

            results = [
                build_result_from_codes(o.label, o.codes, collected_ids, o.blocked)
                for o in outcomes
            ]
            for r in results:
                if r.status == STATUS_BLOCKED:
                    self.log_message.emit(
                        f"  [{r.category_name}] 리스팅 차단/오류 — 이 카테고리는 건너뜁니다."
                    )
                elif r.total_codes == 0:
                    self.log_message.emit(f"  [{r.category_name}] 상품 없음")
                else:
                    self.log_message.emit(
                        f"  [{r.category_name}] 전체 {r.total_codes}건 / "
                        f"신규 {r.new_codes}건 / 이미수집 {r.already_collected}건"
                    )

            plan = build_crawl_plan(
                results,
                collected_ids,
                max_items=self.config.max_items,
                output_dir=str(self.config.output_dir),
            )
            if plan.total_targets == 0:
                self.log_message.emit("[카테고리] 수집할 신규 대상이 없습니다 "
                                      "(이미 수집됨 또는 상품 없음/차단).")
                self._finish_with(None)
                return
            self.log_message.emit(
                f"[계획 확정] {len(plan.categories)}개 카테고리, "
                f"{plan.total_targets:,}건 (hash={plan.plan_hash})"
            )

            # ── Phase B: 판매자정보 (기존 파이프라인 재사용) ───────────────
            self.phase_changed.emit(PHASE_SELLER, 2, 2)
            crawler = SellerCrawler(
                storage,
                control=self.control,
                on_log=self.log_message.emit,
                on_category=self.progress_changed.emit,
                on_item=self.progress_changed.emit,
                on_collected=self.item_collected.emit,
                on_error=self.error_occurred.emit,
                delay=self.config.phase2_delay,
            )
            summary = crawler.crawl(plan)
            self.summary = summary
            self._emit_stats(summary)
            self.finished_crawl.emit(summary)
        except CancelledError:
            self.log_message.emit("[카테고리] 취소 — 수집을 중단합니다.")
            self._finish_with(None)
        except Exception as e:  # noqa: BLE001 - 워커 최상위 예외 격리
            self.log_message.emit(traceback.format_exc())
            self.error_occurred.emit(f"{type(e).__name__}: {e}")
            self._finish_with(None)

    # ── 내부 ─────────────────────────────────────────────────────────────
    def _emit_stats(self, summary: CrawlSummary) -> None:
        self.stats_changed.emit(summary)

    def _finish_with(self, summary: CrawlSummary | None) -> None:
        self.summary = summary
        self.finished_crawl.emit(summary)
