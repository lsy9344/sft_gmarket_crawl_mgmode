"""단일 SellerCollector.exe의 GUI/런타임 설치 모드 분기 테스트."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from app import main as app_main


class RuntimeModeDispatchTest(unittest.TestCase):
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
