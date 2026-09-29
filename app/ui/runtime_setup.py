"""배포 EXE의 첫 실행 시 필요한 브라우저를 자동으로 준비한다."""

from __future__ import annotations

import codecs
import os
import sys

from PyQt6.QtCore import QProcess, QProcessEnvironment, QTimer
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar,
    QPushButton, QVBoxLayout,
)


class RuntimeSetupDialog(QDialog):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("앱 시작 준비")
        self.resize(580, 360)
        self.status = QLabel("필요한 도구가 설치되어 있는지 확인하고 있습니다.")
        self.status.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setAccessibleName("설치 진행 내용")
        self.details.setMaximumBlockCount(1000)
        self.retry = QPushButton("다시 시도")
        self.retry.hide()
        self.retry.clicked.connect(self._check)
        self.close_button = QPushButton("중단")
        self.close_button.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self.retry)
        buttons.addWidget(self.close_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(self.progress)
        layout.addWidget(self.details)
        layout.addLayout(buttons)
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        self.process.setProcessEnvironment(env)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.started.connect(self._started)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self.stopper = QProcess(self)
        self.stopper.finished.connect(self._stop_finished)
        self.stopper.errorOccurred.connect(self._stop_error)
        self._cancelled = False
        self._mode = "--verify-runtime"
        self._busy = True
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        QTimer.singleShot(0, self._check)

    def _check(self) -> None:
        self.status.setText("필요한 도구가 설치되어 있는지 확인하고 있습니다.")
        self.details.clear()
        self._start("--verify-runtime")

    def _start(self, mode: str) -> None:
        self._mode = mode
        self._busy = True
        self._cancelled = False
        self._decoder.reset()
        self.retry.hide()
        self.close_button.setText("중단")
        self.close_button.setEnabled(True)
        self.progress.setRange(0, 0)
        # 같은 EXE의 설치 모드로 실행하므로 별도 설치 파일/Python이 필요 없다.
        self.process.start(sys.executable, [mode])

    def _read_output(self) -> None:
        text = self._decoder.decode(bytes(self.process.readAllStandardOutput()))
        if text:
            cursor = self.details.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            cursor.insertText(text.replace("\r", "\n"))
            self.details.setTextCursor(cursor)
            self.details.ensureCursorVisible()

    def _started(self) -> None:
        if self._cancelled:
            self._cancelled = False
            self.reject()

    def _finished(self, code: int, status: QProcess.ExitStatus) -> None:
        self._read_output()
        if self._cancelled:
            if self.stopper.state() == QProcess.ProcessState.NotRunning:
                self._show_cancelled()
            return
        if status != QProcess.ExitStatus.NormalExit:
            self._failed()
        elif code == 0:
            self._busy = False
            self.accept()
        elif self._mode == "--verify-runtime":
            self.status.setText(
                "필요한 도구를 자동으로 설치하고 있습니다.\n"
                "처음에는 약 1.4GB를 내려받습니다. 인터넷을 연결하고 잠시 기다려 주세요.\n"
                "설치가 끝나면 앱이 자동으로 열립니다."
            )
            self._start("--setup-runtime")
        else:
            self._failed()

    def _process_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self.details.appendPlainText(self.process.errorString())
            self._failed()

    def _failed(self) -> None:
        self._busy = False
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.status.setText(
            "준비를 마치지 못했습니다. 인터넷 연결과 남은 저장 공간을 확인한 뒤\n"
            "‘다시 시도’를 눌러 주세요. 아래에서 자세한 내용을 확인할 수 있습니다."
        )
        self.retry.show()
        self.close_button.setText("닫기")
        self.close_button.setEnabled(True)

    def _show_cancelled(self) -> None:
        self._failed()
        self.status.setText("준비를 중단했습니다. ‘다시 시도’를 누르면 다시 준비합니다.")

    def _stop_finished(self, code: int, *_args) -> None:
        if self.process.state() == QProcess.ProcessState.NotRunning:
            self._show_cancelled()
        elif code != 0:
            self.status.setText("아직 중단하지 못했습니다. 잠시 후 ‘중단’을 다시 눌러 주세요.")
            self.close_button.setEnabled(True)

    def _stop_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self._stop_finished(-1)

    def reject(self) -> None:
        if not self._busy:
            super().reject()
        elif not self._cancelled or self.stopper.state() == QProcess.ProcessState.NotRunning:
            self._cancelled = True
            self.status.setText("준비를 중단하고 있습니다. 잠시 기다려 주세요.")
            self.close_button.setEnabled(False)
            pid = self.process.processId()
            if not pid:
                if self.process.state() == QProcess.ProcessState.NotRunning:
                    self._show_cancelled()
            elif sys.platform == "win32":
                # 오직 이 창에서 실행한 설치기 PID와 그 하위 다운로드만 종료한다.
                taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
                self.stopper.start(taskkill, ["/PID", str(pid), "/T", "/F"])
            else:
                self.process.kill()

    def closeEvent(self, event) -> None:
        if self._busy:
            event.ignore()
            self.reject()
        else:
            super().closeEvent(event)


def ensure_runtime() -> bool:
    return RuntimeSetupDialog().exec() == QDialog.DialogCode.Accepted
