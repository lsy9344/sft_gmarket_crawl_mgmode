"""Coupang 카테고리 탭 패널: 카테고리 트리 선택 → 수집 (컨셉 전환 2026-08-24).

- 쿠팡 '카테고리' 버튼과 동일한 전체 카테고리 트리를 표시
  (데이터: /n-api/web-adapter/category-list, 로컬 캐시 우선)
- 카테고리 선택 → 해당 카테고리 리스팅 페이지(1..N) 수집 시작
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import (
    QAbstractItemView,
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
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.applog import log_line
from app.core.coupang.categories import CategoryNode, flatten_descendants
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

ROLE_ID = 0x0100
ROLE_NAME = 0x0101


class CategoryPanel(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._state = "idle"
        self._groups: list[tuple[str, list[CategoryNode]]] = []
        self._build_ui()
        self._apply_state()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        info = QLabel(
            "대상: 쿠팡 카테고리 리스팅 — 선택한 카테고리의 페이지(1..N, 실측 상한 17)를 "
            "순회 수집합니다. 로켓배송 상품은 기본 제외. "
            "카테고리 목록은 쿠팡 '카테고리' 메뉴와 동일합니다."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        # ── 카테고리 트리 + 설정 ─────────────────────────────────────────
        middle = QHBoxLayout()

        tree_box = QVBoxLayout()
        tree_head = QHBoxLayout()
        self.btn_refresh_categories = QPushButton("카테고리 목록 새로고침")
        self.cache_label = QLabel("카테고리 목록 없음 — 새로고침을 눌러 쿠팡에서 불러오세요")
        tree_head.addWidget(self.btn_refresh_categories)
        tree_head.addWidget(self.cache_label, stretch=1)
        tree_box.addLayout(tree_head)

        self.category_tree = QTreeWidget()
        self.category_tree.setHeaderLabels(["카테고리", "ID"])
        self.category_tree.setColumnWidth(0, 260)
        self.category_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        tree_box.addWidget(self.category_tree, stretch=1)
        self.selected_label = QLabel("선택: 없음")
        tree_box.addWidget(self.selected_label)
        middle.addLayout(tree_box, stretch=1)

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

        self.chk_exclude_rocket = QCheckBox("로켓배송 상품 제외")
        self.chk_exclude_rocket.setChecked(True)
        form.addRow("필터:", self.chk_exclude_rocket)

        self.chk_include_subs = QCheckBox("하위 카테고리 포함 수집")
        self.chk_include_subs.setChecked(True)
        form.addRow("하위 카테고리:", self.chk_include_subs)

        self.spin_max_pages = QSpinBox()
        self.spin_max_pages.setRange(1, 50)
        self.spin_max_pages.setValue(17)
        form.addRow("최대 페이지:", self.spin_max_pages)

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
        delay_row.addWidget(QLabel("초 (페이지 간격)"))
        form.addRow("페이지 딜레이:", delay_row)

        middle.addWidget(settings_group)
        layout.addLayout(middle, stretch=1)

        # ── 제어 버튼 ────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self.btn_start = QPushButton("수집 시작")
        self.btn_pause = QPushButton("일시정지")
        self.btn_resume = QPushButton("재개")
        self.btn_cancel = QPushButton("취소")
        self.btn_open_result = QPushButton("결과 열기")
        for b in (self.btn_start, self.btn_pause, self.btn_resume, self.btn_cancel,
                  self.btn_open_result):
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

        self.category_tree.currentItemChanged.connect(self._on_tree_select)

    def _browse_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "출력 폴더 선택")
        if path:
            self.output_dir_edit.setText(path)

    # ── 카테고리 트리 ────────────────────────────────────────────────────

    def set_category_groups(self, groups: list[tuple[str, list[CategoryNode]]],
                            fetched_at: str = "", total: int = 0) -> None:
        self._groups = groups
        self.category_tree.clear()
        for label, roots in groups:
            group_item = QTreeWidgetItem([f"▣ {label}", ""])
            group_item.setFlags(group_item.flags())
            self.category_tree.addTopLevelItem(group_item)
            for node in roots:
                group_item.addChild(self._make_node_item(node))
            group_item.setExpanded(False)
        stamp = fetched_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.cache_label.setText(f"카테고리 {total}개 · 불러온 시각: {stamp}")

    def _make_node_item(self, node: CategoryNode) -> QTreeWidgetItem:
        item = QTreeWidgetItem([node.name, node.id])
        item.setData(0, ROLE_ID, node.id)
        item.setData(0, ROLE_NAME, node.name)
        for child in node.children:
            item.addChild(self._make_node_item(child))
        return item

    def _on_tree_select(self, current: QTreeWidgetItem | None, _prev) -> None:
        cid = current.data(0, ROLE_ID) if current else ""
        name = current.data(0, ROLE_NAME) if current else ""
        if cid:
            subs = self._descendant_count(str(cid))
            extra = f" — 하위 {subs}개 포함 가능" if subs else " — 하위 없음"
            self.selected_label.setText(f"선택: {name} ({cid}){extra}")
        else:
            self.selected_label.setText("선택: 없음 (세부 카테고리를 선택하세요)")

    def _find_selected_node(self, category_id: str) -> CategoryNode | None:
        stack = [n for _, roots in self._groups for n in roots]
        while stack:
            node = stack.pop()
            if node.id == category_id:
                return node
            stack.extend(node.children)
        return None

    def _descendant_count(self, category_id: str) -> int:
        node = self._find_selected_node(category_id)
        return len(flatten_descendants(node)) if node else 0

    def selected_subcategories(self) -> list[tuple[str, str]]:
        """선택 카테고리의 하위 전부 (id, 이름) — 체크박스 해제 시 []."""
        if not self.chk_include_subs.isChecked():
            return []
        selected = self.selected_category()
        if not selected:
            return []
        node = self._find_selected_node(selected[0])
        if node is None:
            return []
        return [(c.id, c.name) for c in flatten_descendants(node)]

    def selected_category(self) -> tuple[str, str] | None:
        """(category_id, name) — 그룹 헤더 등 비카테고리 선택 시 None."""
        item = self.category_tree.currentItem()
        if item is None:
            return None
        cid = item.data(0, ROLE_ID)
        if not cid:
            return None
        return str(cid), str(item.data(0, ROLE_NAME) or cid)

    def set_cache_label(self, text: str) -> None:
        self.cache_label.setText(text)

    # ── 상태 관리 ────────────────────────────────────────────────────────

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
        for w in (self.output_dir_edit, self.chk_exclude_rocket, self.chk_include_subs,
                  self.spin_max_pages, self.spin_delay_min, self.spin_delay_max):
            w.setEnabled(settings_enabled)
        self.btn_browse.setEnabled(settings_enabled)
        self.category_tree.setEnabled(settings_enabled)
        self.btn_refresh_categories.setEnabled(settings_enabled and s != "loading_categories")

    def set_loading_categories(self, loading: bool) -> None:
        self.btn_refresh_categories.setEnabled(not loading)
        self.category_tree.setEnabled(not loading)
        if loading:
            self.cache_label.setText("카테고리 목록을 불러오는 중... (쿠팡 접속, 약 30초)")

    def set_external_busy(self, busy: bool) -> None:
        if busy:
            for b in (self.btn_start, self.btn_pause, self.btn_resume, self.btn_cancel,
                      self.btn_refresh_categories):
                b.setEnabled(False)
            for w in (self.output_dir_edit, self.chk_exclude_rocket, self.chk_include_subs,
                      self.spin_max_pages, self.spin_delay_min, self.spin_delay_max):
                w.setEnabled(False)
            self.btn_browse.setEnabled(False)
            self.category_tree.setEnabled(False)
        else:
            self._apply_state()

    # ── 설정/데이터 ──────────────────────────────────────────────────────

    def build_config(self) -> SearchRunConfig | None:
        output_dir = self.output_dir_edit.text().strip()
        selected = self.selected_category()
        if not output_dir or not selected:
            return None
        cid, name = selected
        delay_min = self.spin_delay_min.value()
        delay_max = max(delay_min, self.spin_delay_max.value())
        ts = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        safe_name = re.sub(r"[^\w가-힣]+", "_", name)[:30].strip("_") or cid
        subs = self.selected_subcategories()
        try:
            return SearchRunConfig(
                output_dir=Path(output_dir),
                output_prefix=f"coupang_category_{safe_name}_{ts}",
                keyword="",
                category_id=cid,
                category_name=name,
                exclude_rocket=self.chk_exclude_rocket.isChecked(),
                max_pages=self.spin_max_pages.value(),
                page_delay_min=delay_min,
                page_delay_max=delay_max,
                subcategories=tuple(subs),
            )
        except ValueError:
            return None

    def append_log(self, msg: str) -> None:
        log_line(f"[카테고리] {msg}")
        timestamp = datetime.now().astimezone().strftime("%H:%M:%S")
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
