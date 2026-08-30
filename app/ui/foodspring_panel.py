"""Foodspring 탭 패널: 설정/버튼/진행/로그/결과."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import (
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.applog import log_line
from app.models.foodspring_records import FoodSpringRunConfig

DISPLAY_COLUMNS = (
    "seller_id",
    "store_name",
    "owner_name",
    "business_number",
    "phone",
    "email",
    "address",
    "ecommerce_report_number",
)

COLUMN_LABELS = {
    "seller_id": "셀러ID",
    "store_name": "셀러명(스토어)",
    "owner_name": "대표자명",
    "business_number": "사업자등록번호",
    "phone": "연락처",
    "email": "이메일",
    "address": "사업장 소재지",
    "ecommerce_report_number": "통신판매 신고번호",
}


class FoodSpringPanel(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._state = "idle"
        self._build_ui()
        self._apply_state()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        info = QLabel(
            "대상: foodspring.co.kr/special/wcpd (전국 택배 배송 — 상품 전체 + 판매자 사업자정보)"
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        settings_group = QGroupBox("실행 설정")
        form = QFormLayout(settings_group)

        dir_row = QHBoxLayout()
        self.output_dir_edit = QLineEdit()
        self.output_dir_edit.setPlaceholderText("출력 폴더 선택...")
        self.btn_browse = QPushButton("찾아보기")
        self.btn_browse.clicked.connect(self._browse_output_dir)
        dir_row.addWidget(self.output_dir_edit)
        dir_row.addWidget(self.btn_browse)
        form.addRow("출력 폴더:", dir_row)

        self.spin_max_pages = QSpinBox()
        self.spin_max_pages.setRange(1, 999)
        self.spin_max_pages.setValue(200)
        form.addRow("목록 최대 페이지:", self.spin_max_pages)

        self.spin_workers = QSpinBox()
        self.spin_workers.setRange(1, 8)
        self.spin_workers.setValue(3)
        form.addRow("동시 작업 수:", self.spin_workers)

        self.spin_delay_min = QDoubleSpinBox()
        self.spin_delay_min.setRange(0, 60)
        self.spin_delay_min.setValue(0.8)
        self.spin_delay_min.setSingleStep(0.5)
        form.addRow("지연 최소 (초):", self.spin_delay_min)

        self.spin_delay_max = QDoubleSpinBox()
        self.spin_delay_max.setRange(0, 60)
        self.spin_delay_max.setValue(2.0)
        self.spin_delay_max.setSingleStep(0.5)
        form.addRow("지연 최대 (초):", self.spin_delay_max)

        self.prefix_edit = QLineEdit()
        self.prefix_edit.setPlaceholderText("선택 (비우면 자동)")
        form.addRow("출력 prefix:", self.prefix_edit)

        layout.addWidget(settings_group)

        btn_row = QHBoxLayout()
        self.btn_start = QPushButton("수집 시작")
        self.btn_pause = QPushButton("일시정지")
        self.btn_resume = QPushButton("재개")
        self.btn_cancel = QPushButton("취소")
        self.btn_open_result = QPushButton("결과 열기")
        for b in (self.btn_start, self.btn_pause, self.btn_resume, self.btn_cancel, self.btn_open_result):
            btn_row.addWidget(b)
        layout.addLayout(btn_row)

        self.lbl_phase = QLabel("대기 중")
        self.lbl_progress = QLabel("")
        self.lbl_stats = QLabel("")
        layout.addWidget(self.lbl_phase)
        layout.addWidget(self.lbl_progress)
        layout.addWidget(self.lbl_stats)

        self.log_area = QPlainTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setMaximumBlockCount(2000)
        layout.addWidget(self.log_area, stretch=1)

        self.table = QTableWidget()
        self.table.setColumnCount(len(DISPLAY_COLUMNS))
        self.table.setHorizontalHeaderLabels(
            [COLUMN_LABELS[c] for c in DISPLAY_COLUMNS]
        )
        header = self.table.horizontalHeader()
        assert header is not None
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, stretch=2)

    def _browse_output_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "출력 폴더 선택")
        if d:
            self.output_dir_edit.setText(d)

    # ── 상태 관리 ──────────────────────────────────────────────────
    def set_state(self, state: str) -> None:
        self._state = state
        self._apply_state()

    def _apply_state(self) -> None:
        s = self._state
        self.btn_start.setEnabled(s in ("idle", "finished", "failed"))
        self.btn_pause.setEnabled(s == "running")
        self.btn_resume.setEnabled(s == "paused")
        self.btn_cancel.setEnabled(s in ("running", "paused"))
        self.btn_open_result.setEnabled(s in ("idle", "finished", "failed"))
        settings_enabled = s in ("idle", "finished", "failed")
        self.spin_max_pages.setEnabled(settings_enabled)
        self.spin_workers.setEnabled(settings_enabled)
        self.spin_delay_min.setEnabled(settings_enabled)
        self.spin_delay_max.setEnabled(settings_enabled)
        self.prefix_edit.setEnabled(settings_enabled)
        self.output_dir_edit.setEnabled(settings_enabled)
        self.btn_browse.setEnabled(settings_enabled)

    def set_external_busy(self, busy: bool) -> None:
        if busy:
            self._state = "external_busy"
            self.btn_start.setEnabled(False)
            self.btn_pause.setEnabled(False)
            self.btn_resume.setEnabled(False)
            self.btn_cancel.setEnabled(False)
            self.spin_max_pages.setEnabled(False)
            self.spin_workers.setEnabled(False)
            self.spin_delay_min.setEnabled(False)
            self.spin_delay_max.setEnabled(False)
            self.prefix_edit.setEnabled(False)
            self.output_dir_edit.setEnabled(False)
            self.btn_browse.setEnabled(False)
        else:
            self._state = "idle"
            self._apply_state()

    # ── 설정 검증 및 생성 ──────────────────────────────────────────
    def build_config(self) -> FoodSpringRunConfig | None:
        out_dir = self.output_dir_edit.text().strip()
        if not out_dir:
            return None
        prefix = self.prefix_edit.text().strip() or None
        try:
            return FoodSpringRunConfig(
                output_dir=Path(out_dir),
                output_prefix=prefix,
                max_pages=self.spin_max_pages.value(),
                workers=self.spin_workers.value(),
                delay_min=self.spin_delay_min.value(),
                delay_max=self.spin_delay_max.value(),
            )
        except ValueError:
            return None

    # ── UI 갱신 슬롯 ──────────────────────────────────────────────
    def append_log(self, msg: str) -> None:
        log_line(f"[Foodspring] {msg}")
        timestamp = datetime.now().astimezone().strftime("%H:%M:%S")
        self.log_area.appendPlainText(f"[{timestamp}] {msg}")

    def set_phase(self, name: str, current: int, total: int) -> None:
        self.lbl_phase.setText(f"단계 {current}/{total}: {name}")

    def set_progress_text(self, text: str) -> None:
        self.lbl_progress.setText(text)

    def set_stats_text(self, text: str) -> None:
        self.lbl_stats.setText(text)

    def add_record(self, record: dict) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        for col, key in enumerate(DISPLAY_COLUMNS):
            val = record.get(key, "")
            if isinstance(val, bool):
                val = "O" if val else ""
            self.table.setItem(row, col, QTableWidgetItem(str(val)))

    def clear_results(self) -> None:
        self.table.setRowCount(0)
        self.log_area.clear()
        self.lbl_phase.setText("대기 중")
        self.lbl_progress.setText("")
        self.lbl_stats.setText("")