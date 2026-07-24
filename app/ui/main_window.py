"""메인 윈도우: 설정/사전조사/진행/로그/결과 위젯과 워커를 배선한다 (WORK_ORDER §7).

사용자 플로우 (§7.4):
    [카테고리 선택] → [사전 조사] → [조사 결과 확인] → [수집 시작] → [완료/저장]
취소/일시정지는 Control(스레드 이벤트) 로 건 단위 안전 중단을 보장한다 (§7.5).
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core.base import Control
from app.core.crawler import reconcile_leftover_checkpoints
from app.core.plan import build_crawl_plan
from app.core.storage import LoadStatus, Storage
from app.models.records import PrescanResult
from app.ui.widgets.log_panel import LogPanel
from app.ui.widgets.prescan_table import PrescanTable
from app.ui.widgets.progress_panel import ProgressPanel
from app.ui.widgets.result_table import ResultTable
from app.ui.widgets.settings_panel import SettingsPanel
from app.workers.crawl_worker import CrawlWorker
from app.workers.prescan_worker import PrescanWorker


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Gmarket 판매자 수집기 v2.0")
        self.resize(1100, 900)

        # 상태
        self.prescan_results: list[PrescanResult] = []
        self.control: Control | None = None
        self.prescan_worker: PrescanWorker | None = None
        self.crawl_worker: CrawlWorker | None = None
        self._success = 0
        self._processed = 0
        self._auto_start_after_prescan = False
        # 마지막으로 완료된 Pre-scan 이 사용한 카테고리/저장 경로 스냅샷.
        # 조사 이후 설정이 바뀌면 [수집 시작] 시 재조사를 요구한다 (§7.4 정합성 보장).
        self._prescan_snapshot: dict | None = None
        # closeEvent 가 취소 요청 후 워커 종료를 기다리는 중인지 여부.
        self._closing = False
        # 종료 확인 QMessageBox 가 열려 있는 동안(사용자가 아직 답하지 않은
        # 상태) True. Qt 의 중첩 이벤트 루프는 이 모달 대화상자가 떠 있는
        # 동안에도 다른 시그널(예: Pre-scan 완료)을 계속 처리하므로, 이 플래그로
        # '이어서 수집' 자동 시작 등 새 워커 생성이 그 틈에 끼어들지 못하게
        # 막는다 (3차 리뷰 HIGH-3 회귀 방지). _closing 과 달리 사용자가 "아니오"
        # 를 선택하면 다시 False 로 풀린다.
        self._close_prompt_active = False

        self._build_ui()
        self._set_ui_state("idle")

    # ── UI 구성 ────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)

        # 설정 패널
        self.settings = SettingsPanel()
        root.addWidget(self.settings)

        # 버튼 행 (§7.3)
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
        root.addLayout(btn_row)

        self.btn_prescan.clicked.connect(self.on_prescan)
        self.btn_start.clicked.connect(self.on_start)
        self.btn_pause.clicked.connect(self.on_pause_toggle)
        self.btn_cancel.clicked.connect(self.on_cancel)
        self.btn_resume_run.clicked.connect(self.on_resume_run)
        self.btn_reset.clicked.connect(self.on_reset)

        # 결과/진행/로그 위젯
        self.prescan_table = PrescanTable()
        self.progress = ProgressPanel()
        self.log = LogPanel()
        self.result_table = ResultTable()

        # 스크롤 가능한 본문 (작은 화면 대응)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.addWidget(self.prescan_table)
        body_layout.addWidget(self.progress)
        body_layout.addWidget(self.log)
        body_layout.addWidget(self.result_table)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        root.addWidget(scroll, stretch=1)

        self.setCentralWidget(central)

        # 하단 상태 표시줄
        self.statusBar().showMessage("준비됨")

    # ── 상태 전이 (버튼 활성화 규칙) ───────────────────────────────
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

    # ── 공통 ───────────────────────────────────────────────────────
    def _make_storage(self) -> Storage | None:
        """저장 경로로 Storage 생성. 경로가 유효하지 않으면 안내 후 None 반환."""
        try:
            return Storage(self.settings.output_dir())
        except OSError as e:
            QMessageBox.critical(
                self, "저장 경로 오류",
                f"저장 경로를 사용할 수 없습니다:\n{self.settings.output_dir()}\n\n{e}",
            )
            return None

    # ── 사전 조사 (Phase 0) ────────────────────────────────────────
    def on_prescan(self) -> None:
        cats = self.settings.selected_categories()
        if not cats:
            QMessageBox.warning(self, "카테고리 선택", "최소 한 개 이상의 카테고리를 선택하세요.")
            return

        storage = self._make_storage()
        if storage is None:
            return

        self.control = Control()
        self.prescan_results = []
        self.prescan_table.clear_results()
        # 이번 조사가 사용한 설정 스냅샷 — [수집 시작] 시 변경 여부 대조용.
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
        # 종료 대기 재시도용 — 워커 생성 시점에 즉시 연결한다(지연 연결 금지).
        # closeEvent 의 확인 대화상자가 열려 있는 동안 워커가 끝나도 이 연결이
        # 이미 살아있으므로 finished 시그널을 놓치지 않는다(HIGH-2 회귀 방지).
        worker.finished.connect(self._maybe_close_after_worker)
        self.prescan_worker = worker

        self.log.append_log(f"[Pre-scan] {len(cats)}개 카테고리 사전 조사를 시작합니다...")
        self.statusBar().showMessage("사전 조사 진행 중...")
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
            self.statusBar().showMessage(f"사전 조사 완료 — 신규 {new_total:,}건")

            # WORK_ORDER §6: Pre-scan 전체 0건 → 수집 불가 알림.
            # '이어서 수집' 자동 흐름에서는 팝업 대신 로그로만 안내(불필요한 확인 클릭 방지).
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
            self.statusBar().showMessage("사전 조사 취소/실패")

    def _on_prescan_thread_done(self) -> None:
        self._set_ui_state("prescanned")
        # '이어서 수집'으로 트리거된 경우 조사 후 자동으로 수집 시작
        if self._auto_start_after_prescan:
            self._auto_start_after_prescan = False
            if self._closing or self._close_prompt_active:
                # 종료 확인 대화상자가 열려 있는 동안(또는 종료 처리 중) Pre-scan
                # 이 끝난 경우 — 새 워커를 시작하지 않는다(3차 리뷰 HIGH-3).
                # 사용자가 "아니오"를 눌러 종료를 취소하면 조사 결과는 이미
                # 화면에 채워져 있으니 [수집 시작]을 수동으로 누르면 된다.
                self.log.append_log("[이어서 수집] 종료 처리 중이라 자동 수집 시작을 건너뜁니다.")
            elif self.prescan_results and self.prescan_table.total_new() > 0:
                self.on_start()
            else:
                self.log.append_log("[이어서 수집] 수집할 신규 대상이 없습니다.")

    # ── 수집 (Phase 1+2) ───────────────────────────────────────────
    def on_start(self) -> None:
        if self._closing or self._close_prompt_active:
            # 종료 처리/확인 중에는 새 워커를 시작하지 않는다(3차 리뷰 HIGH-3).
            return
        if not self.prescan_results:
            QMessageBox.information(self, "사전 조사 필요", "먼저 [사전 조사]를 실행하세요.")
            return
        if self.prescan_table.total_new() <= 0:
            QMessageBox.information(self, "수집 대상 없음", "수집 가능한 신규 상품이 없습니다.")
            return

        # Pre-scan 이후 카테고리 선택/저장 경로가 바뀌었다면, 화면에 보이는 조사
        # 결과와 실제로 수집될 대상이 어긋난다. 재조사를 요구해 불일치를 차단한다.
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

        # collected_ids.json 은 중복 방지의 단일 진실 공급원이다. 격리(백업)
        # 조차 실패해 원본이 위험한 상태로 남아있는데(QUARANTINE_FAILED) 그냥
        # 빈 집합으로 진행하면 ①대량 재수집이 발생하고 ②이후 저장이 백업
        # 없는 원본을 조용히 덮어써 영구히 잃는다(6차 리뷰 HIGH-4). 이
        # 상태에서는 아예 시작하지 않는다.
        ids_status, collected_ids = storage.load_collected_ids_status()
        if ids_status == LoadStatus.QUARANTINE_FAILED:
            QMessageBox.critical(
                self, "저장 파일 손상",
                "collected_ids.json 이 손상됐고 격리(백업)에도 실패해 원본이 "
                "위험한 상태로 남아 있어 수집을 시작할 수 없습니다.\n\n"
                "저장 경로를 직접 확인해 수동으로 백업한 뒤 다시 시도하세요.",
            )
            return

        # CrawlPlan 을 확정하기 전에 먼저 잔여 체크포인트를 승격한다(3차 리뷰
        # MEDIUM). 순서가 반대이면(예전 동작) 계획이 아직 최종 파일로 승격되지
        # 않은 체크포인트 속 goodscode 를 '신규'로 오인해 다시 계획에 담을 수
        # 있다 — 승격이 나중에 성공하면 collected_ids 에는 있지만 계획에는
        # 이미 포함된 상태로 어긋난다. 승격 실패 또는 손상된(자동 복구 불가)
        # 체크포인트가 있으면(fail-closed) 아예 수집을 시작하지 않는다 — 문제를
        # 해결한 뒤 재시도해야 안전하다(4차 리뷰 아키텍처 BLOCK-2: 손상 체크포인트를
        # 조용히 넘기면 사용자가 데이터 유실 가능성을 영영 모를 수 있다).
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

        # 지금 이 순간의(승격까지 반영된) collected_ids 스냅샷으로 카테고리별
        # target_codes 를 max_items 캡까지 완전히 확정한다. 이후 crawl() 실행
        # 중 collected_ids.json 이 바뀌어도 이 계획 자체는 흔들리지 않는다 —
        # Pre-scan 에서 사용자가 확인한 대상과 실제로 수집되는 대상을 항상
        # 일치시킨다.
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
        # 종료 대기 재시도용 — 워커 생성 시점에 즉시 연결한다(HIGH-2 회귀 방지).
        worker.finished.connect(self._maybe_close_after_worker)
        self.crawl_worker = worker

        self.log.append_log("[수집] Phase 2 판매자정보 수집을 시작합니다...")
        self.statusBar().showMessage("수집 진행 중...")
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

        # summary 가 없으면(워커 최상위 예외) 또는 summary.error 가 설정되어 있으면
        # "완료"가 아니라 명확한 실패로 표시한다 — 그렇지 않으면 크래시가 조용히
        # "수집 완료 (0건)"으로 보여 사용자가 실패를 알아채지 못한다.
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
            self.statusBar().showMessage(f"수집 실패: {err}")
            QMessageBox.critical(
                self, "수집 실패",
                f"수집 중 오류가 발생하여 중단되었습니다{where}.\n\n{err}\n\n"
                f"그때까지의 부분 결과는 저장되었습니다 (성공 {success:,}건).",
            )
        else:
            head = "수집 취소됨" if cancelled else "수집 완료"
            self.log.append_log(f"[{head}] 성공 {success:,}건 / 실패 {failed:,}건")
            self.statusBar().showMessage(f"{head} ({success:,}건){saved}")

    def _on_crawl_thread_done(self) -> None:
        self._set_ui_state("prescanned" if self.prescan_results else "idle")

    # ── 일시정지 / 취소 / 이어서 / 초기화 ──────────────────────────
    def on_pause_toggle(self) -> None:
        if not self.control:
            return
        if self.control.is_paused():
            self.control.resume()
            self.btn_pause.setText("일시정지")
            self.log.append_log("[제어] 재개")
            self.statusBar().showMessage("수집 진행 중...")
        else:
            self.control.pause()
            self.btn_pause.setText("재개")
            self.log.append_log("[제어] 일시정지 (현재 건 완료 후 대기)")
            self.statusBar().showMessage("일시정지됨")

    def on_cancel(self) -> None:
        if self.control:
            self.control.request_cancel()
            self.log.append_log("[제어] 취소 요청 — 현재 건 완료 후 중지합니다.")
            self.statusBar().showMessage("취소 중...")

    def on_resume_run(self) -> None:
        """이어서 수집: 사전 조사 후 자동으로 수집 시작(이미 수집분은 자동 제외)."""
        cats = self.settings.selected_categories()
        if not cats:
            QMessageBox.warning(self, "카테고리 선택", "최소 한 개 이상의 카테고리를 선택하세요.")
            return
        self._auto_start_after_prescan = True
        self.log.append_log("[이어서 수집] 사전 조사 후 남은 신규 건만 자동 수집합니다.")
        self.on_prescan()

    def on_reset(self) -> None:
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
            # 삭제 실패를 조용히 넘기면 '초기화 완료'로 보이지만 실제로는
            # 다음 실행의 체크포인트 승격 로직이 지워지지 않은 옛 체크포인트를
            # 되살려 collected_ids 를 다시 채울 수 있다(3차 리뷰 MEDIUM).
            names = ", ".join(p.name for p in failed_partials)
            self.log.append_log(f"[초기화] 일부 체크포인트 삭제 실패: {names}")
            self.statusBar().showMessage("초기화 일부 실패 — 체크포인트 확인 필요")
            QMessageBox.warning(
                self, "초기화 일부 실패",
                "수집 이력/상태는 초기화됐지만 다음 체크포인트 파일은 삭제하지 "
                f"못했습니다:\n{names}\n\n"
                "저장 경로에서 위 파일을 직접 지우지 않으면 다음 수집 시 이전 "
                "데이터가 다시 나타날 수 있습니다.",
            )
        else:
            self.log.append_log("[초기화] 수집 이력/상태를 초기화했습니다.")
            self.statusBar().showMessage("초기화 완료")
        self._set_ui_state("idle")

    # ── 공통 ───────────────────────────────────────────────────────
    def on_error(self, msg: str) -> None:
        self.log.append_log(f"[오류] {msg}")

    def _active_worker(self):
        for worker in (self.crawl_worker, self.prescan_worker):
            if worker is not None and worker.isRunning():
                return worker
        return None

    def _maybe_close_after_worker(self) -> None:
        """종료 대기 중(self._closing)이면 워커 완료 시 창 닫기를 재시도.

        prescan_worker/crawl_worker 생성 시점에 **즉시** 이 슬롯을 연결해둔다
        (closeEvent 안에서 지연 연결하지 않는다). 그렇지 않으면 '종료 확인'
        대화상자가 열려 있는 동안 워커가 끝나 finished 시그널이 아직 아무도
        연결하지 않은 채로 지나가버려 영구히 놓치는 경쟁 조건이 생긴다
        (HIGH-2 회귀 방지). _closing 이 False 인 동안은 그냥 아무 것도 하지 않는다.
        """
        if self._closing:
            self.close()

    def closeEvent(self, event) -> None:
        """실행 중인 워커가 있으면 실제로 스레드가 끝날 때까지 창 종료를 보류한다.

        Phase 0/1 리스팅은 15~20초 타임아웃, Phase 2 재시도는 최대 수 초가 걸릴 수
        있어 고정된 짧은 대기(예: 3초)로는 스레드가 살아있는 채로 창이 닫혀 좀비
        스레드/파일 쓰기 충돌이 발생할 수 있다. 취소를 요청한 뒤 창 닫기 이벤트는
        무시하고, 워커의 finished 시그널이 실제로 도착했을 때만(폴링이나 고정
        타임아웃이 아니라) 다시 close() 를 호출해 안전하게 종료한다.
        """
        if self._closing:
            # 이미 종료 처리 중: 워커가 실제로 끝났는지(=finished 도착) 재확인.
            if self._active_worker() is None:
                super().closeEvent(event)
            else:
                event.ignore()
            return

        active = self._active_worker()
        if active is None:
            super().closeEvent(event)
            return

        # 대화상자를 띄우기 전에 먼저 가드를 켠다 — QMessageBox 는 모달이지만
        # Qt 의 중첩 이벤트 루프는 그동안에도 다른 시그널(예: Pre-scan
        # 완료 → '이어서 수집' 자동 시작)을 계속 처리한다. 이 창이 열려 있는
        # 동안 그런 시그널이 새 워커를 만들지 못하도록 막는다(3차 리뷰 HIGH-3).
        self._close_prompt_active = True
        reply = QMessageBox.question(
            self, "종료 확인",
            "사전 조사/수집이 진행 중입니다.\n"
            "현재 건을 완료하고 중간 저장한 뒤 종료할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            self._close_prompt_active = False
            event.ignore()
            return

        self._closing = True
        if self.control:
            self.control.request_cancel()
        self.statusBar().showMessage("종료 처리 중... 현재 건 완료 후 종료합니다.")
        self.setEnabled(False)

        # 확인 대화상자가 열려 있던 동안 워커가 이미 끝났을 수 있다 — finished
        # 시그널은 워커 생성 시점에 이미 연결돼 있으므로(_maybe_close_after_worker)
        # 놓치지는 않지만, self._closing 이 그때는 아직 False 라 아무 동작도
        # 하지 않았을 수 있다. 여기서 즉시 한 번 더 실제 상태를 확인한다.
        if self._active_worker() is None:
            self._closing = False
            self.setEnabled(True)
            super().closeEvent(event)
            return
        event.ignore()
