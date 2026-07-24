"""사전 조사(Phase 0) 결과 테이블 위젯.

WORK_ORDER §7.3 UI 구성 기준. PrescanResult 목록을 표로 렌더링한다.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.utils.helpers import estimate_seconds, format_duration

_COLUMNS = ["#", "카테고리", "전체 상품", "신규 대상", "이미수집", "상태"]

# 상태 라벨 → 표시 색상
_STATUS_COLORS = {
    "수집 가능": QColor(0, 150, 60),    # green
    "완료됨": QColor(130, 130, 130),    # gray
    "상품 없음": QColor(200, 40, 40),   # red
    "차단/오류": QColor(210, 130, 0),   # orange — 진짜 빈 카테고리와 구분
}


class PrescanTable(QWidget):
    """사전 조사 결과 표 + 합계 요약 라벨."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # 마지막으로 표시한 결과 (total_new 계산용)
        self._results: list = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        group = QGroupBox("사전 조사 결과")
        group_layout = QVBoxLayout(group)

        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        group_layout.addWidget(self._table)

        self._summary_label = QLabel("")
        group_layout.addWidget(self._summary_label)

        root.addWidget(group)

    # ── 공개 API ─────────────────────────────────────────────────────
    def set_results(self, results: list) -> None:
        """PrescanResult 목록으로 표를 채우고 합계 요약을 갱신한다."""
        self._results = list(results)
        self._table.setRowCount(0)

        total_sum = 0
        new_sum = 0
        already_sum = 0

        for index, result in enumerate(results, start=1):
            row = self._table.rowCount()
            self._table.insertRow(row)

            total_sum += result.total_codes
            new_sum += result.new_codes
            already_sum += result.already_collected

            self._table.setItem(row, 0, self._int_item(index))
            self._table.setItem(row, 1, QTableWidgetItem(result.category_name))
            self._table.setItem(row, 2, self._int_item(result.total_codes))
            self._table.setItem(row, 3, self._int_item(result.new_codes))
            self._table.setItem(row, 4, self._int_item(result.already_collected))

            status_item = QTableWidgetItem(result.status_label)
            color = _STATUS_COLORS.get(result.status_label)
            if color is not None:
                status_item.setForeground(color)
            self._table.setItem(row, 5, status_item)

        self._summary_label.setText(
            f"합계: {total_sum:,}건  |  신규: {new_sum:,}건  |  "
            f"이미수집: {already_sum:,}건  |  "
            f"예상 소요 시간: {format_duration(estimate_seconds(new_sum))}"
        )

    def clear_results(self) -> None:
        """표와 요약 라벨을 초기화."""
        self._results = []
        self._table.setRowCount(0)
        self._summary_label.setText("")

    def total_new(self) -> int:
        """마지막으로 설정된 결과들의 신규 건수 합계 (없으면 0)."""
        return sum(result.new_codes for result in self._results)

    # ── 내부 헬퍼 ────────────────────────────────────────────────────
    @staticmethod
    def _int_item(value: int) -> QTableWidgetItem:
        """정수를 우측 정렬로 표시하는 셀 아이템."""
        item = QTableWidgetItem(str(int(value)))
        item.setTextAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        return item
