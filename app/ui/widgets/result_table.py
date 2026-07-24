"""수집 결과 미리보기 테이블 위젯 (판매자 레코드 실시간 추가).

WORK_ORDER §7.3 UI 구성 기준. SellerRecord dict 를 최근 순으로 표시한다.
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

_COLUMNS = ["스토어명", "상호명", "이메일", "전화", "사업자번호"]
# dict 키 → 컬럼 순서
_KEYS = ["store_name", "company_name", "email", "phone", "business_number"]

# 최근 보관 최대 행 수 (초과 시 가장 오래된 행 제거)
_MAX_ROWS = 500


class ResultTable(QWidget):
    """수집된 판매자 정보를 실시간으로 누적 표시하는 미리보기 테이블."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        group = QGroupBox("수집 결과 미리보기")
        group_layout = QVBoxLayout(group)

        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        group_layout.addWidget(self._table)

        root.addWidget(group)

    # ── 공개 API ─────────────────────────────────────────────────────
    def add_record(self, record: dict) -> None:
        """레코드 1건을 마지막 행으로 추가. 최대 행 수 초과 시 가장 오래된 행 제거."""
        row = self._table.rowCount()
        self._table.insertRow(row)
        for col, key in enumerate(_KEYS):
            value = record.get(key, "")
            self._table.setItem(row, col, QTableWidgetItem(str(value)))

        # 오래된 행 정리
        while self._table.rowCount() > _MAX_ROWS:
            self._table.removeRow(0)

        self._table.scrollToBottom()

    def clear_records(self) -> None:
        self._table.setRowCount(0)
