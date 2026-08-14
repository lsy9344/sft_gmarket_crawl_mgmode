"""CoupangPanel 및 MainWindow 2탭 UI 테스트 (AC-02~05, AC-15~17)."""

import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QTabWidget

from app.ui.coupang_panel import CoupangPanel
from app.ui.main_window import MainWindow

_app = None


def setUpModule():
    global _app
    _app = QApplication.instance() or QApplication(sys.argv)


class CoupangPanelStateTest(unittest.TestCase):
    """AC-05: panel state transitions and config validation."""

    def setUp(self):
        self.panel = CoupangPanel()

    def test_initial_state_idle(self):
        self.assertTrue(self.panel.btn_start.isEnabled())
        self.assertFalse(self.panel.btn_pause.isEnabled())
        self.assertFalse(self.panel.btn_cancel.isEnabled())

    def test_running_state(self):
        self.panel.set_state("running")
        self.assertFalse(self.panel.btn_start.isEnabled())
        self.assertTrue(self.panel.btn_pause.isEnabled())
        self.assertTrue(self.panel.btn_cancel.isEnabled())
        self.assertFalse(self.panel.spin_max_pages.isEnabled())

    def test_paused_state(self):
        self.panel.set_state("paused")
        self.assertFalse(self.panel.btn_start.isEnabled())
        self.assertTrue(self.panel.btn_resume.isEnabled())
        self.assertTrue(self.panel.btn_cancel.isEnabled())

    def test_cancelling_state(self):
        self.panel.set_state("cancelling")
        self.assertFalse(self.panel.btn_start.isEnabled())
        self.assertFalse(self.panel.btn_pause.isEnabled())
        self.assertFalse(self.panel.btn_cancel.isEnabled())

    def test_finished_state(self):
        self.panel.set_state("finished")
        self.assertTrue(self.panel.btn_start.isEnabled())
        self.assertTrue(self.panel.spin_max_pages.isEnabled())

    def test_external_busy(self):
        self.panel.set_external_busy(True)
        self.assertFalse(self.panel.btn_start.isEnabled())
        self.assertFalse(self.panel.spin_max_pages.isEnabled())
        self.panel.set_external_busy(False)
        self.assertTrue(self.panel.btn_start.isEnabled())

    def test_append_log_prefixes_timestamp(self):
        """진단성 회귀: Coupang 로그도 Gmarket 처럼 [HH:MM:SS] 을 붙여야
        지연/행 구간을 로그로 추적할 수 있다."""
        self.panel.append_log("테스트 메시지")
        self.assertRegex(
            self.panel.log_area.toPlainText(),
            r"^\[\d{2}:\d{2}:\d{2}\] 테스트 메시지$",
        )

    def test_build_config_no_dir_returns_none(self):
        self.panel.output_dir_edit.setText("")
        self.assertIsNone(self.panel.build_config())

    def test_build_config_valid(self):
        self.panel.output_dir_edit.setText("/tmp/test_out")
        self.panel.spin_max_pages.setValue(5)
        config = self.panel.build_config()
        self.assertIsNotNone(config)
        self.assertEqual(config.max_scroll_pages, 5)


class MainWindowTabTest(unittest.TestCase):
    """AC-02, AC-03, AC-04: 2-tab structure and Gmarket preservation."""

    def setUp(self):
        self.win = MainWindow()

    def tearDown(self):
        self.win.close()

    def test_has_three_tabs(self):
        """AC-02: QTabWidget with Gmarket, Coupang, Coupang 검색 tabs."""
        self.assertIsInstance(self.win.tab_widget, QTabWidget)
        self.assertEqual(self.win.tab_widget.count(), 3)
        self.assertEqual(self.win.tab_widget.tabText(0), "Gmarket")
        self.assertEqual(self.win.tab_widget.tabText(1), "Coupang")
        self.assertEqual(self.win.tab_widget.tabText(2), "Coupang 검색")

    def test_window_title_platform_neutral(self):
        """AC-02: platform-neutral title."""
        self.assertIn("판매자 정보 수집기", self.win.windowTitle())

    def test_gmarket_buttons_exist(self):
        """AC-03: existing Gmarket buttons preserved."""
        self.assertTrue(hasattr(self.win, "btn_prescan"))
        self.assertTrue(hasattr(self.win, "btn_start"))
        self.assertTrue(hasattr(self.win, "btn_pause"))
        self.assertTrue(hasattr(self.win, "btn_cancel"))
        self.assertTrue(hasattr(self.win, "btn_resume_run"))
        self.assertTrue(hasattr(self.win, "btn_reset"))

    def test_gmarket_initial_state(self):
        """AC-03: Gmarket initial button states unchanged."""
        self.assertTrue(self.win.btn_prescan.isEnabled())
        self.assertFalse(self.win.btn_start.isEnabled())
        self.assertFalse(self.win.btn_pause.isEnabled())
        self.assertFalse(self.win.btn_cancel.isEnabled())

    def test_coupang_panel_exists(self):
        """AC-04: Coupang panel exists and app starts without Camoufox."""
        self.assertIsInstance(self.win.coupang_panel, CoupangPanel)

    def test_cross_tab_busy_blocks_coupang(self):
        """AC-15: Gmarket running blocks Coupang start."""
        self.win._set_ui_state("crawling")
        self.assertFalse(self.win.coupang_panel.btn_start.isEnabled())

    def test_cross_tab_busy_blocks_gmarket(self):
        """AC-15: Coupang running blocks Gmarket start."""
        self.win._set_gmarket_busy(True)
        self.assertFalse(self.win.btn_prescan.isEnabled())
        self.assertFalse(self.win.btn_start.isEnabled())
        self.assertFalse(self.win.btn_resume_run.isEnabled())
        self.assertFalse(self.win.btn_reset.isEnabled())


class MainWindowCloseTest(unittest.TestCase):
    """AC-16, AC-17: close coordination."""

    def setUp(self):
        self.win = MainWindow()

    def tearDown(self):
        self.win._closing = False
        self.win._close_prompt_active = False
        self.win.close()

    def test_close_prompt_blocks_new_worker(self):
        """AC-16: no new worker while close dialog is open."""
        self.win._close_prompt_active = True
        self.win.on_coupang_start()
        self.assertIsNone(self.win.coupang_worker)

    def test_closing_blocks_new_worker(self):
        """AC-16: no new worker during closing."""
        self.win._closing = True
        self.win.on_coupang_start()
        self.assertIsNone(self.win.coupang_worker)


if __name__ == "__main__":
    unittest.main()
