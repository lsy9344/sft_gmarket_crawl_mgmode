"""단일 SellerCollector.exe의 GUI/런타임 설치 모드 분기 테스트."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch

from app import main as app_main


class RuntimeModeDispatchTest(unittest.TestCase):
    def test_frozen_startup_requires_runtime_before_opening_window(self) -> None:
        for ready in (True, False):
            with (
                self.subTest(ready=ready),
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "argv", ["SellerCollector.exe"]),
                patch.object(app_main, "_hide_console_window"),
                patch.object(app_main, "_set_browsers_path"),
                patch.object(app_main, "_install_crash_hooks"),
                patch("app.core.applog.setup_file_logging", return_value=None),
                patch("PyQt6.QtWidgets.QApplication") as application,
                patch("app.ui.runtime_setup.ensure_runtime", return_value=ready) as ensure,
                patch("app.ui.main_window.MainWindow") as window,
            ):
                application.return_value.exec.return_value = 0
                self.assertEqual(app_main.main(), 0 if ready else 1)
                ensure.assert_called_once_with()
                self.assertEqual(window.call_count, 1 if ready else 0)
                self.assertEqual(application.return_value.exec.call_count, 1 if ready else 0)

    def test_ali_only_launch_opens_ali_tab_with_separate_log(self) -> None:
        window = MagicMock()
        tabs = [MagicMock(), window.aliexpress_category_panel, window.brightdata_panel]
        window.tab_widget.count.return_value = len(tabs)
        window.tab_widget.widget.side_effect = tabs
        with (
            patch.object(sys, "argv", ["AliCollector.exe", "--ali-only"]),
            patch.object(app_main, "_hide_console_window"),
            patch.object(app_main, "_set_browsers_path"),
            patch.object(app_main, "_install_crash_hooks"),
            patch("app.core.applog.setup_file_logging", return_value=None) as logging_setup,
            patch("PyQt6.QtWidgets.QApplication") as application,
            patch("app.ui.main_window.MainWindow", return_value=window),
        ):
            application.return_value.exec.return_value = 0
            self.assertEqual(app_main.main(), 0)
        logging_setup.assert_called_once_with(filename="ali_collector.log")
        self.assertEqual(
            [call.args for call in window.tab_widget.setTabVisible.call_args_list],
            [(0, False), (1, True), (2, True)],
        )
        window.tab_widget.setCurrentWidget.assert_called_once_with(window.aliexpress_category_panel)

    def test_normal_launch_does_not_enter_runtime_mode(self) -> None:
        self.assertIsNone(app_main._run_runtime_mode(["SellerCollector.exe"]))

    def test_setup_runtime_dispatches_to_bundled_installer(self) -> None:
        original_argv = sys.argv
        with patch("scripts.setup_coupang_runtime.main", return_value=0) as setup_main:
            result = app_main._run_runtime_mode(
                ["SellerCollector.exe", "--setup-runtime"]
            )

        self.assertEqual(result, 0)
        setup_main.assert_called_once_with()
        self.assertIs(sys.argv, original_argv)

    def test_verify_runtime_maps_to_installer_verify_only(self) -> None:
        observed: list[list[str]] = []

        def fake_setup_main() -> int:
            observed.append(list(sys.argv))
            return 7

        original_argv = sys.argv
        with patch("scripts.setup_coupang_runtime.main", side_effect=fake_setup_main):
            result = app_main._run_runtime_mode(
                ["SellerCollector.exe", "--verify-runtime"]
            )

        self.assertEqual(result, 7)
        self.assertEqual(observed, [["SellerCollector.exe", "--verify-only"]])
        self.assertIs(sys.argv, original_argv)

    def test_conflicting_runtime_modes_fail_without_running_installer(self) -> None:
        with patch("scripts.setup_coupang_runtime.main") as setup_main:
            result = app_main._run_runtime_mode(
                [
                    "SellerCollector.exe",
                    "--setup-runtime",
                    "--verify-runtime",
                ]
            )

        self.assertEqual(result, 2)
        setup_main.assert_not_called()

    def test_main_returns_runtime_exit_code_before_starting_gui(self) -> None:
        with (
            patch.object(app_main, "_run_runtime_mode", return_value=9),
            patch.object(app_main, "_hide_console_window") as hide_console,
        ):
            self.assertEqual(app_main.main(), 9)

        hide_console.assert_not_called()


if __name__ == "__main__":
    unittest.main()
