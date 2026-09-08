"""Coupang 탭 패널: 설정/버튼/진행/로그/결과 (WORK_ORDER §10)."""

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
from app.models.coupang_records import CoupangRunConfig

DISPLAY_COLUMNS = (
    "vendor_id",
    "store_name",
    "company_name",
    "ceo_name",
    "business_number",
    "phone",
    "email",
    "power_seller",
)

COLUMN_LABELS = {
    "vendor_id": "판매자ID",
    "store_name": "스토어명",
    "company_name": "상호명",
    "ceo_name": "대표자",
    "business_number": "사업자번호",
    "phone": "전화",
    "email": "이메일",
    "power_seller": "파워셀러",
}


class CoupangPanel(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._state = "idle"
        self._build_ui()
        self._apply_state()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # 대상 안내
        info = QLabel("대상: coupang.com/np/omp '전체' 탭 (카테고리 선택 없음)")
        layout.addWidget(info)

        # 실행 설정
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
        self.spin_max_pages.setValue(10)
        form.addRow("최대 피드 페이지:", self.spin_max_pages)

        self.spin_batch_size = QSpinBox()
        self.spin_batch_size.setRange(1, 100)
        self.spin_batch_size.setValue(10)
        form.addRow("배치 크기:", self.spin_batch_size)

        self.spin_warmup = QDoubleSpinBox()
        self.spin_warmup.setRange(0, 300)
        self.spin_warmup.setValue(20)
        form.addRow("웜업 (초):", self.spin_warmup)

        self.spin_delay_min = QDoubleSpinBox()
        self.spin_delay_min.setRange(0, 60)
        self.spin_delay_min.setValue(2.5)
        self.spin_delay_min.setSingleStep(0.5)
        form.addRow("지연 최소 (초):", self.spin_delay_min)

        self.spin_delay_max = QDoubleSpinBox()
        self.spin_delay_max.setRange(0, 60)
        self.spin_delay_max.setValue(3.5)
        self.spin_delay_max.setSingleStep(0.5)
        form.addRow("지연 최대 (초):", self.spin_delay_max)

        self.prefix_edit = QLineEdit()
        self.prefix_edit.setPlaceholderText("선택 (비우면 자동)")
        form.addRow("출력 prefix:", self.prefix_edit)

        layout.addWidget(settings_group)

        # 버튼
        btn_row = QHBoxLayout()
        self.btn_start = QPushButton("수집 시작")
        self.btn_pause = QPushButton("일시정지")
        self.btn_resume = QPushButton("재개")
        self.btn_cancel = QPushButton("취소")
        self.btn_open_result = QPushButton("결과 열기")
        for b in (self.btn_start, self.btn_pause, self.btn_resume, self.btn_cancel, self.btn_open_result):
            btn_row.addWidget(b)
        layout.addLayout(btn_row)

        # 진행 표시
        self.lbl_phase = QLabel("대기 중")
        self.lbl_progress = QLabel("")
        self.lbl_stats = QLabel("")
        layout.addWidget(self.lbl_phase)
        layout.addWidget(self.lbl_progress)
        layout.addWidget(self.lbl_stats)

        # 로그
        self.log_area = QPlainTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setMaximumBlockCount(2000)
        layout.addWidget(self.log_area, stretch=1)

        # 결과 테이블
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
        self.spin_batch_size.setEnabled(settings_enabled)
        self.spin_warmup.setEnabled(settings_enabled)
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
            self.spin_batch_size.setEnabled(False)
            self.spin_warmup.setEnabled(False)
            self.spin_delay_min.setEnabled(False)
            self.spin_delay_max.setEnabled(False)
            self.prefix_edit.setEnabled(False)
            self.output_dir_edit.setEnabled(False)
            self.btn_browse.setEnabled(False)
        else:
            self._state = "idle"
            self._apply_state()

    # ── 설정 검증 및 생성 ──────────────────────────────────────────
    def build_config(self) -> CoupangRunConfig | None:
        out_dir = self.output_dir_edit.text().strip()
        if not out_dir:
            return None
        prefix = self.prefix_edit.text().strip() or None
        try:
            return CoupangRunConfig(
                output_dir=Path(out_dir),
                output_prefix=prefix,
                max_scroll_pages=self.spin_max_pages.value(),
                batch_size=self.spin_batch_size.value(),
                warmup_time=self.spin_warmup.value(),
                delay_min=self.spin_delay_min.value(),
                delay_max=self.spin_delay_max.value(),
            )
        except ValueError:
            return None

    # ── UI 갱신 슬롯 ──────────────────────────────────────────────
    def append_log(self, msg: str) -> None:
        log_line(f"[Coupang] {msg}")
        timestamp = datetime.now().astimezone().strftime("%H:%M:%S")
        self.log_area.appendPlainText(f"[{timestamp}] {msg}")

    def set_phase(self, name: str, current: int, total: int) -> None:
        self.lbl_phase.setText(f"단계 {current}/{total}: {name}")

    def set_stats_text(self, text: str) -> None:
        self.lbl_stats.setText(text)

    def set_progress_text(self, text: str) -> None:
        self.lbl_progress.setText(text)

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
