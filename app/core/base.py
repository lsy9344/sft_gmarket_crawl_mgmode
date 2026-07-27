"""엔진 공통: 취소/일시정지 제어 및 콜백 기본형.

엔진(prescan/crawler)은 Qt 에 의존하지 않는다. 워커가 Control 을 구현하고
콜백으로 시그널을 emit 한다.
"""

from __future__ import annotations

import threading
import time


class CancelledError(Exception):
    """사용자 취소로 수집을 중단할 때 발생."""


class Control:
    """취소/일시정지 제어. 스레드 이벤트 기반, Qt 비의존.

    - request_cancel(): 취소 요청
    - pause()/resume(): 일시정지/재개
    - is_cancelled(): 취소 여부
    - checkpoint(): 일시정지면 대기, 취소면 CancelledError 발생
                    (현재 건 완료 후 호출하여 '건 단위 안전 중단' 보장)
    """

    def __init__(self) -> None:
        self._cancel = threading.Event()
        self._resume = threading.Event()
        self._resume.set()  # 기본: 실행(정지 아님)

    def request_cancel(self) -> None:
        self._cancel.set()
        self._resume.set()  # 정지 상태여도 즉시 빠져나오도록

    def pause(self) -> None:
        self._resume.clear()

    def resume(self) -> None:
        self._resume.set()

    def is_paused(self) -> bool:
        return not self._resume.is_set()

    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    def wait_if_paused(self, poll: float = 0.1) -> None:
        """일시정지 상태이면 재개/취소까지 대기."""
        while not self._resume.wait(poll):
            if self._cancel.is_set():
                return

    def checkpoint(self) -> None:
        """건 경계에서 호출: 정지 시 대기, 취소 시 CancelledError."""
        self.wait_if_paused()
        if self._cancel.is_set():
            raise CancelledError()

    def sleep(self, seconds: float, poll_interval: float = 0.1) -> None:
        """중단 가능한 분할 대기. 취소 시 CancelledError, 일시정지 시 재개까지 대기."""
        end = time.monotonic() + seconds
        while True:
            self.wait_if_paused(poll_interval)
            if self._cancel.is_set():
                raise CancelledError()
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(poll_interval, remaining))
