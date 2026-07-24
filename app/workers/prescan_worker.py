"""Phase 0 사전 조사 워커: QThread ↔ Qt 비의존 엔진 브리지 (WORK_ORDER §7.2).

PrescanWorker 는 백그라운드 스레드에서 Prescanner.run 을 실행하고,
엔진 콜백을 pyqtSignal 로 UI 스레드에 중계한다. Control 은 메인 윈도우가
생성/소유하여 UI 스레드에서 취소/일시정지를 제어하므로 여기서 만들지 않는다.
"""

from __future__ import annotations

import traceback

from PyQt6.QtCore import QThread, pyqtSignal

from app.core.base import CancelledError, Control
from app.core.config import CategoryDef
from app.core.prescan import Prescanner
from app.core.storage import Storage


class PrescanWorker(QThread):
    """Phase 0 사전 조사를 백그라운드로 실행하는 QThread 워커."""

    # (category_name, current, total)
    prescan_progress = pyqtSignal(str, int, int)
    # list[PrescanResult]
    prescan_complete = pyqtSignal(list)
    log_message = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        storage: Storage,
        categories: list[CategoryDef],
        control: Control,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.storage = storage
        self.categories = categories
        # Control 은 caller(메인 윈도우) 소유 — 절대 새로 만들지 않는다.
        self.control = control

    def run(self) -> None:
        try:
            prescanner = Prescanner(
                self.storage,
                control=self.control,
                on_log=self.log_message.emit,
                on_progress=self.prescan_progress.emit,
            )
            results = prescanner.run(self.categories)
            self.prescan_complete.emit(results)
        except CancelledError:
            self.log_message.emit("[Pre-scan] 취소됨")
            # UI 가 버튼을 다시 활성화할 수 있도록 빈 결과 전달
            self.prescan_complete.emit([])
        except Exception as e:  # noqa: BLE001 - 워커 경계에서 모든 예외 포착
            self.log_message.emit(traceback.format_exc())
            self.error_occurred.emit(str(e))
