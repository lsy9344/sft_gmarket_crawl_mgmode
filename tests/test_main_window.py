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


if __name__ == "__main__":
    unittest.main()
