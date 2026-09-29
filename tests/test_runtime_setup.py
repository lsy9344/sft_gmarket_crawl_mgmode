"""다운로드 없이 자동 설치의 화면 전환과 실패 복구를 검증한다."""

import os
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QProcess, QTimer
from PyQt6.QtWidgets import QApplication, QDialog

from app.ui.runtime_setup import RuntimeSetupDialog


class RuntimeSetupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.timer_patch = patch("app.ui.runtime_setup.QTimer.singleShot")
        self.timer_patch.start()
        self.dialog = RuntimeSetupDialog()
        self.start_patch = patch.object(self.dialog.process, "start")
        self.start = self.start_patch.start()
        self.output_patch = patch.object(self.dialog.process, "readAllStandardOutput", return_value=b"")
        self.output_patch.start()
        self.dialog._check()

    def tearDown(self):
        self.dialog._busy = False
        self.dialog.close()
        self.start_patch.stop()
        self.output_patch.stop()
        self.timer_patch.stop()

    def test_ready_pc_opens_without_installing(self):
        self.dialog._finished(0, QProcess.ExitStatus.NormalExit)
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Accepted)
        self.start.assert_called_once_with(sys.executable, ["--verify-runtime"])

    def test_missing_runtime_installs_then_opens(self):
        self.dialog._finished(1, QProcess.ExitStatus.NormalExit)
        self.start.assert_called_with(sys.executable, ["--setup-runtime"])
        self.assertIn("자동으로 설치", self.dialog.status.text())
        self.assertTrue(self.dialog._busy)
        self.dialog._finished(0, QProcess.ExitStatus.NormalExit)
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Accepted)

    def test_install_failure_allows_retry_and_rechecks(self):
        self.dialog._finished(1, QProcess.ExitStatus.NormalExit)
        self.dialog._finished(1, QProcess.ExitStatus.NormalExit)
        self.assertFalse(self.dialog._busy)
        self.assertFalse(self.dialog.retry.isHidden())
        self.assertTrue(self.dialog.close_button.isEnabled())
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Rejected)
        self.dialog.retry.click()
        self.start.assert_called_with(sys.executable, ["--verify-runtime"])
        self.assertTrue(self.dialog._busy)

    def test_crashed_check_does_not_start_installation(self):
        self.dialog._finished(1, QProcess.ExitStatus.CrashExit)
        self.assertEqual(self.start.call_count, 1)
        self.assertFalse(self.dialog._busy)

    def test_failed_start_allows_exit(self):
        self.dialog._process_error(QProcess.ProcessError.FailedToStart)
        self.assertFalse(self.dialog._busy)
        self.assertTrue(self.dialog.close_button.isEnabled())

    def test_cancel_stops_only_owned_windows_process_tree(self):
        with (
            patch.object(QDialog, "reject") as reject,
            patch.object(sys, "platform", "win32"),
            patch.object(self.dialog.process, "processId", return_value=12345),
            patch.object(self.dialog.stopper, "start") as stop,
        ):
            self.dialog.reject()
            reject.assert_not_called()
            self.assertEqual(stop.call_args.args[1], ["/PID", "12345", "/T", "/F"])
            self.assertFalse(self.dialog.close_button.isEnabled())
            self.dialog._finished(1, QProcess.ExitStatus.CrashExit)
            self.assertIn("중단했습니다", self.dialog.status.text())
            self.assertEqual(self.start.call_count, 1)
            self.dialog.reject()
            reject.assert_called_once()

    def test_cancel_tool_failure_allows_another_attempt(self):
        self.dialog._cancelled = True
        with patch.object(self.dialog.process, "state", return_value=QProcess.ProcessState.Running):
            self.dialog._stop_error(QProcess.ProcessError.FailedToStart)
        self.assertTrue(self.dialog._busy)
        self.assertTrue(self.dialog._cancelled)
        self.assertTrue(self.dialog.close_button.isEnabled())

    def test_cancel_then_retry_can_install_and_open(self):
        self.dialog._cancelled = True
        self.dialog._show_cancelled()
        self.dialog.retry.click()
        self.dialog._finished(1, QProcess.ExitStatus.NormalExit)
        self.start.assert_called_with(sys.executable, ["--setup-runtime"])
        self.dialog._finished(0, QProcess.ExitStatus.NormalExit)
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Accepted)

    def test_cancel_during_start_waits_for_owned_pid(self):
        with (
            patch.object(self.dialog.process, "processId", return_value=0),
            patch.object(self.dialog.process, "state", return_value=QProcess.ProcessState.Starting),
        ):
            self.dialog.reject()
        self.assertTrue(self.dialog._busy)
        self.assertTrue(self.dialog._cancelled)
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(self.dialog.process, "processId", return_value=12345),
            patch.object(self.dialog.stopper, "start") as stop,
        ):
            self.dialog._started()
        self.assertEqual(stop.call_args.args[1], ["/PID", "12345", "/T", "/F"])

    def test_successful_stop_waits_for_installer_finished_signal(self):
        self.dialog._cancelled = True
        with patch.object(self.dialog.process, "state", return_value=QProcess.ProcessState.Running):
            self.dialog._stop_finished(0)
        self.assertTrue(self.dialog._busy)
        self.assertTrue(self.dialog._cancelled)
        self.dialog._finished(0, QProcess.ExitStatus.NormalExit)
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Rejected)
        self.assertFalse(self.dialog._busy)

    def test_split_utf8_output_is_preserved(self):
        data = "설치 중".encode()
        with patch.object(self.dialog.process, "readAllStandardOutput", side_effect=[data[:2], data[2:]]):
            self.dialog._read_output()
            self.dialog._read_output()
        self.assertEqual(self.dialog.details.toPlainText(), "설치 중")

    def test_real_child_process_output_and_completion(self):
        self.start_patch.stop()
        self.output_patch.stop()
        # 실제 비동기 프로세스와 Qt 이벤트 처리를 검증하되 다운로드는 하지 않는다.
        self.dialog.process.start(sys.executable, ["-c", "print('ready', flush=True)"])
        self.timer_patch.stop()
        timer = QTimer(self.dialog)
        timer.setSingleShot(True)
        timer.timeout.connect(self.dialog._failed)
        timer.timeout.connect(self.dialog.reject)
        timer.start(5000)
        try:
            self.assertEqual(self.dialog.exec(), QDialog.DialogCode.Accepted)
        finally:
            timer.stop()
        self.assertIn("ready", self.dialog.details.toPlainText())


if __name__ == "__main__":
    unittest.main()
