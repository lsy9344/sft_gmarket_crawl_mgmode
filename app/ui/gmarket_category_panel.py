"""Gmarket 카테고리 탭 패널: 대/중/소 카테고리 트리 선택 → 수집.

- 트리: 국내 PC 사이트의 전체 카테고리(대분류 60 + 중분류 + 소분류, 한글명)
  — 소스는 Cloudflare 없는 `category.gmarket.co.kr/listview/L{code}.aspx`
  (docs/gmarket/CATEGORY_RESEARCH.md). 캐시(7일) + 번들 seed 우선.
  5,548개 노드 탐색을 위한 이름/코드 검색 필터를 제공한다.
- 수집: 선택 카테고리(또는 대분류 전체)의 `/n/list` 리스팅을 Bright Data
  Web Unlocker 로 순회해 goodscode 를 모은 뒤(Phase A), 기존 SellerCrawler 로
  판매자정보를 수집·저장한다(Phase B).
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core import config
from app.core.applog import log_line
from app.core.gmarket_categories import GmarketCategoryNode, count_nodes
from app.core.gmarket_category_crawler import (
    CategoryTarget,
    GmarketCategoryRunConfig,
)
from app.ui.widgets.collection_workspace import collection_workspace
from app.ui.widgets.result_table import ResultTable

ROLE_CODE = 0x0100
ROLE_NAME = 0x0101
ROLE_GROUP = 0x0102

# 화면에서 함께 선택할 대분류 묶음. 저장된 카테고리 트리는 그대로 유지한다.
HOME_DECO_CODES = frozenset({
    "100000031", "100000039", "100000093", "100000014",
    "100000085", "100000041", "100000083", "100000112",
})


class GmarketCategoryPanel(QWidget):
    """'Gmarket 카테고리' 탭 위젯 (CategoryPanel 패턴 미러링)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._state = "idle"
        self._roots: list[GmarketCategoryNode] = []
        self._last_output_dir: str = ""
        self._build_ui()
        self._apply_state()

    # ── UI 구성 ─────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        info = QLabel(
            "⚠ 수집에는 Bright Data API 토큰이 필요합니다 — 상단 '설정' 탭에서 "
            "입력·저장한 뒤 시작하세요 (입력 없이는 시작할 수 없습니다).\n"
            "대상: Gmarket 전체 카테고리 — 대/중/소 트리(또는 대분류 전체)에서 "
            "카테고리를 선택하면 그 카테고리의 리스팅 상품(goodscode)을 수집한 뒤 "
            "판매자 사업자정보를 수집합니다. 리스팅은 Bright Data Web Unlocker "
            "(한국 IP 경유)를 사용하며 브라우저가 필요 없습니다."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        # ── 카테고리 트리 + 설정 ─────────────────────────────────────────
        left_column = QWidget()
        left_layout = QVBoxLayout(left_column)
        left_layout.setContentsMargins(0, 0, 0, 0)

        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)

        tree_panel = QWidget()
        tree_box = QVBoxLayout(tree_panel)
        tree_box.setContentsMargins(0, 0, 0, 0)
        tree_head = QHBoxLayout()
        self.btn_refresh_categories = QPushButton("카테고리 목록 새로고침")
        self.btn_refresh_categories.setToolTip(
            "Gmarket 에서 전체 카테고리(대/중/소)를 다시 불러옵니다.\n"
            "약 60개 대분류 페이지를 순회하므로 1분 내외 걸립니다."
        )
        self.cache_label = QLabel("카테고리 목록 없음 — 새로고침을 눌러 불러오세요")
        self.cache_label.setWordWrap(True)
        tree_head.addWidget(self.btn_refresh_categories)
        tree_head.addWidget(self.cache_label, stretch=1)
        tree_box.addLayout(tree_head)

        self.tree_filter = QLineEdit()
        self.tree_filter.setPlaceholderText(
            "카테고리 검색 (이름/코드) — 예: 자켓, 200000502"
        )
        self.tree_filter.setClearButtonEnabled(True)
        self.tree_filter.setToolTip("트리에서 이름 또는 코드가 일치하는 카테고리만 표시합니다.")
        tree_box.addWidget(self.tree_filter)

        self.category_tree = QTreeWidget()
        self.category_tree.setHeaderLabels(["카테고리", "코드"])
        self.category_tree.setColumnWidth(0, 240)
        self.category_tree.setToolTip("홈데코 묶음을 선택하면 그 아래 모든 카테고리를 수집합니다.")
        self.category_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        tree_box.addWidget(self.category_tree, stretch=1)
        self.selected_label = QLabel("선택: 없음 (대분류/중분류/소분류를 선택하세요)")
        self.selected_label.setWordWrap(True)
        tree_box.addWidget(self.selected_label)
        controls_layout.addWidget(tree_panel, stretch=1)

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

        self.chk_include_subs = QCheckBox("하위 카테고리 포함 수집")
        self.chk_include_subs.setChecked(True)
        self.chk_include_subs.setToolTip(
            "체크 시 선택한 카테고리 + 그 아래 모든 하위(중/소분류)를 각각 수집합니다.\n"
            "묶음을 선택하면 체크 여부와 관계없이 하위 전체를 수집합니다."
        )
        form.addRow("하위 카테고리:", self.chk_include_subs)

        self.chk_all_targets = QCheckBox("전체 카테고리 대상 (중분류 전체 수집)")
        self.chk_all_targets.setToolTip(
            "체크 시 트리 선택을 무시하고 모든 중분류(약 723개)를 대상으로 합니다.\n"
            "대분류 페이지는 레거시 변형(상품 목록 없음)으로 응답할 수 있어,\n"
            "실제 목록이 보장되는 중분류 레벨을 전체 대상으로 삼습니다."
        )
        form.addRow("전체 대상:", self.chk_all_targets)

        self.spin_max_pages = QSpinBox()
        self.spin_max_pages.setRange(1, 200)
        self.spin_max_pages.setValue(config.CATEGORY_MAX_PAGES)
        form.addRow("최대 페이지:", self.spin_max_pages)

        delay_row = QHBoxLayout()
        self.spin_page_delay_min = QDoubleSpinBox()
        self.spin_page_delay_min.setRange(1.0, 600.0)
        self.spin_page_delay_min.setValue(config.CATEGORY_PAGE_DELAY_MIN)
        self.spin_page_delay_max = QDoubleSpinBox()
        self.spin_page_delay_max.setRange(1.0, 600.0)
        self.spin_page_delay_max.setValue(config.CATEGORY_PAGE_DELAY_MAX)
        delay_row.addWidget(self.spin_page_delay_min)
        delay_row.addWidget(QLabel("~"))
        delay_row.addWidget(self.spin_page_delay_max)
        delay_row.addWidget(QLabel("초 (페이지 간격)"))
        form.addRow("리스팅 딜레이:", delay_row)

        self.spin_phase2_delay = QDoubleSpinBox()
        self.spin_phase2_delay.setRange(0.1, 10.0)
        self.spin_phase2_delay.setSingleStep(0.1)
        self.spin_phase2_delay.setValue(config.DEFAULT_DELAY)
        form.addRow("판매자정보 간격:", self.spin_phase2_delay)

        self.spin_max_items = QSpinBox()
        self.spin_max_items.setRange(10, 100000)
        self.spin_max_items.setValue(5000)
        self.spin_max_items.setToolTip(
            "카테고리당 최대 수집 수(Phase B 캡). 상품이 더 많아도 이 값까지만 수집합니다."
        )
        form.addRow("대상당 최대 수:", self.spin_max_items)

        controls_layout.addWidget(settings_group)

        # ── 제어 버튼 ────────────────────────────────────────────────────
        btn_row = QGridLayout()
        btn_row.setHorizontalSpacing(8)
        btn_row.setVerticalSpacing(8)
        self.btn_start = QPushButton("수집 시작")
        self.btn_pause = QPushButton("일시정지")
        self.btn_resume = QPushButton("재개")
        self.btn_cancel = QPushButton("취소")
        self.btn_open_result = QPushButton("결과 열기")
        buttons = (self.btn_start, self.btn_pause, self.btn_resume,
                   self.btn_cancel, self.btn_open_result)
        for index, button in enumerate(buttons):
            btn_row.addWidget(button, index // 3, index % 3)
        for column in range(3):
            btn_row.setColumnStretch(column, 1)
        left_layout.addLayout(btn_row)

        self.phase_label = QLabel("대기 중")
        self.progress_label = QLabel("")
        self.stats_label = QLabel("")
        self.phase_label.setWordWrap(True)
        self.progress_label.setWordWrap(True)
        self.stats_label.setWordWrap(True)
        controls_layout.addWidget(self.phase_label)
        controls_layout.addWidget(self.progress_label)
        controls_layout.addWidget(self.stats_label)

        controls_scroll = QScrollArea()
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        controls_scroll.setWidget(controls)
        left_layout.addWidget(controls_scroll, stretch=1)

        log_panel = QGroupBox("실시간 로그")
        log_layout = QVBoxLayout(log_panel)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        log_layout.addWidget(self.log_view)

        self.result_table = ResultTable()
        result_table_widget = getattr(self.result_table, "_table", None)
        if result_table_widget is not None:
            result_header = result_table_widget.horizontalHeader()
            result_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            result_header.setStretchLastSection(False)
            result_header.setMinimumSectionSize(140)
            for column, width in enumerate((170, 170, 230, 160, 170)):
                result_table_widget.setColumnWidth(column, width)

        layout.addWidget(
            collection_workspace(left_column, log_panel, self.result_table), stretch=1
        )

        self.category_tree.currentItemChanged.connect(self._on_tree_select)
        self.tree_filter.textChanged.connect(self._filter_tree)
        self.chk_all_targets.toggled.connect(self._on_all_targets_toggled)

    def _browse_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "출력 폴더 선택")
        if path:
            self.output_dir_edit.setText(path)

    # ── 카테고리 트리 ────────────────────────────────────────────────────
    def set_category_roots(self, roots: list[GmarketCategoryNode],
                           fetched_at: str = "", total: int = 0) -> None:
        """트리 루트(대분류 목록)를 표시한다."""
        self._roots = list(roots)
        self.category_tree.clear()
        home_deco = None
        for node in roots:
            item = self._make_item(node)
            if node.code in HOME_DECO_CODES:
                if home_deco is None:
                    home_deco = QTreeWidgetItem(["홈데코", "묶음"])
                    home_deco.setData(0, ROLE_GROUP, True)
                    home_deco.setData(0, ROLE_NAME, "홈데코")
                    self.category_tree.addTopLevelItem(home_deco)
                    home_deco.setExpanded(True)
                home_deco.addChild(item)
            else:
                self.category_tree.addTopLevelItem(item)
        stamp = fetched_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        label_total = total or count_nodes(roots)
        self.cache_label.setText(
            f"대분류 {len(roots)} · 전체 {label_total:,}개 · 불러온 시각: {stamp}"
        )
        self._filter_tree(self.tree_filter.text())
        self._on_all_targets_toggled(self.chk_all_targets.isChecked())

    def _make_item(self, node: GmarketCategoryNode) -> QTreeWidgetItem:
        item = QTreeWidgetItem([node.name, node.code])
        item.setData(0, ROLE_CODE, node.code)
        item.setData(0, ROLE_NAME, node.name)
        for child in node.children:
            item.addChild(self._make_item(child))
        return item

    def _on_tree_select(self, current: QTreeWidgetItem | None, _prev) -> None:
        if self.chk_all_targets.isChecked():
            return  # 전체 대상 모드에서는 트리 선택 라벨을 갱신하지 않는다
        if current is None:
            self.selected_label.setText("선택: 없음 (대분류/중분류/소분류를 선택하세요)")
            return
        path = self._item_path_names(current)
        if current.data(0, ROLE_GROUP):
            self.selected_label.setText(
                f"선택: {current.text(0)} 묶음 — 하위 전체 {len(self.build_targets())}개 수집"
            )
            return
        extra = ""
        if self.chk_include_subs.isChecked():
            child_count = self._descendant_count(current)
            extra = f" — 하위 {child_count}개 포함 수집" if child_count else " — 하위 없음"
        self.selected_label.setText(f"선택: {path}{extra}")

    def _on_all_targets_toggled(self, checked: bool) -> None:
        """전체 대상 모드: 트리/검색 잠금, 선택 라벨을 대상 요약으로 교체."""
        self.category_tree.setEnabled(not checked and self._state in
                                      ("idle", "finished", "failed"))
        self.tree_filter.setEnabled(not checked)
        if checked:
            middle_total = sum(len(r.children) for r in self._roots)
            self.selected_label.setText(
                f"선택: 전체 카테고리 — 중분류 {middle_total}개 수집"
            )
        else:
            self.selected_label.setText("선택: 없음 (대분류/중분류/소분류를 선택하세요)")
            self._on_tree_select(self.category_tree.currentItem(), None)

    def _filter_tree(self, text: str) -> None:
        """이름/코드 검색어로 트리 표시 필터링 (일치 노드 + 그 조상만 표시)."""
        query = text.strip().lower()
        if not query:
            self._set_all_visible(self.category_tree.invisibleRootItem())
            return

        def walk(item: QTreeWidgetItem) -> bool:
            matched = query in item.text(0).lower() or query in item.text(1).lower()
            if matched and item.data(0, ROLE_GROUP):
                self._set_all_visible(item)
                item.setExpanded(True)
                return True
            for i in range(item.childCount()):
                if walk(item.child(i)):
                    matched = True
            item.setHidden(not matched)
            if matched:
                item.setExpanded(True)
            return matched

        walk(self.category_tree.invisibleRootItem())

    @staticmethod
    def _set_all_visible(root: QTreeWidgetItem) -> None:
        stack = [root]
        while stack:
            item = stack.pop()
            item.setHidden(False)
            stack.extend(item.child(i) for i in range(item.childCount()))

    @staticmethod
    def _item_path_names(item: QTreeWidgetItem) -> str:
        """트리 아이템의 조상 이름 체인 → '대분류 > 중분류 > 소분류'."""
        names: list[str] = []
        node: QTreeWidgetItem | None = item
        while node is not None:
            name = node.data(0, ROLE_NAME) or node.text(0)
            if name and not node.data(0, ROLE_GROUP):
                names.append(str(name))
            node = node.parent()
        return " > ".join(reversed(names))

    def _descendant_count(self, item: QTreeWidgetItem) -> int:
        total = 0
        for i in range(item.childCount()):
            child = item.child(i)
            total += 1 + self._descendant_count(child)
        return total

    def selected_item(self) -> QTreeWidgetItem | None:
        """현재 선택된 카테고리 또는 수집 가능한 묶음."""
        item = self.category_tree.currentItem()
        if item is None:
            return None
        code = item.data(0, ROLE_CODE)
        if not code and not item.data(0, ROLE_GROUP):
            return None
        return item

    def build_targets(self) -> list[CategoryTarget]:
        """선택 카테고리(+하위 포함 시 전체 후손) → CategoryTarget 목록.

        전체 대상 모드(chk_all_targets)에서는 모든 중분류(M)를 반환한다 —
        대분류(L) 페이지는 레거시 변형(상품 목록 없음)으로 응답할 수 있어
        상품 목록이 보장되는 중분류 레벨을 대상으로 삼는다(실측 2026-09-08).
        각 대상의 label 은 '대 > 중 > 소' 전체 경로 — Phase B 파일명/source
        라벨로 쓰이며 경로가 같아야 충돌하지 않는다.
        """
        seen: set[str] = set()
        targets: list[CategoryTarget] = []

        def add(target_item: QTreeWidgetItem) -> None:
            code = target_item.data(0, ROLE_CODE)
            if not code or str(code) in seen:
                return
            seen.add(str(code))
            targets.append(
                CategoryTarget(str(code), self._item_path_names(target_item))
            )

        if self.chk_all_targets.isChecked():
            items = {
                child.data(0, ROLE_CODE): child
                for child in self._iter_children(self.category_tree.invisibleRootItem())
                if child.data(0, ROLE_CODE)
            }
            for root in self._roots:
                for child in root.children:
                    add(items[child.code])
            return targets

        item = self.selected_item()
        if item is None:
            return targets
        add(item)
        if self.chk_include_subs.isChecked() or item.data(0, ROLE_GROUP):
            for child in self._iter_children(item):
                add(child)
        return targets

    def _iter_children(self, item: QTreeWidgetItem):
        """아이템 하위를 DFS 로 순회 (중복 코드 제거)."""
        seen: set[str] = set()
        stack = [item]
        while stack:
            node = stack.pop()
            for i in range(node.childCount()):
                child = node.child(i)
                code = child.data(0, ROLE_CODE)
                if code and str(code) in seen:
                    continue
                if code:
                    seen.add(str(code))
                yield child
                stack.append(child)

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
        for w in (self.output_dir_edit, self.chk_include_subs, self.chk_all_targets,
                  self.spin_max_pages, self.spin_page_delay_min,
                  self.spin_page_delay_max, self.spin_phase2_delay,
                  self.spin_max_items):
            w.setEnabled(settings_enabled)
        self.btn_browse.setEnabled(settings_enabled)
        self.tree_filter.setEnabled(settings_enabled
                                    and not self.chk_all_targets.isChecked())
        self.category_tree.setEnabled(settings_enabled
                                      and not self.chk_all_targets.isChecked())
        self.btn_refresh_categories.setEnabled(settings_enabled)

    def set_loading_categories(self, loading: bool) -> None:
        self.btn_refresh_categories.setEnabled(not loading)
        self.category_tree.setEnabled(not loading)
        if loading:
            self.cache_label.setText("카테고리 목록을 불러오는 중... (약 1분, 취소 가능)")

    def set_external_busy(self, busy: bool) -> None:
        """다른 탭이 실행 중이면 이 탭의 조작을 잠근다."""
        if busy:
            for b in (self.btn_start, self.btn_pause, self.btn_resume, self.btn_cancel,
                      self.btn_refresh_categories):
                b.setEnabled(False)
            for w in (self.output_dir_edit, self.chk_include_subs, self.chk_all_targets,
                      self.spin_max_pages, self.spin_page_delay_min,
                      self.spin_page_delay_max, self.spin_phase2_delay,
                      self.spin_max_items):
                w.setEnabled(False)
            self.btn_browse.setEnabled(False)
            self.tree_filter.setEnabled(False)
            self.category_tree.setEnabled(False)
        else:
            self._apply_state()

    # ── 설정/데이터 ──────────────────────────────────────────────────────
    def build_config(self) -> GmarketCategoryRunConfig | None:
        output_dir = self.output_dir_edit.text().strip()
        targets = self.build_targets()
        if not output_dir or not targets:
            return None
        leaf_name = targets[0].label.split(" > ")[-1]
        ts = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        safe_name = re.sub(r"[^\w가-힣]+", "_", leaf_name)[:40].strip("_")
        delay_min = self.spin_page_delay_min.value()
        delay_max = max(delay_min, self.spin_page_delay_max.value())
        try:
            return GmarketCategoryRunConfig(
                output_dir=Path(output_dir),
                output_prefix=f"gmarket_category_{safe_name or targets[0].code}_{ts}",
                targets=tuple(targets),
                max_pages=self.spin_max_pages.value(),
                page_delay_min=delay_min,
                page_delay_max=delay_max,
                phase2_delay=self.spin_phase2_delay.value(),
                max_items=self.spin_max_items.value(),
            )
        except ValueError:
            return None

    def output_dir(self) -> str:
        return self.output_dir_edit.text().strip()

    # ── 로그/진행/결과 ───────────────────────────────────────────────────
    def append_log(self, msg: str) -> None:
        log_line(f"[Gmarket 카테고리] {msg}")
        timestamp = datetime.now().astimezone().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{timestamp}] {msg}")

    def set_phase(self, name: str, current: int, total: int) -> None:
        self.phase_label.setText(f"단계 {current}/{total}: {name}")

    def set_progress_text(self, text: str) -> None:
        self.progress_label.setText(text)

    def set_stats_text(self, text: str) -> None:
        self.stats_label.setText(text)

    def add_record(self, record: dict) -> None:
        self.result_table.add_record(record)

    def clear_results(self) -> None:
        self.result_table.clear_records()
        self.log_view.clear()
        self.phase_label.setText("대기 중")
        self.progress_label.setText("")
        self.stats_label.setText("")
