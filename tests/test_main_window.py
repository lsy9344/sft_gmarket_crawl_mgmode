"""app.ui.main_window(MainWindow) 실제 PyQt6 통합 테스트.

PyQt6 가 설치되어 있지 않은 환경(이 저장소의 기본 stdlib-only 개발 환경)에서는
이 파일의 모든 테스트를 건너뛴다 — tests/ 전체 discover 가 깨지지 않게 하기
위함이다(tests/__init__.py 가 requests/bs4 를 조건부로 스텁하는 것과 같은
패턴). PyQt6 를 설치한 환경(예: `pip install PyQt6` 한 venv)에서 실행하면
실제 QApplication + 이벤트 루프로, 지금까지 코드 리뷰로만(실행 검증 없이)
다뤄졌던 UI 경쟁 조건 — 특히 3차 리뷰 HIGH-2/HIGH-3 의 '종료 확인 대화상자가
열려 있는 동안 다른 시그널이 새 워커를 만들면 안 된다' — 을 실제로 검증한다.

QT_QPA_PLATFORM=offscreen 환경 변수를 설정하면 디스플레이 없이도 실행된다:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_main_window -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.base import Control

try:
    from PyQt6.QtGui import QCloseEvent
    from PyQt6.QtWidgets import QApplication, QMessageBox

    _PYQT6_AVAILABLE = True
except ImportError:  # pragma: no cover - PyQt6 미설치 환경
    _PYQT6_AVAILABLE = False


@unittest.skipUnless(_PYQT6_AVAILABLE, "PyQt6 미설치 — UI 통합 테스트 건너뜀")
class _MainWindowTestCase(unittest.TestCase):
    """공통 setUp: 실제 QApplication + MainWindow 인스턴스."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        from app.ui.main_window import MainWindow

        self.win = MainWindow()
        self.addCleanup(self._safe_close)

    def _safe_close(self) -> None:
        """테스트가 가짜(MagicMock) 워커를 prescan_worker/crawl_worker 에
        남겨둔 채 끝날 수 있다 — 정리 시점에 실제 close() 가 이를 "실행 중인
        워커"로 오인해 (모킹되지 않은) 진짜 QMessageBox.question() 을
        띄우려 하면 offscreen 플랫폼에서 크래시가 난다. 정리 전 항상 워커
        참조를 비우고 순수하게 위젯만 닫는다."""
        self.win.prescan_worker = None
        self.win.crawl_worker = None
        self.win.coupang_worker = None
        self.win.category_worker = None
        self.win.categories_worker = None
        self.win.category_login_worker = None
        self.win._closing = False
        self.win._close_prompt_active = False
        self.win.close()


class InitialStateTest(_MainWindowTestCase):
    def test_initial_ui_state_idle(self) -> None:
        self.assertTrue(self.win.btn_prescan.isEnabled())
        self.assertFalse(self.win.btn_start.isEnabled())  # Pre-scan 전에는 비활성
        self.assertFalse(self.win.btn_pause.isEnabled())
        self.assertFalse(self.win.btn_cancel.isEnabled())
        self.assertTrue(self.win.btn_reset.isEnabled())
        self.assertIsNone(self.win._active_worker())

    def test_close_with_no_active_worker_accepts_immediately(self) -> None:
        event = QCloseEvent()
        self.win.closeEvent(event)
        self.assertTrue(event.isAccepted())


class GmarketRuntimePreflightTest(_MainWindowTestCase):
    def test_missing_runtime_blocks_prescan_before_storage_or_worker(self) -> None:
        from app.core.gmarket_preflight import GmarketPreflightResult

        self.win.settings.selected_categories = lambda: [object()]  # type: ignore[method-assign]
        result = GmarketPreflightResult(False, "setup required")
        with (
            patch("app.core.gmarket_preflight.check_gmarket_runtime", return_value=result),
            patch.object(QMessageBox, "critical") as critical,
            patch.object(self.win, "_make_storage") as make_storage,
        ):
            self.win.on_prescan()

        critical.assert_called_once()
        make_storage.assert_not_called()
        self.assertIsNone(self.win.prescan_worker)


class CloseEventRaceTest(_MainWindowTestCase):
    """3차 리뷰 HIGH-2/HIGH-3 회귀: 종료 확인 대화상자가 열려 있는 동안(Qt 의
    중첩 이벤트 루프가 다른 시그널을 계속 처리하는 동안) Pre-scan 완료
    시그널이 도착해도 '이어서 수집'의 자동 수집 시작이 발생하면 안 된다.

    QMessageBox.question 을 모킹하면서, 그 side_effect 안에서 직접
    _on_prescan_thread_done() 을 호출해 "모달 대화상자가 화면에 떠 있는
    바로 그 순간" 다른 시그널이 도착하는 상황을 정확히 재현한다 — 실제 Qt
    의 중첩 이벤트 루프가 정확히 이 지점에서 다른 콜백을 실행시키기 때문에,
    이 모킹은 인위적인 우회가 아니라 그 타이밍을 그대로 옮겨온 것이다.
    """

    def _make_running_worker(self):
        worker = MagicMock()
        worker.isRunning.return_value = True
        return worker

    def test_prescan_completion_during_close_dialog_does_not_autostart(self) -> None:
        self.win._auto_start_after_prescan = True
        self.win.prescan_results = []
        self.win.prescan_worker = self._make_running_worker()
        self.win.crawl_worker = None

        started: list[bool] = []
        self.win.on_start = lambda: started.append(True)  # type: ignore[method-assign]

        def fake_question(*_a, **_k):
            self.win._on_prescan_thread_done()
            return QMessageBox.StandardButton.Yes

        with patch.object(QMessageBox, "question", side_effect=fake_question):
            self.win.closeEvent(QCloseEvent())

        self.assertEqual(started, [])
        self.assertFalse(self.win._auto_start_after_prescan)  # 소비되어 False 로 남는다

    def test_on_start_itself_refuses_to_run_while_close_prompt_active(self) -> None:
        """closeEvent 가드뿐 아니라 on_start() 자신도 방어선을 갖는다 — 어떤
        경로로든(버튼 클릭 재현 등) 종료 확인 중 호출되면 즉시 반환해야 한다."""
        self.win._close_prompt_active = True
        self.win.prescan_results = [object()]  # 방어선 통과 여부만 보려는 것이므로 내용은 무관

        # on_start() 는 _closing/_close_prompt_active 검사에서 바로 return 해야
        # 하며, 그 이후의 storage/plan 관련 코드에 도달하면 안 된다.
        with patch.object(self.win, "_make_storage") as make_storage:
            self.win.on_start()
            make_storage.assert_not_called()

    def test_no_answer_resets_close_prompt_active_and_ignores_close(self) -> None:
        self.win.prescan_worker = self._make_running_worker()

        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            event = QCloseEvent()
            self.win.closeEvent(event)

        self.assertFalse(event.isAccepted())
        self.assertFalse(self.win._closing)
        self.assertFalse(self.win._close_prompt_active)

    def test_worker_finished_during_dialog_still_closes_after_yes(self) -> None:
        """HIGH-2 원본 회귀: 대화상자가 열려 있는 동안 워커가 이미 끝났다면
        (isRunning() 이 False 로 바뀌었다면), Yes 응답 직후 즉시 재확인하여
        창을 닫아야 한다(폴링/고정 타임아웃 없이)."""
        worker = self._make_running_worker()
        self.win.prescan_worker = worker

        def fake_question(*_a, **_k):
            # 대화상자가 열려 있던 동안 워커가 실제로 끝났다고 가정.
            worker.isRunning.return_value = False
            return QMessageBox.StandardButton.Yes

        with patch.object(QMessageBox, "question", side_effect=fake_question):
            event = QCloseEvent()
            self.win.closeEvent(event)

        self.assertTrue(event.isAccepted())
        self.assertFalse(self.win._closing)  # 완전히 닫혔으므로 원상 복귀


class CoupangLoginCoordinationTest(_MainWindowTestCase):
    def _start_fake_login(self) -> Control:
        control = Control()
        worker = MagicMock()
        worker.isRunning.return_value = True
        self.win.category_login_control = control
        self.win.category_login_worker = worker
        return control

    def test_close_requests_login_cancel(self) -> None:
        control = self._start_fake_login()

        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            event = QCloseEvent()
            self.win.closeEvent(event)

        self.assertTrue(control.is_cancelled())
        self.assertFalse(event.isAccepted())

    def test_login_start_disables_other_tabs(self) -> None:
        from app.core.coupang.preflight import PreflightResult, PreflightStatus

        ready = PreflightResult(PreflightStatus.OK, "ok")
        with (
            patch("app.core.coupang.preflight.check_runtime", return_value=ready),
            patch("app.ui.main_window.CoupangLoginWorker") as worker_cls,
        ):
            worker_cls.return_value.isRunning.return_value = True
            self.win.on_category_login()

        self.assertFalse(self.win.btn_prescan.isEnabled())
        self.assertFalse(self.win.coupang_panel.btn_start.isEnabled())
        self.assertFalse(self.win.foodspring_panel.btn_start.isEnabled())
        self.assertTrue(self.win.category_panel.btn_cancel.isEnabled())

    def test_login_start_failure_restores_other_tabs(self) -> None:
        from app.core.coupang.preflight import PreflightResult, PreflightStatus

        ready = PreflightResult(PreflightStatus.OK, "ok")
        with (
            patch("app.core.coupang.preflight.check_runtime", return_value=ready),
            patch("app.ui.main_window.CoupangLoginWorker") as worker_cls,
            patch.object(QMessageBox, "critical") as critical,
        ):
            worker_cls.return_value.start.side_effect = RuntimeError("start failed")
            self.win.on_category_login()

        critical.assert_called_once()
        self.assertIsNone(self.win.category_login_worker)
        self.assertTrue(self.win.btn_prescan.isEnabled())
        self.assertTrue(self.win.coupang_panel.btn_start.isEnabled())
        self.assertTrue(self.win.foodspring_panel.btn_start.isEnabled())
        self.assertEqual(self.win.category_panel._state, "failed")


class OnStartReconcileBlockingTest(_MainWindowTestCase):
    """4차/5차 리뷰 회귀: 체크포인트 승격이 fail-closed 상태를 보고하면
    on_start() 는 CrawlWorker 를 만들지 않고 QMessageBox.critical 로 사용자에게
    알려야 한다."""

    def test_corrupt_checkpoint_blocks_start_with_critical_dialog(self) -> None:
        from app.core.storage import Storage
        from app.models.records import STATUS_COLLECTABLE, PrescanResult

        tmpdir = tempfile.mkdtemp()
        storage = Storage(tmpdir)
        storage.reset_collected_ids()
        # 손상된(의미적으로 잘못된) 체크포인트를 미리 만들어둔다.
        storage._partial_path("cat").write_text('{"broken": true}', encoding="utf-8")

        self.win.settings._output_edit.setText(tmpdir)
        self.win.prescan_results = [
            PrescanResult("cat", "best", 1, 1, 0, ["1"], STATUS_COLLECTABLE)
        ]
        self.win.prescan_table.set_results(self.win.prescan_results)
        self.win._prescan_snapshot = {
            "categories": tuple(c.name for c in self.win.settings.selected_categories()),
            "output_dir": tmpdir,
        }

        with patch.object(QMessageBox, "critical") as critical_mock, \
             patch("app.ui.main_window.CrawlWorker") as worker_cls:
            self.win.on_start()

        critical_mock.assert_called_once()
        worker_cls.assert_not_called()
        self.assertIsNone(self.win.crawl_worker)

    def test_collected_ids_quarantine_failure_blocks_start(self) -> None:
        """6차 리뷰 HIGH-4 회귀: collected_ids.json 이 손상됐고 격리(백업)
        조차 실패하면(원본이 위험한 상태로 남음), on_start() 는 빈 집합으로
        조용히 진행하지 않고 CrawlWorker 를 만들지 않은 채 막아야 한다."""
        from app.core.storage import Storage
        from app.models.records import STATUS_COLLECTABLE, PrescanResult

        tmpdir = tempfile.mkdtemp()
        storage = Storage(tmpdir)
        storage.collected_ids_path.write_text("{broken", encoding="utf-8")

        self.win.settings._output_edit.setText(tmpdir)
        self.win.prescan_results = [
            PrescanResult("cat", "best", 1, 1, 0, ["1"], STATUS_COLLECTABLE)
        ]
        self.win.prescan_table.set_results(self.win.prescan_results)
        self.win._prescan_snapshot = {
            "categories": tuple(c.name for c in self.win.settings.selected_categories()),
            "output_dir": tmpdir,
        }

        with patch.object(Path, "replace", side_effect=OSError("simulated replace failure")), \
             patch.object(Path, "write_bytes", side_effect=OSError("simulated copy failure")), \
             patch.object(QMessageBox, "critical") as critical_mock, \
             patch("app.ui.main_window.CrawlWorker") as worker_cls:
            self.win.on_start()

        critical_mock.assert_called_once()
        worker_cls.assert_not_called()
        self.assertIsNone(self.win.crawl_worker)
        # 원본이 그대로 보존됐어야 한다.
        self.assertTrue(storage.collected_ids_path.exists())
        self.assertEqual(storage.collected_ids_path.read_text(encoding="utf-8"), "{broken")


class ResetCleanupFailureTest(_MainWindowTestCase):
    """3차 리뷰 MEDIUM 회귀: 체크포인트 삭제 실패를 조용히 넘기지 않고
    경고 대화상자로 알려야 한다 — '초기화 완료'로 잘못 표시하면 안 된다."""

    def test_partial_cleanup_failure_shows_warning_not_success(self) -> None:
        from app.core.storage import Storage

        tmpdir = tempfile.mkdtemp()
        storage = Storage(tmpdir)
        storage.save_partial_results([{"goodscode": "1"}], "cat")
        self.win.settings._output_edit.setText(tmpdir)

        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), \
             patch.object(QMessageBox, "warning") as warning_mock, \
             patch("app.core.storage.Storage.clear_all_partials", return_value=[storage._partial_path("cat")]):
            self.win.on_reset()

        warning_mock.assert_called_once()
        self.assertIn("초기화 일부 실패", self.win.statusBar().currentMessage())


class ResetDuringCoupangRunTest(_MainWindowTestCase):
    """외부 리뷰 HIGH 회귀: Coupang 크롤링 중 Gmarket '초기화'가 실행되면
    reset 말미의 _set_ui_state("idle") 이 coupang_panel.set_external_busy(False)
    를 거쳐 실행 중인 패널을 idle 로 덮어써 일시정지/취소 버튼이 죽는다.
    버튼 비활성화(UI 클릭 차단)와 on_reset() 자체 가드(직접 호출/이벤트 경쟁
    차단)를 모두 검증한다."""

    def _start_fake_coupang_run(self) -> None:
        worker = MagicMock()
        worker.isRunning.return_value = True
        self.win.coupang_worker = worker
        self.win._set_gmarket_busy(True)
        self.win.coupang_panel.set_state("running")

    def test_reset_button_disabled_while_coupang_running(self) -> None:
        self._start_fake_coupang_run()
        self.assertFalse(self.win.btn_reset.isEnabled())

    def test_direct_on_reset_is_noop_and_preserves_coupang_controls(self) -> None:
        """버튼이 아닌 경로로 on_reset() 이 호출돼도 dialog/storage 접근 없이
        즉시 반환하고, Coupang 일시정지/취소는 계속 살아 있어야 한다."""
        self._start_fake_coupang_run()

        with patch.object(QMessageBox, "question") as question_mock, \
             patch.object(self.win, "_make_storage") as make_storage:
            self.win.on_reset()

        question_mock.assert_not_called()
        make_storage.assert_not_called()
        self.assertEqual(self.win.coupang_panel._state, "running")
        self.assertTrue(self.win.coupang_panel.btn_pause.isEnabled())
        self.assertTrue(self.win.coupang_panel.btn_cancel.isEnabled())
        self.assertFalse(self.win.coupang_panel.btn_start.isEnabled())

    def test_on_reset_blocked_during_close_prompt(self) -> None:
        self.win._close_prompt_active = True
        with patch.object(QMessageBox, "question") as question_mock, \
             patch.object(self.win, "_make_storage") as make_storage:
            self.win.on_reset()
        question_mock.assert_not_called()
        make_storage.assert_not_called()


class CoupangFinishTest(_MainWindowTestCase):
    """LOW 회귀: Coupang 워커의 finished_crawl 시그널 슬롯
    (_on_coupang_finished / _on_coupang_thread_done)을 직접 호출해, 저장 실패
    시 QMessageBox.critical 이 뜨고 패널 상태가 'failed' 로, 정상 완료 시
    다이얼로그 없이 'finished' 로 전이되는지 실제 UI 로 검증한다 — outcome
    함수만 검사하던 기존 테스트가 놓친 UI adapter 경로다."""

    def _worker_with_summary(self, summary):
        worker = MagicMock()
        worker.isRunning.return_value = False
        worker.summary = summary
        return worker

    def test_save_error_summary_shows_critical_and_failed_state(self) -> None:
        from app.models.coupang_records import CoupangRunSummary

        summary = CoupangRunSummary()
        summary.records = [{"vendor_id": "V1"}]
        summary.json_path = "/tmp/coupang_x.json"
        summary.csv_path = None
        summary.save_error = "CSV 쓰기 실패 (JSON 저장됨: /tmp/coupang_x.json)"
        summary.termination_reason = "token_exhausted"

        self.win.coupang_worker = self._worker_with_summary(summary)

        with patch.object(QMessageBox, "critical") as critical_mock:
            self.win._on_coupang_finished(summary)
            self.win._on_coupang_thread_done()

        critical_mock.assert_called_once()
        # 표시된 메시지에 저장 실패 원인이 담겨야 한다.
        shown = " ".join(str(a) for a in critical_mock.call_args.args)
        self.assertIn("CSV 쓰기 실패", shown)
        self.assertEqual(self.win.coupang_panel._state, "failed")

    def test_success_summary_no_dialog_and_finished_state(self) -> None:
        from app.models.coupang_records import CoupangRunSummary

        summary = CoupangRunSummary()
        summary.records = [{"vendor_id": "V1"}]
        summary.business_info_success = 1
        summary.json_path = "/tmp/coupang_ok.json"
        summary.csv_path = "/tmp/coupang_ok.csv"
        summary.termination_reason = "token_exhausted"

        self.win.coupang_worker = self._worker_with_summary(summary)

        with patch.object(QMessageBox, "critical") as critical_mock:
            self.win._on_coupang_finished(summary)
            self.win._on_coupang_thread_done()

        critical_mock.assert_not_called()
        self.assertEqual(self.win.coupang_panel._state, "finished")
        self.assertIn("Coupang 완료", self.win.statusBar().currentMessage())

    def test_remaining_outcomes(self) -> None:
        """AC-22: cleanup-error / cancelled / error / no-records outcome 전이를
        table-driven 으로 검증해 UI adapter 계약을 완전히 잠근다."""
        from app.models.coupang_records import CoupangRunSummary

        def _cleanup_error() -> CoupangRunSummary:
            s = CoupangRunSummary()
            s.records = [{"vendor_id": "V1"}]
            s.business_info_success = 1
            s.json_path = "/tmp/coupang_c.json"
            s.cleanup_error = "브라우저 잔류 프로세스"
            s.termination_reason = "token_exhausted"
            return s

        def _cancelled() -> CoupangRunSummary:
            s = CoupangRunSummary()
            s.records = [{"vendor_id": "V1"}]
            s.json_path = "/tmp/coupang_p.json"
            s.cancelled = True
            s.termination_reason = "cancelled"
            return s

        def _error() -> CoupangRunSummary:
            s = CoupangRunSummary()
            s.error = "getPromotion API 실패"
            s.termination_reason = "error"
            return s

        def _no_records() -> CoupangRunSummary:
            s = CoupangRunSummary()
            s.termination_reason = "no_items"
            return s

        # (name, factory, expect_critical, expect_state, status_substr)
        cases = [
            ("cleanup_error", _cleanup_error, True, "failed", "cleanup 실패"),
            ("cancelled", _cancelled, False, "finished", "취소"),
            ("error", _error, False, "failed", "Coupang 실패"),
            ("no_records", _no_records, False, "failed", "수집된 상품 없음"),
        ]

        for name, factory, expect_critical, expect_state, status_substr in cases:
            with self.subTest(name):
                summary = factory()
                self.win.coupang_worker = self._worker_with_summary(summary)
                with patch.object(QMessageBox, "critical") as critical_mock:
                    self.win._on_coupang_finished(summary)
                    self.win._on_coupang_thread_done()
                if expect_critical:
                    critical_mock.assert_called_once()
                else:
                    critical_mock.assert_not_called()
                self.assertEqual(self.win.coupang_panel._state, expect_state)
                self.assertIn(status_substr, self.win.statusBar().currentMessage())


if __name__ == "__main__":
    unittest.main()
