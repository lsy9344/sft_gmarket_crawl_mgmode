"""Patchright 2페이지 시험 코어와 Qt 신호를 연결하는 워커."""

from __future__ import annotations

import traceback

from PyQt6.QtCore import QThread, pyqtSignal

from app.core.base import Control
from app.core.coupang.patchright_canary import run_canary


class CoupangPatchrightCanaryWorker(QThread):
    log_message = pyqtSignal(str)
    result_ready = pyqtSignal(object)
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        control: Control,
        category_id: str = "176573",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._control = control
        self._category_id = category_id

    def _on_event(self, state: dict) -> None:
        event = state.get("event")
        if event == "browser_started":
            self.log_message.emit("[Patchright] 윈도우 Chrome을 열었습니다.")
        elif event == "home_loaded":
            self.log_message.emit(
                f"[Patchright] 쿠팡 홈 정상 (HTTP {state.get('home_status')})"
            )
        elif event == "category_loaded":
            self.log_message.emit(
                f"[Patchright] 목록 1페이지 정상 (HTTP {state.get('category_status')})"
            )
        elif event == "live_completed":
            self.log_message.emit(
                f"[Patchright] 접속 시험 성공 — 상품 {len(state.get('products', []))}개 확인"
            )
        elif event == "blocked":
            reference = state.get("reference") or "참조번호 없음"
            self.log_message.emit(f"[Patchright] 접근 거부 감지 — {reference}")
        elif event == "guard_refused":
            self.log_message.emit(f"[Patchright] 안전 중단 — {state.get('reason', '')}")
        elif event == "cancelled":
            self.log_message.emit("[Patchright] 사용자가 시험을 취소했습니다.")
        elif event == "failed":
            self.log_message.emit(f"[Patchright] 시험 실패 — {state.get('error', '')}")

    def run(self) -> None:
        try:
            result = run_canary(
                live_category=self._category_id,
                limit=5,
                control=self._control,
                on_event=self._on_event,
            )
            self.result_ready.emit(result)
        except Exception as error:  # Qt 워커 최상위 예외 격리
            self.log_message.emit(traceback.format_exc())
            self.error_occurred.emit(str(error))
