"""Coupang 검색 탭 패널: 키워드 입력/설정/버튼/진행/로그/결과."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import (
    QCheckBox,
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
from app.core.coupang.search_crawler import SearchRunConfig

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


class SearchPanel(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._state = "idle"
        self._build_ui()
        self._apply_state()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        info = QLabel(
            "대상: 쿠팡 키워드 검색 결과 — 정렬 4종(판매량/낮은가격/높은가격/최신) 순회 수집. "
            "랭킹순 제외, 로켓배송 상품은 기본 제외."
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

        self.keyword_edit = QLineEdit()
        self.keyword_edit.setPlaceholderText("검색어 입력 (예: 뷰티)")
        form.addRow("검색어:", self.keyword_edit)

        self.chk_exclude_rocket = QCheckBox("로켓배송 상품 제외")
        self.chk_exclude_rocket.setChecked(True)
        self.chk_price_bands = QCheckBox("가격 밴드 수집 (층2)")
        self.chk_price_bands.setChecked(True)
        filter_row = QHBoxLayout()
        filter_row.addWidget(self.chk_exclude_rocket)
        filter_row.addWidget(self.chk_price_bands)
        form.addRow("필터:", filter_row)

        self.category_edit = QLineEdit()
        self.category_edit.setPlaceholderText("선택 — 카테고리 ID 입력 시 페이지 순회 수집 (예: 뷰티=176522)")
        form.addRow("카테고리 ID:", self.category_edit)

        self.spin_max_pages = QSpinBox()
        self.spin_max_pages.setRange(1, 50)
        self.spin_max_pages.setValue(17)
        form.addRow("카테고리 최대 페이지:", self.spin_max_pages)

        delay_row = QHBoxLayout()
        self.spin_delay_min = QDoubleSpinBox()
        self.spin_delay_min.setRange(5.0, 120.0)
        self.spin_delay_min.setValue(15.0)
        self.spin_delay_max = QDoubleSpinBox()
        self.spin_delay_max.setRange(5.0, 180.0)
        self.spin_delay_max.setValue(20.0)
        delay_row.addWidget(self.spin_delay_min)
        delay_row.addWidget(QLabel("~"))
        delay_row.addWidget(self.spin_delay_max)
        delay_row.addWidget(QLabel("초 (정렬 전환 간격)"))
        form.addRow("페이지 딜레이:", delay_row)

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

        self.phase_label = QLabel("대기 중")
        self.progress_label = QLabel("")
        self.stats_label = QLabel("")
        layout.addWidget(self.phase_label)
        layout.addWidget(self.progress_label)
        layout.addWidget(self.stats_label)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(160)
        layout.addWidget(self.log_view)

        self.result_table = QTableWidget(0, len(DISPLAY_COLUMNS))
        self.result_table.setHorizontalHeaderLabels([COLUMN_LABELS[c] for c in DISPLAY_COLUMNS])
        self.result_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.result_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.result_table, stretch=1)

    def _browse_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "출력 폴더 선택")
        if path:
            self.output_dir_edit.setText(path)

    # ── 상태 관리 ──────────────────────────────────────────────────────────

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
        for w in (self.output_dir_edit, self.keyword_edit, self.chk_exclude_rocket,
                  self.chk_price_bands, self.category_edit, self.spin_max_pages,
                  self.spin_delay_min, self.spin_delay_max):
            w.setEnabled(settings_enabled)
        self.btn_browse.setEnabled(settings_enabled)

    def set_external_busy(self, busy: bool) -> None:
        if busy:
            for b in (self.btn_start, self.btn_pause, self.btn_resume, self.btn_cancel):
                b.setEnabled(False)
            for w in (self.output_dir_edit, self.keyword_edit, self.chk_exclude_rocket,
                      self.chk_price_bands, self.category_edit, self.spin_max_pages,
                      self.spin_delay_min, self.spin_delay_max):
                w.setEnabled(False)
            self.btn_browse.setEnabled(False)
        else:
            self._apply_state()

    # ── 설정/데이터 ────────────────────────────────────────────────────────

    def build_config(self) -> SearchRunConfig | None:
        output_dir = self.output_dir_edit.text().strip()
        keyword = self.keyword_edit.text().strip()
        if not output_dir or not keyword:
            return None
        delay_min = self.spin_delay_min.value()
        delay_max = max(delay_min, self.spin_delay_max.value())
        category_id = self.category_edit.text().strip()
        try:
            return SearchRunConfig(
                output_dir=Path(output_dir),
                keyword=keyword,
                exclude_rocket=self.chk_exclude_rocket.isChecked(),
                include_price_bands=self.chk_price_bands.isChecked(),
                category_id=category_id,
                max_pages=self.spin_max_pages.value(),
                page_delay_min=delay_min,
                page_delay_max=delay_max,
            )
        except ValueError:
            return None

    def append_log(self, msg: str) -> None:
        from datetime import datetime as _dt
        log_line(f"[검색] {msg}")
        timestamp = _dt.now().astimezone().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{timestamp}] {msg}")

    def set_phase(self, name: str, current: int, total: int) -> None:
        self.phase_label.setText(f"단계 {current}/{total}: {name}")

    def set_stats_text(self, text: str) -> None:
        self.stats_label.setText(text)

    def set_progress_text(self, text: str) -> None:
        self.progress_label.setText(text)

    def add_record(self, record: dict) -> None:
        row = self.result_table.rowCount()
        self.result_table.insertRow(row)
        for col, key in enumerate(DISPLAY_COLUMNS):
            value = record.get(key, "")
            if key == "power_seller":
                value = "✓" if value else ""
            self.result_table.setItem(row, col, QTableWidgetItem(str(value)))

    def clear_results(self) -> None:
        self.result_table.setRowCount(0)
        self.log_view.clear()
        self.phase_label.setText("대기 중")
        self.progress_label.setText("")
        self.stats_label.setText("")
