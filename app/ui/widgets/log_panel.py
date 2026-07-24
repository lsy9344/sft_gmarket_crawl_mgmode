"""실시간 로그 패널 위젯 (읽기 전용 모노스페이스 텍스트).

WORK_ORDER §7.3 UI 구성 기준. 각 로그 줄 앞에 [HH:MM:SS] 타임스탬프를 붙인다.
"""

from __future__ import annotations

from datetime import datetime

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QGroupBox,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

# 로그 최대 보관 줄 수 (오래된 줄부터 자동 폐기)
_MAX_BLOCKS = 2000


class LogPanel(QWidget):
    """수집 진행 로그를 실시간으로 출력하는 패널."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        group = QGroupBox("실시간 로그")
        group_layout = QVBoxLayout(group)

        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setMaximumBlockCount(_MAX_BLOCKS)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        self._text.setFont(font)
        group_layout.addWidget(self._text)

        root.addWidget(group)

    # ── 공개 API ─────────────────────────────────────────────────────
    def append_log(self, msg: str) -> None:
        """[HH:MM:SS] 타임스탬프를 붙여 한 줄 추가하고 맨 아래로 스크롤."""
        timestamp = datetime.now().astimezone().strftime("%H:%M:%S")
        self._text.appendPlainText(f"[{timestamp}] {msg}")
        scrollbar = self._text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def clear_log(self) -> None:
        self._text.clear()
