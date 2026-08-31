"""Coupang 로그인 QThread 워커: login 코어 ↔ Qt signal 브리지."""

from __future__ import annotations

import traceback
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from app.core.base import CancelledError, Control


class CoupangLoginWorker(QThread):
    """영속 프로필 브라우저를 띄워 사용자의 수동 로그인을 대기한다 (별도 세션)."""

    log_message = pyqtSignal(str)
    login_finished = pyqtSignal(bool)  # True=로그인 감지, False=타임아웃/취소
    error_occurred = pyqtSignal(str)

    def __init__(
        self, control: Control, profile_dir: Path | None = None, parent=None
    ) -> None:
        super().__init__(parent)
        self._control = control
        self._profile_dir = profile_dir

    def run(self) -> None:
        from app.core.coupang.login import LoginSession

        try:
            session = LoginSession(
                control=self._control,
                profile_dir=self._profile_dir,
                on_log=self.log_message.emit,
            )
            completed = session.run()
            self.login_finished.emit(completed)
        except CancelledError:
            self.log_message.emit("[로그인] 취소되었습니다 — 세션을 닫습니다.")
            self.login_finished.emit(False)
        except Exception as e:  # noqa: BLE001 - 워커 최상위 예외 격리
            self.log_message.emit(traceback.format_exc())
            self.error_occurred.emit(str(e))
