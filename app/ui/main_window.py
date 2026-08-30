"""메인 윈도우: Gmarket/Coupang 2탭 구성, 전역 단일-워커 조정 (WORK_ORDER §10.1)."""

from __future__ import annotations

import os

from PyQt6.QtWidgets import (
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.core.base import Control
from app.core.crawler import reconcile_leftover_checkpoints
from app.core.plan import build_crawl_plan
from app.core.storage import LoadStatus, Storage
from app.models.records import PrescanResult
from app.ui.coupang_panel import CoupangPanel
from app.ui.foodspring_panel import FoodSpringPanel
from app.ui.widgets.log_panel import LogPanel
from app.ui.widgets.prescan_table import PrescanTable
from app.ui.widgets.progress_panel import ProgressPanel
from app.ui.widgets.result_table import ResultTable
from app.ui.widgets.settings_panel import SettingsPanel
from app.workers.coupang_worker import CoupangWorker
from app.workers.crawl_worker import CrawlWorker
from app.workers.foodspring_worker import FoodSpringWorker
from app.workers.prescan_worker import PrescanWorker


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("판매자 정보 수집기 v3.0")
        self.resize(1100, 900)

        # Gmarket 상태
        self.prescan_results: list[PrescanResult] = []
        self.control: Control | None = None
        self.prescan_worker: PrescanWorker | None = None
        self.crawl_worker: CrawlWorker | None = None
        self._success = 0
        self._processed = 0
        self._auto_start_after_prescan = False
        self._prescan_snapshot: dict | None = None
        self._closing = False
        self._close_prompt_active = False

        # Coupang 상태
        self.coupang_control: Control | None = None
        self.coupang_worker: CoupangWorker | None = None

        # Foodspring 상태
        self.foodspring_control: Control | None = None
        self.foodspring_worker: FoodSpringWorker | None = None

        self._build_ui()
        self._set_ui_state("idle")

    # ── UI 구성 ────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self.tab_widget = QTabWidget()

        # Gmarket 탭
        gmarket_tab = QWidget()
        gmarket_layout = QVBoxLayout(gmarket_tab)

        self.settings = SettingsPanel()
        gmarket_layout.addWidget(self.settings)

        btn_row = QHBoxLayout()
        self.btn_prescan = QPushButton("사전 조사")
        self.btn_start = QPushButton("수집 시작")
        self.btn_pause = QPushButton("일시정지")
        self.btn_cancel = QPushButton("취소")
        self.btn_resume_run = QPushButton("이어서 수집")
        self.btn_reset = QPushButton("초기화")
        for b in (
            self.btn_prescan,
            self.btn_start,
            self.btn_pause,
            self.btn_cancel,
            self.btn_resume_run,
            self.btn_reset,
        ):
            btn_row.addWidget(b)
        gmarket_layout.addLayout(btn_row)

        self.btn_prescan.clicked.connect(self.on_prescan)
        self.btn_start.clicked.connect(self.on_start)
        self.btn_pause.clicked.connect(self.on_pause_toggle)
        self.btn_cancel.clicked.connect(self.on_cancel)
        self.btn_resume_run.clicked.connect(self.on_resume_run)
        self.btn_reset.clicked.connect(self.on_reset)

        self.prescan_table = PrescanTable()
        self.progress = ProgressPanel()
        self.log = LogPanel()
        self.result_table = ResultTable()

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.addWidget(self.prescan_table)
        body_layout.addWidget(self.progress)
        body_layout.addWidget(self.log)
        body_layout.addWidget(self.result_table)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        gmarket_layout.addWidget(scroll, stretch=1)

        self.tab_widget.addTab(gmarket_tab, "Gmarket")

        # Coupang 탭
        self.coupang_panel = CoupangPanel()
        self.tab_widget.addTab(self.coupang_panel, "Coupang")

        # Foodspring 탭
        self.foodspring_panel = FoodSpringPanel()
        self.tab_widget.addTab(self.foodspring_panel, "Foodspring")

        # Coupang 버튼 연결
        self.coupang_panel.btn_start.clicked.connect(self.on_coupang_start)
        self.coupang_panel.btn_pause.clicked.connect(self.on_coupang_pause)
        self.coupang_panel.btn_resume.clicked.connect(self.on_coupang_resume)
        self.coupang_panel.btn_cancel.clicked.connect(self.on_coupang_cancel)
        self.coupang_panel.btn_open_result.clicked.connect(self._on_coupang_open_result)

        # Foodspring 버튼 연결
        self.foodspring_panel.btn_start.clicked.connect(self.on_foodspring_start)
        self.foodspring_panel.btn_pause.clicked.connect(self.on_foodspring_pause)
        self.foodspring_panel.btn_resume.clicked.connect(self.on_foodspring_resume)
        self.foodspring_panel.btn_cancel.clicked.connect(self.on_foodspring_cancel)
        self.foodspring_panel.btn_open_result.clicked.connect(self._on_foodspring_open_result)

        self.setCentralWidget(self.tab_widget)
        self._show_status("준비됨")

    def _show_status(self, message: str) -> None:
        """Qt stubs allow a missing status bar; update it only when available."""
        status_bar = self.statusBar()
        if status_bar is not None:
            status_bar.showMessage(message)

    # ── Gmarket 상태 전이 ─────────────────────────────────────────
    def _set_ui_state(self, state: str) -> None:
        idle = state == "idle"
        prescanning = state == "prescanning"
        prescanned = state == "prescanned"
        crawling = state == "crawling"

        self.settings.set_controls_enabled(idle or prescanned)
        self.btn_prescan.setEnabled(idle or prescanned)
        has_new = bool(self.prescan_results) and self.prescan_table.total_new() > 0
        self.btn_start.setEnabled(prescanned and has_new)
        self.btn_pause.setEnabled(crawling)
        self.btn_cancel.setEnabled(prescanning or crawling)
        self.btn_resume_run.setEnabled(idle or prescanned)
        self.btn_reset.setEnabled(idle or prescanned)
        if not crawling:
            self.btn_pause.setText("일시정지")

        # Cross-tab: Gmarket 실행 중이면 Coupang/Foodspring 차단
        gmarket_busy = state in ("prescanning", "crawling")
        self.coupang_panel.set_external_busy(gmarket_busy)
        self.foodspring_panel.set_external_busy(gmarket_busy)

    # ── 전역 단일 워커 ─────────────────────────────────────────────
    def _active_worker(self):
        for worker in (self.crawl_worker, self.prescan_worker, self.coupang_worker, self.foodspring_worker):
            if worker is not None and worker.isRunning():
                return worker
        return None

    def _active_control(self) -> Control | None:
        if self.coupang_worker is not None and self.coupang_worker.isRunning():
            return self.coupang_control
        if self.foodspring_worker is not None and self.foodspring_worker.isRunning():
            return self.foodspring_control
        return self.control

    # ── Gmarket: 사전 조사 ─────────────────────────────────────────
    def _make_storage(self) -> Storage | None:
        try:
            return Storage(self.settings.output_dir())
        except OSError as e:
            QMessageBox.critical(
                self, "저장 경로 오류",
                f"저장 경로를 사용할 수 없습니다:\n{self.settings.output_dir()}\n\n{e}",
            )
            return None

    def on_prescan(self) -> None:
        if self._closing or self._close_prompt_active:
            return
        if self._active_worker() is not None:
            return
        cats = self.settings.selected_categories()
        if not cats:
            QMessageBox.warning(self, "카테고리 선택", "최소 한 개 이상의 카테고리를 선택하세요.")
            return

        from app.core.gmarket_preflight import check_gmarket_runtime

        runtime = check_gmarket_runtime()
        if not runtime.ok:
            QMessageBox.critical(self, "Gmarket 런타임 미준비", runtime.message)
            return

        storage = self._make_storage()
        if storage is None:
            return

        self.control = Control()
        self.prescan_results = []
        self.prescan_table.clear_results()
        self._prescan_snapshot = {
            "categories": tuple(c.name for c in cats),
            "output_dir": self.settings.output_dir(),
        }

        worker = PrescanWorker(storage, cats, self.control)
        worker.prescan_progress.connect(self.progress.set_category)
        worker.log_message.connect(self.log.append_log)
        worker.error_occurred.connect(self.on_error)
        worker.prescan_complete.connect(self.on_prescan_complete)
        worker.finished.connect(self._on_prescan_thread_done)
        worker.finished.connect(self._maybe_close_after_worker)
        self.prescan_worker = worker

        self.log.append_log(f"[Pre-scan] {len(cats)}개 카테고리 사전 조사를 시작합니다...")
        self._show_status("사전 조사 진행 중...")
        self._set_ui_state("prescanning")
        worker.start()

    def on_prescan_complete(self, results: list) -> None:
        self.prescan_results = list(results)
        if results:
            self.prescan_table.set_results(results)
            new_total = self.prescan_table.total_new()
            total_all = sum(r.total_codes for r in results)
            self.log.append_log(
                f"[Pre-scan] 조사 완료 — 신규 대상 총 {new_total:,}건"
            )
            self._show_status(f"사전 조사 완료 — 신규 {new_total:,}건")

            if not self._auto_start_after_prescan:
                if total_all == 0:
                    QMessageBox.warning(
                        self, "수집 가능한 상품 없음",
                        "조사한 모든 카테고리에서 상품을 찾지 못했습니다.\n"
                        "네트워크 상태, 차단(Cloudflare) 여부, 또는 카테고리 선택을 확인하세요.",
                    )
                elif new_total == 0:
                    QMessageBox.information(
                        self, "신규 대상 없음",
                        "선택한 카테고리의 상품이 이미 모두 수집되었습니다.\n"
                        "(collected_ids 기준 — 초기화 후 다시 조사하면 전체를 재수집할 수 있습니다.)",
                    )
        else:
            self._show_status("사전 조사 취소/실패")

    def _on_prescan_thread_done(self) -> None:
        self._set_ui_state("prescanned")
        if self._auto_start_after_prescan:
            self._auto_start_after_prescan = False
            if self._closing or self._close_prompt_active:
                self.log.append_log("[이어서 수집] 종료 처리 중이라 자동 수집 시작을 건너뜁니다.")
            elif self.prescan_results and self.prescan_table.total_new() > 0:
                self.on_start()
            else:
                self.log.append_log("[이어서 수집] 수집할 신규 대상이 없습니다.")

    # ── Gmarket: 수집 ──────────────────────────────────────────────
    def on_start(self) -> None:
        if self._closing or self._close_prompt_active:
            return
        if self._active_worker() is not None:
            return
        if not self.prescan_results:
            QMessageBox.information(self, "사전 조사 필요", "먼저 [사전 조사]를 실행하세요.")
            return
        if self.prescan_table.total_new() <= 0:
            QMessageBox.information(self, "수집 대상 없음", "수집 가능한 신규 상품이 없습니다.")
            return

        current_snapshot = {
            "categories": tuple(c.name for c in self.settings.selected_categories()),
            "output_dir": self.settings.output_dir(),
        }
        if current_snapshot != self._prescan_snapshot:
            QMessageBox.warning(
                self, "설정 변경 감지",
                "사전 조사 이후 카테고리 선택 또는 저장 경로가 변경되었습니다.\n"
                "변경된 설정으로 수집하려면 [사전 조사]를 다시 실행하세요.",
            )
            return

        storage = self._make_storage()
        if storage is None:
            return

        ids_status, collected_ids = storage.load_collected_ids_status()
        if ids_status == LoadStatus.QUARANTINE_FAILED:
            QMessageBox.critical(
                self, "저장 파일 손상",
                "collected_ids.json 이 손상됐고 격리(백업)에도 실패해 원본이 "
                "위험한 상태로 남아 있어 수집을 시작할 수 없습니다.\n\n"
                "저장 경로를 직접 확인해 수동으로 백업한 뒤 다시 시도하세요.",
            )
            return

        result = reconcile_leftover_checkpoints(
            storage, collected_ids, on_log=self.log.append_log
        )
        if result.blocking:
            problems = []
            if result.failed_labels:
                problems.append("저장 실패: " + ", ".join(result.failed_labels))
            if result.corrupt_labels:
                problems.append(
                    "손상되어 격리된 체크포인트(백업 파일(*.corrupt_*.bak)에서 수동 확인 필요): "
                    + ", ".join(result.corrupt_labels)
                )
            if result.quarantine_failed_labels:
                problems.append(
                    "손상됐지만 격리(백업)에도 실패해 원본이 위험한 체크포인트"
                    "(저장 경로에서 직접 확인 필요): "
                    + ", ".join(result.quarantine_failed_labels)
                )
            QMessageBox.critical(
                self, "체크포인트 문제로 수집 시작 불가",
                "\n\n".join(problems)
                + "\n\n문제를 해결한 뒤(저장 경로의 디스크 공간/쓰기 권한 확인, "
                "또는 손상된 체크포인트의 백업 파일 확인) 다시 시도하세요.",
            )
            return
        if result.cleanup_failed_labels:
            self.log.append_log(
                "[체크포인트 승격] 정리(삭제) 실패했지만 데이터는 안전합니다: "
                + ", ".join(result.cleanup_failed_labels)
            )

        plan = build_crawl_plan(
            self.prescan_results,
            collected_ids,
            max_items=self.settings.max_items(),
            output_dir=self.settings.output_dir(),
        )
        if plan.total_targets == 0:
            QMessageBox.information(
                self, "수집 대상 없음",
                "확정된 계획에 수집할 신규 상품이 없습니다.\n"
                "(사전 조사 이후 이미 모두 수집되었을 수 있습니다 — 다시 조사해보세요.)",
            )
            return
        self.log.append_log(
            f"[계획 확정] {len(plan.categories)}개 카테고리, {plan.total_targets:,}건 "
            f"(hash={plan.plan_hash})"
        )

        self.control = Control()
        self._success = 0
        self._processed = 0
        self.result_table.clear_records()
        self.progress.reset()
        self.progress.start(plan.total_targets)

        worker = CrawlWorker(
            storage,
            plan,
            self.control,
            delay=self.settings.delay(),
        )
        worker.progress_category.connect(self.progress.set_category)
        worker.progress_item.connect(self.on_progress_item)
        worker.item_collected.connect(self.on_item_collected)
        worker.log_message.connect(self.log.append_log)
        worker.error_occurred.connect(self.on_error)
        worker.finished_crawl.connect(self.on_crawl_finished)
        worker.finished.connect(self._on_crawl_thread_done)
        worker.finished.connect(self._maybe_close_after_worker)
        self.crawl_worker = worker

        self.log.append_log("[수집] Phase 2 판매자정보 수집을 시작합니다...")
        self._show_status("수집 진행 중...")
        self._set_ui_state("crawling")
        worker.start()

    def on_progress_item(self, goodscode: str, current: int, total: int) -> None:
        self._processed += 1
        self.progress.set_item_progress(current, total)
        self.progress.set_counts(self._success, max(0, self._processed - self._success))

    def on_item_collected(self, record: dict) -> None:
        self._success += 1
        self.result_table.add_record(record)
        self.progress.set_counts(self._success, max(0, self._processed - self._success))

    def on_crawl_finished(self, success: int, failed: int) -> None:
        self.progress.stop()
        self.progress.set_counts(success, failed)
        summary = getattr(self.crawl_worker, "summary", None)

        crashed = summary is None or bool(getattr(summary, "error", None))
        cancelled = bool(getattr(summary, "cancelled", False)) if summary else False

        saved = ""
        if summary is not None and summary.all_files:
            saved = f" | 저장: {summary.all_files[0]}"
            self.log.append_log(f"[완료] 통합 저장: {summary.all_files[0]}")

        if crashed:
            err = (getattr(summary, "error", None) if summary else None) or "알 수 없는 오류"
            cat = getattr(summary, "failed_category", None) if summary else None
            where = f" (카테고리: {cat})" if cat else ""
            self.log.append_log(
                f"[수집 실패] 오류로 중단됨{where}: {err} — 부분 저장: 성공 {success:,}건"
            )
            self._show_status(f"수집 실패: {err}")
            QMessageBox.critical(
                self, "수집 실패",
                f"수집 중 오류가 발생하여 중단되었습니다{where}.\n\n{err}\n\n"
                f"그때까지의 부분 결과는 저장되었습니다 (성공 {success:,}건).",
            )
        else:
            head = "수집 취소됨" if cancelled else "수집 완료"
            self.log.append_log(f"[{head}] 성공 {success:,}건 / 실패 {failed:,}건")
            self._show_status(f"{head} ({success:,}건){saved}")

    def _on_crawl_thread_done(self) -> None:
        self._set_ui_state("prescanned" if self.prescan_results else "idle")

    # ── Gmarket: 일시정지/취소/이어서/초기화 ───────────────────────
    def on_pause_toggle(self) -> None:
        if not self.control:
            return
        if self.control.is_paused():
            self.control.resume()
            self.btn_pause.setText("일시정지")
            self.log.append_log("[제어] 재개")
            self._show_status("수집 진행 중...")
        else:
            self.control.pause()
            self.btn_pause.setText("재개")
            self.log.append_log("[제어] 일시정지 (현재 건 완료 후 대기)")
            self._show_status("일시정지됨")

    def on_cancel(self) -> None:
        if self.control:
            self.control.request_cancel()
            self.log.append_log("[제어] 취소 요청 — 현재 건 완료 후 중지합니다.")
            self._show_status("취소 중...")

    def on_resume_run(self) -> None:
        if self._closing or self._close_prompt_active:
            return
        cats = self.settings.selected_categories()
        if not cats:
            QMessageBox.warning(self, "카테고리 선택", "최소 한 개 이상의 카테고리를 선택하세요.")
            return
        self._auto_start_after_prescan = True
        self.log.append_log("[이어서 수집] 사전 조사 후 남은 신규 건만 자동 수집합니다.")
        self.on_prescan()

    def on_reset(self) -> None:
        if self._closing or self._close_prompt_active:
            return
        if self._active_worker() is not None:
            return
        reply = QMessageBox.question(
            self,
            "초기화 확인",
            "수집 이력(collected_ids)과 진행 상태(fastcrawl_state)를 모두 초기화할까요?\n"
            "이후 사전 조사 시 모든 상품이 신규로 집계됩니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        storage = self._make_storage()
        if storage is None:
            return
        storage.reset_collected_ids()
        storage.reset_state()
        failed_partials = storage.clear_all_partials()
        self.prescan_results = []
        self._prescan_snapshot = None
        self.prescan_table.clear_results()
        self.result_table.clear_records()
        self.progress.reset()

        if failed_partials:
            names = ", ".join(p.name for p in failed_partials)
            self.log.append_log(f"[초기화] 일부 체크포인트 삭제 실패: {names}")
            self._show_status("초기화 일부 실패 — 체크포인트 확인 필요")
            QMessageBox.warning(
                self, "초기화 일부 실패",
                "수집 이력/상태는 초기화됐지만 다음 체크포인트 파일은 삭제하지 "
                f"못했습니다:\n{names}\n\n"
                "저장 경로에서 위 파일을 직접 지우지 않으면 다음 수집 시 이전 "
                "데이터가 다시 나타날 수 있습니다.",
            )
        else:
            self.log.append_log("[초기화] 수집 이력/상태를 초기화했습니다.")
            self._show_status("초기화 완료")
        self._set_ui_state("idle")

    # ── Coupang ────────────────────────────────────────────────────
    def on_coupang_start(self) -> None:
        if self._closing or self._close_prompt_active:
            return
        if self._active_worker() is not None:
            return

        config = self.coupang_panel.build_config()
        if config is None:
            QMessageBox.warning(
                self, "설정 오류",
                "출력 폴더를 선택하고 설정 값을 확인하세요.",
            )
            return

        # Output dir writability check
        try:
            config.output_dir.mkdir(parents=True, exist_ok=True)
            import tempfile as _tf

            fd, tmp_path = _tf.mkstemp(dir=str(config.output_dir), prefix=".preflight_")
            os.close(fd)
            os.unlink(tmp_path)
        except OSError as e:
            QMessageBox.critical(
                self, "출력 경로 오류",
                f"출력 폴더에 쓸 수 없습니다:\n{config.output_dir}\n\n{e}",
            )
            return

        # Runtime preflight
        from app.core.coupang.preflight import PreflightStatus, check_runtime

        try:
            pf = check_runtime()
        except Exception as e:  # noqa: BLE001
            # 방어선: preflight 는 알려진 실패를 구조화된 결과로 반환하지만,
            # 예기치 못한 예외가 나더라도 Qt 슬롯 밖으로 새어 나가지 않도록
            # critical 대화상자로 변환한다.
            QMessageBox.critical(
                self, "Coupang 런타임 확인 실패",
                f"런타임 준비 상태를 확인하는 중 예기치 못한 오류가 발생했습니다:\n{e}",
            )
            return
        if pf.status != PreflightStatus.OK:
            QMessageBox.critical(self, "Coupang 런타임 미준비", pf.message)
            return

        self.coupang_control = Control()
        self.coupang_panel.clear_results()

        worker = CoupangWorker(config, self.coupang_control)
        worker.phase_changed.connect(self.coupang_panel.set_phase)
        worker.progress_changed.connect(self._on_coupang_progress)
        worker.log_message.connect(self.coupang_panel.append_log)
        worker.item_collected.connect(self.coupang_panel.add_record)
        worker.stats_changed.connect(self._on_coupang_stats)
        worker.error_occurred.connect(self._on_coupang_error)
        worker.finished_crawl.connect(self._on_coupang_finished)
        worker.finished.connect(self._on_coupang_thread_done)
        worker.finished.connect(self._maybe_close_after_worker)
        self.coupang_worker = worker

        self.coupang_panel.set_state("running")
        self._set_gmarket_busy(True)
        self.foodspring_panel.set_external_busy(True)
        self._show_status("Coupang 수집 진행 중...")
        worker.start()

    def on_coupang_pause(self) -> None:
        if self.coupang_control and not self.coupang_control.is_paused():
            self.coupang_control.pause()
            self.coupang_panel.set_state("paused")
            self.coupang_panel.append_log("[제어] 일시정지 (현재 요청 완료 후 대기)")
            self._show_status("Coupang 일시정지됨")

    def on_coupang_resume(self) -> None:
        if self.coupang_control and self.coupang_control.is_paused():
            self.coupang_control.resume()
            self.coupang_panel.set_state("running")
            self.coupang_panel.append_log("[제어] 재개")
            self._show_status("Coupang 수집 진행 중...")

    def on_coupang_cancel(self) -> None:
        if self.coupang_control:
            self.coupang_control.request_cancel()
            self.coupang_panel.set_state("cancelling")
            self.coupang_panel.append_log("[제어] 취소 요청 — 현재 요청 완료 후 중지합니다.")
            self._show_status("Coupang 취소 중...")

    def _on_coupang_progress(self, kind: str, current: int, total: int) -> None:
        self.coupang_panel.set_progress_text(f"{kind}: {current}/{total}")

    def _on_coupang_stats(self, summary) -> None:
        stats = (
            f"상품 {summary.products_seen} | 판매자 {summary.unique_vendors} | "
            f"사업자 {summary.business_info_success} | "
            f"스킵 {summary.brand_seller_skipped} | 오류 {summary.request_errors} | "
            f"파워셀러 {summary.power_sellers}"
        )
        self.coupang_panel.set_stats_text(stats)

    def _on_coupang_open_result(self) -> None:
        summary = self.coupang_worker.summary if self.coupang_worker else None
        path = summary.json_path if summary else None
        if path:
            import subprocess
            import sys

            if sys.platform == "win32":
                os.startfile(os.path.dirname(path))
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        else:
            self.coupang_panel.append_log("[결과 열기] 저장된 결과가 없습니다.")

    def _on_coupang_error(self, msg: str) -> None:
        self.coupang_panel.append_log(f"[오류] {msg}")

    def _on_coupang_finished(self, summary) -> None:
        from app.core.coupang.outcome import RunOutcome, determine_outcome

        stats = (
            f"상품 {summary.products_seen} | 판매자 {summary.unique_vendors} | "
            f"사업자정보 {summary.business_info_success} | "
            f"스킵 {summary.brand_seller_skipped} | 오류 {summary.request_errors} | "
            f"파워셀러 {summary.power_sellers}"
        )
        self.coupang_panel.set_stats_text(stats)
        outcome = determine_outcome(summary)

        if outcome == RunOutcome.SAVE_ERROR:
            msg = summary.save_error or summary.error or "결과 저장 실패"
            self.coupang_panel.append_log(f"[실패] {msg}")
            self._show_status("Coupang 저장 실패")
            QMessageBox.critical(self, "Coupang 저장 실패", msg)
        elif outcome == RunOutcome.CLEANUP_ERROR:
            msg = f"브라우저 종료 실패 — 잔류 프로세스 확인 필요: {summary.cleanup_error}"
            self.coupang_panel.append_log(f"[실패] {msg}")
            if summary.json_path:
                self.coupang_panel.append_log(f"  결과 파일: {summary.json_path}")
            self._show_status("Coupang cleanup 실패")
            QMessageBox.critical(self, "Coupang 브라우저 종료 실패", msg)
        elif outcome == RunOutcome.CANCELLED:
            if summary.records and summary.json_path:
                self.coupang_panel.append_log("[취소] 부분 결과 저장 완료")
            else:
                self.coupang_panel.append_log("[취소] 수집된 결과 없음")
            self._show_status("Coupang 취소됨")
        elif outcome == RunOutcome.ERROR:
            self.coupang_panel.append_log(f"[실패] {summary.error}")
            self._show_status(f"Coupang 실패: {summary.error}")
        elif outcome == RunOutcome.NO_RECORDS:
            reason_msg = {
                "no_items": "수집된 상품 없음",
                "template_not_captured": "피드 요청 캡처 실패",
                "token_exhausted": "피드 종료 — 확보 사업자 0명",
                "no_new_items": "피드 종료 — 확보 사업자 0명",
                "page_limit": "페이지 상한 도달 — 확보 사업자 0명",
            }.get(summary.termination_reason, summary.termination_reason)
            self.coupang_panel.append_log(f"[실패] {reason_msg}")
            self._show_status(f"Coupang 실패: {reason_msg}")
        else:
            reason_msg = {
                "token_exhausted": "피드 자연 종료",
                "no_new_items": "피드 자연 종료 (신규 없음)",
                "page_limit": "페이지 상한 도달 (전량 보장 안 함)",
            }.get(summary.termination_reason, summary.termination_reason)
            self.coupang_panel.append_log(f"[완료] {reason_msg} | {stats}")
            self._show_status(f"Coupang 완료 ({summary.business_info_success}명)")

    def _on_coupang_thread_done(self) -> None:
        from app.core.coupang.outcome import RunOutcome, determine_outcome

        self._set_gmarket_busy(False)
        self.foodspring_panel.set_external_busy(False)
        self._set_ui_state("prescanned" if self.prescan_results else "idle")
        summary = self.coupang_worker.summary if self.coupang_worker else None
        if summary is None:
            self.coupang_panel.set_state("failed")
            return
        outcome = determine_outcome(summary)
        if outcome in (RunOutcome.SAVE_ERROR, RunOutcome.CLEANUP_ERROR, RunOutcome.ERROR, RunOutcome.NO_RECORDS):
            self.coupang_panel.set_state("failed")
        else:
            self.coupang_panel.set_state("finished")

    def _set_gmarket_busy(self, busy: bool) -> None:
        if busy:
            self.btn_prescan.setEnabled(False)
            self.btn_start.setEnabled(False)
            self.btn_resume_run.setEnabled(False)
            self.btn_reset.setEnabled(False)
        else:
            self._set_ui_state("prescanned" if self.prescan_results else "idle")

    # ── Foodspring ────────────────────────────────────────────────
    def on_foodspring_start(self) -> None:
        if self._closing or self._close_prompt_active:
            return
        if self._active_worker() is not None:
            return

        config = self.foodspring_panel.build_config()
        if config is None:
            QMessageBox.warning(
                self, "설정 오류",
                "출력 폴더를 선택하고 설정 값을 확인하세요.",
            )
            return

        # 출력 경로 쓰기 확인
        try:
            config.output_dir.mkdir(parents=True, exist_ok=True)
            import tempfile as _tf

            fd, tmp_path = _tf.mkstemp(dir=str(config.output_dir), prefix=".preflight_")
            os.close(fd)
            os.unlink(tmp_path)
        except OSError as e:
            QMessageBox.critical(
                self, "출력 경로 오류",
                f"출력 폴더에 쓸 수 없습니다:\n{config.output_dir}\n\n{e}",
            )
            return

        # 런타임 preflight
        from app.core.foodspring.preflight import (
            PreflightStatus,
            check_all,
        )

        try:
            pf = check_all()
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(
                self, "Foodspring 런타임 확인 실패",
                f"런타임 준비 상태를 확인하는 중 오류가 발생했습니다:\n{e}",
            )
            return
        if pf.status != PreflightStatus.OK:
            QMessageBox.critical(self, "Foodspring 런타임 미준비", pf.message)
            return

        self.foodspring_control = Control()
        self.foodspring_panel.clear_results()

        worker = FoodSpringWorker(config, self.foodspring_control)
        worker.phase_changed.connect(self.foodspring_panel.set_phase)
        worker.progress_changed.connect(self._on_foodspring_progress)
        worker.log_message.connect(self.foodspring_panel.append_log)
        worker.item_collected.connect(self.foodspring_panel.add_record)
        worker.stats_changed.connect(self._on_foodspring_stats)
        worker.error_occurred.connect(self._on_foodspring_error)
        worker.finished_crawl.connect(self._on_foodspring_finished)
        worker.finished.connect(self._on_foodspring_thread_done)
        worker.finished.connect(self._maybe_close_after_worker)
        self.foodspring_worker = worker

        self.foodspring_panel.set_state("running")
        self._set_gmarket_busy(True)
        self.coupang_panel.set_external_busy(True)
        self._show_status("Foodspring 수집 진행 중...")
        try:
            worker.start()
        except Exception as e:  # noqa: BLE001
            # 스레드 시작 자체가 실패하면 잠금을 복구하고 실패 상태로 전환
            self.foodspring_panel.set_state("failed")
            self._set_gmarket_busy(False)
            self.coupang_panel.set_external_busy(False)
            self.foodspring_panel.append_log(f"[오류] 워커 시작 실패: {e}")
            self._show_status("Foodspring 시작 실패")
            QMessageBox.critical(self, "Foodspring 시작 실패", str(e))

    def on_foodspring_pause(self) -> None:
        if self.foodspring_control and not self.foodspring_control.is_paused():
            self.foodspring_control.pause()
            self.foodspring_panel.set_state("paused")
            self.foodspring_panel.append_log("[제어] 일시정지 (현재 요청 완료 후 대기)")
            self._show_status("Foodspring 일시정지됨")

    def on_foodspring_resume(self) -> None:
        if self.foodspring_control and self.foodspring_control.is_paused():
            self.foodspring_control.resume()
            self.foodspring_panel.set_state("running")
            self.foodspring_panel.append_log("[제어] 재개")
            self._show_status("Foodspring 수집 진행 중...")

    def on_foodspring_cancel(self) -> None:
        if self.foodspring_control:
            self.foodspring_control.request_cancel()
            self.foodspring_panel.set_state("cancelling")
            self.foodspring_panel.append_log("[제어] 취소 요청 — 현재 요청 완료 후 중지합니다.")
            self._show_status("Foodspring 취소 중...")

    def _on_foodspring_progress(self, kind: str, current: int, total: int) -> None:
        self.foodspring_panel.set_progress_text(f"{kind}: {current}/{total}")

    def _on_foodspring_stats(self, summary) -> None:
        stats = (
            f"상품 {getattr(summary, 'products_seen', 0)} | "
            f"셀러 {getattr(summary, 'unique_vendors', 0)} | "
            f"사업자등록번호 {getattr(summary, 'business_info_success', 0)} | "
            f"이메일 {getattr(summary, 'email_success', 0)}"
        )
        self.foodspring_panel.set_stats_text(stats)

    def _on_foodspring_open_result(self) -> None:
        summary = self.foodspring_worker.summary if self.foodspring_worker else None
        path = summary.xlsx_path if summary else None
        if path:
            import subprocess
            import sys

            if sys.platform == "win32":
                os.startfile(os.path.dirname(path))
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        else:
            self.foodspring_panel.append_log("[결과 열기] 저장된 결과가 없습니다.")

    def _on_foodspring_error(self, msg: str) -> None:
        self.foodspring_panel.append_log(f"[오류] {msg}")

    def _on_foodspring_finished(self, summary) -> None:
        from app.core.foodspring.outcome import (
            RunOutcome,
            determine_outcome,
        )

        outcome = determine_outcome(summary)
        stats = (
            f"상품 {summary.products_seen} | 셀러 {summary.unique_vendors} | "
            f"사업자등록번호 {summary.business_info_success} | "
            f"이메일 {summary.email_success} | "
            f"정보없음 {summary.missing_info_sellers} | 오류 {summary.request_errors}"
        )
        self.foodspring_panel.set_stats_text(stats)
        saved = f" — 부분 저장: {summary.xlsx_path}" if summary.xlsx_path else ""
        if outcome == RunOutcome.SAVE_ERROR:
            msg = summary.save_error or summary.error or "결과 저장 실패"
            self.foodspring_panel.append_log(f"[실패] 엑셀 저장 실패: {msg}")
            self._show_status("Foodspring 저장 실패")
            QMessageBox.critical(self, "Foodspring 저장 실패", msg)
        elif outcome == RunOutcome.CANCELLED:
            if summary.xlsx_path:
                self.foodspring_panel.append_log("[취소] 부분 결과 저장 완료")
            else:
                self.foodspring_panel.append_log("[취소] 수집된 결과 없음")
            self._show_status("Foodspring 취소됨")
        elif outcome == RunOutcome.ERROR:
            self.foodspring_panel.append_log(f"[실패] {summary.error}{saved}")
            self._show_status(f"Foodspring 실패: {summary.error}")
        elif outcome == RunOutcome.NO_RECORDS:
            reason_msg = {
                "no_items": "수집된 상품 없음",
                "no_sellers": "셀러 정보 없음",
            }.get(summary.termination_reason, summary.termination_reason)
            self.foodspring_panel.append_log(f"[실패] {reason_msg}")
            self._show_status(f"Foodspring 실패: {reason_msg}")
        else:
            self.foodspring_panel.append_log(
                f"[완료] {stats} | 엑셀: {summary.xlsx_path}"
            )
            self._show_status(f"Foodspring 완료 ({summary.business_info_success} 셀러)")

    def _on_foodspring_thread_done(self) -> None:
        from app.core.foodspring.outcome import (
            RunOutcome,
            determine_outcome,
        )

        self._set_gmarket_busy(False)
        self.coupang_panel.set_external_busy(False)
        summary = self.foodspring_worker.summary if self.foodspring_worker else None
        if summary is None:
            self.foodspring_panel.set_state("failed")
            return
        outcome = determine_outcome(summary)
        if outcome in (RunOutcome.SAVE_ERROR, RunOutcome.ERROR, RunOutcome.NO_RECORDS):
            self.foodspring_panel.set_state("failed")
        else:
            self.foodspring_panel.set_state("finished")

    # ── 공통 ───────────────────────────────────────────────────────
    def on_error(self, msg: str) -> None:
        self.log.append_log(f"[오류] {msg}")

    def _maybe_close_after_worker(self) -> None:
        if self._closing:
            self.close()

    def closeEvent(self, event) -> None:
        if self._closing:
            if self._active_worker() is None:
                super().closeEvent(event)
            else:
                event.ignore()
            return

        active = self._active_worker()
        if active is None:
            super().closeEvent(event)
            return

        self._close_prompt_active = True
        reply = QMessageBox.question(
            self, "종료 확인",
            "수집이 진행 중입니다.\n"
            "현재 요청을 완료하고 중간 저장한 뒤 종료할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            self._close_prompt_active = False
            event.ignore()
            return

        self._closing = True
        ctrl = self._active_control()
        if ctrl:
            ctrl.request_cancel()
        self._show_status("종료 처리 중... 현재 요청 완료 후 종료합니다.")
        self.setEnabled(False)

        if self._active_worker() is None:
            self._closing = False
            self.setEnabled(True)
            super().closeEvent(event)
            return
        event.ignore()
