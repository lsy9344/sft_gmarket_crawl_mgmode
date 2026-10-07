"""쿠팡 병렬 카테고리 탭 패널 — 카테고리 가족별 다중 인스턴스 수집 (M3).

'쿠팡 카테고리' 탭을 병렬 인스턴스 패널로 교체한다(2026-10-01). 사용자는
트리에서 카테고리 최상위 노드 여러 개를 고른다 — 선택된 각 노드(다른 선택
항목의 하위에 속한 선택은 제외)가 1개 '가족'이 되고, 가족은 그 노드와 모든
후손(id 중복 첫 1회, categories.flatten_descendants 재사용)으로 구성된다.

시작 요청은 Ali 탭 시그널 스타일을 따른다 — 패널은 실행 설정 dict 만
emit(start_requested) 하고, preflight·재개 판단·워커 생성은 main_window 가
맡는다. 패널은 인스턴스 카드 현황·로그·결과 표를 갱신하는 표시 영역이다.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
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
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.applog import log_line
from app.core.config import DEFAULT_OUTPUT_DIR
from app.core.coupang.categories import (
    CategoryNode,
    find_node,
    flatten_descendants,
)
from app.core.coupang.parallel_manager import (
    MAX_INSTANCES,
    STATUS_BLOCKED,
    STATUS_COLLECTING,
    STATUS_COMPLETE,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_WAITING,
)
from app.core.coupang.work_plan import (
    WORK_KIND_WHOLE,
    plan_summary,
    plan_work_units,
    unit_to_dict,
)
from app.ui.widgets.collection_workspace import collection_workspace
from app.workers.coupang_parallel_worker import SELLER_DISPLAY_FIELDS

# 결과 표 열 — 기존 CategoryPanel.DISPLAY_COLUMNS 8열과 동일한 규격.
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

# 인스턴스 상태 문자 → 표시 문구(매니저 STATUS_* 상수와 1:1).
_STATUS_TEXT = {
    STATUS_WAITING: "대기",
    STATUS_COLLECTING: "수집중",
    STATUS_BLOCKED: "차단",
    STATUS_COMPLETE: "완주",
    STATUS_DONE: "완료",
    STATUS_ERROR: "오류",
}


def _default_output_dir() -> Path:
    """기본 출력 폴더 — output/coupang_parallel_{타임스탬프}."""
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    return DEFAULT_OUTPUT_DIR / f"coupang_parallel_{stamp}"


def _instance_line_label(number: int) -> str:
    """인스턴스 회선 표시 — 1번 직접, 2~N번 Decodo 스티키(시작 sid)."""
    if number <= 1:
        return "직접"
    return f"Decodo i{number}"


# 배분 방식 — 가족 모드(선택한 최상위 각각 = 1개 작업)와 분할 모드
# (선택 1개 카테고리를 인스턴스 수만큼 샤드로 나눔). 콤보 인덱스와 1:1.
MODE_FAMILY = "family"
MODE_SHARD = "shard"
_MODE_LABELS = (
    ("가족별 배분 (선택한 최상위 각각 = 1개 작업)", MODE_FAMILY),
    ("단일 카테고리 분할 (선택 1개를 인스턴스에 균등 분할)", MODE_SHARD),
)


class _InstanceCard(QWidget):
    """인스턴스 1개 행 카드 — 이름·회선·가족·상태·누적."""

    def __init__(self, number: int, parent=None) -> None:
        super().__init__(parent)
        self.number = number
        self.instance_id = str(number)
        # 패널이 넣어주는 가족명 조회 콜백(family_index → 표시명).
        self.family_name_lookup = None
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 2, 6, 2)
        self.name_label = QLabel(f"인스턴스 {number}")
        self.line_label = QLabel(_instance_line_label(number))
        self.family_label = QLabel("—")
        self.status_label = QLabel("대기")
        self.totals_label = QLabel("3P 0개 · 판매자 0명")
        for label, stretch in (
            (self.name_label, 1),
            (self.line_label, 1),
            (self.family_label, 2),
            (self.status_label, 2),
            (self.totals_label, 2),
        ):
            label.setWordWrap(True)
            row.addWidget(label, stretch)
        # 마지막으로 받은 요약 — collecting 처럼 총계가 빠진 이벤트에서도
        # 직전 누적을 유지한다.
        self.last_summary: dict = {}

    def update_state(self, state_str: str, summary: dict) -> None:
        merged = dict(self.last_summary)
        merged.update(summary or {})
        self.last_summary = merged
        family_index = merged.get("family_index")
        if family_index is None:
            self.family_label.setText("—")
        else:
            self.family_label.setText(self._family_name(family_index))
        self.status_label.setText(_status_text(state_str, merged))
        products = merged.get("total_products")
        sellers = merged.get("total_sellers")
        if products is not None or sellers is not None:
            self.totals_label.setText(
                f"3P {int(products or 0):,}개 · 판매자 {int(sellers or 0):,}명"
            )

    def _family_name(self, family_index) -> str:
        if self.family_name_lookup is not None:
            name = self.family_name_lookup(family_index)
            if name:
                return name
        return f"가족 {int(family_index) + 1}"


def _status_text(state_str: str, summary: dict) -> str:
    """상태 문자 + 요약 → 카드 상태 문구(대기는 다음 세션 시각 포함)."""
    base = _STATUS_TEXT.get(state_str, state_str or "대기")
    if state_str == STATUS_WAITING:
        next_run_at = summary.get("next_run_at")
        if isinstance(next_run_at, (int, float)) and next_run_at > 0:
            stamp = datetime.fromtimestamp(next_run_at).strftime("%H:%M")
            return f"대기 [다음 {stamp}]"
    return base


class CoupangParallelPanel(QWidget):
    """'쿠팡 카테고리' 탭 교체용 병렬 수집 패널."""

    start_requested = pyqtSignal(dict)
    stop_requested = pyqtSignal()
    refresh_categories_requested = pyqtSignal()
    open_output_requested = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._state = "idle"
        self._external_busy = False
        self._groups: list[tuple[str, list[CategoryNode]]] = []
        # 선택에서 확정한 가족(시작 전 미리보기·카드 가족명 표기용).
        self._families: list[list[tuple[str, str]]] = []
        self._family_names: list[str] = []
        self._instance_cards: dict[str, _InstanceCard] = {}
        self._build_ui()
        self.set_instance_count_preview(self.spin_instances.value())
        self._update_decodo_hint()
        self._apply_state()

    # ── UI 구성 ─────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        info = QLabel(
            "카테고리 최상위 노드를 여러 개 선택하세요 — '가족별 배분'이면 선택한 "
            "각 노드와 모든 하위가 1개 '가족'이 되고, 가족들을 인스턴스(직접 "
            "회선 1 + Decodo 스티키 회선 2~N)에 나누어 교대로 수집합니다(세션은 "
            "순차 실행 — 한 시점에 브라우저 1개, 인스턴스별 간격으로 엇갈려 "
            "돌아갑니다). '단일 카테고리 분할'이면 선택 1개 가족을 작업 단위 "
            "인스턴스 수의 2배로 나눠 교대로 수집하고, 전 단위 완주 시 루트에서 "
            "하나로 병합해 최종 파일을 만듭니다. 카테고리 물량(productCount)을 "
            "알면 큰 카테고리를 목록 페이지 범위·판매자 슬라이스 조각으로 더 "
            "잘게 쪼개 모든 회선이 일감을 갖게 하고, 목록이 끝난 카테고리의 "
            "판매자 조각은 곧바로 대기 큐에 배출돼 아무 회선이나 이어받습니다. "
            "인스턴스별로 세션 간격(기본 80분)을 두고 안전 장치가 함께 "
            "동작합니다. 정지·비정상 종료 후에도 진행 상태가 출력 폴더에 "
            "저장되어 같은 폴더로 다시 시작하면 이어서 수집되고(구버전 배분은 "
            "이전 규약으로 그대로 재개), 차단 인스턴스는 차단 1시간 뒤 재시작 시 "
            "안전 장치가 복구를 자동 승인합니다."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        left_column = QWidget()
        left_layout = QVBoxLayout(left_column)
        left_layout.setContentsMargins(0, 0, 0, 0)

        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)

        # ── 카테고리 트리(다중 선택) ────────────────────────────────
        tree_panel = QGroupBox("카테고리 선택 (다중)")
        tree_box = QVBoxLayout(tree_panel)
        tree_head = QHBoxLayout()
        self.btn_refresh_categories = QPushButton("카테고리 목록 새로고침")
        self.btn_refresh_categories.clicked.connect(
            self.refresh_categories_requested.emit
        )
        self.cache_label = QLabel(
            "카테고리 목록 없음 — 새로고침을 눌러 쿠팡에서 불러오세요"
        )
        self.cache_label.setWordWrap(True)
        tree_head.addWidget(self.btn_refresh_categories)
        tree_head.addWidget(self.cache_label, stretch=1)
        tree_box.addLayout(tree_head)

        self.category_tree = QTreeWidget()
        self.category_tree.setHeaderLabels(["카테고리", "ID"])
        self.category_tree.setColumnWidth(0, 240)
        self.category_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.category_tree.itemSelectionChanged.connect(
            self._on_tree_selection_changed
        )
        tree_box.addWidget(self.category_tree, stretch=1)
        self.selected_label = QLabel("선택: 없음 (최상위 카테고리를 1개 이상 선택)")
        self.selected_label.setWordWrap(True)
        tree_box.addWidget(self.selected_label)
        controls_layout.addWidget(tree_panel, stretch=1)

        # ── 실행 설정 ──────────────────────────────────────────────
        settings_group = QGroupBox("실행 설정")
        form = QFormLayout(settings_group)

        self.mode_combo = QComboBox()
        for label, _mode in _MODE_LABELS:
            self.mode_combo.addItem(label)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow("배분 방식:", self.mode_combo)

        self.spin_instances = QSpinBox()
        self.spin_instances.setRange(1, MAX_INSTANCES)
        self.spin_instances.setValue(3)
        self.spin_instances.setToolTip(
            "동시에 운영할 인스턴스 수(1~20). 1번은 직접 회선, 2~N번은 Decodo\n"
            "스티키 회선입니다. Decodo 계정이 없으면 1개로 강등됩니다."
        )
        self.spin_instances.valueChanged.connect(self._on_instance_count_changed)
        form.addRow("인스턴스 수:", self.spin_instances)

        dir_row = QHBoxLayout()
        self.output_dir_edit = QLineEdit(str(_default_output_dir()))
        self.btn_browse = QPushButton("찾아보기")
        self.btn_browse.clicked.connect(self._browse_output_dir)
        dir_row.addWidget(self.output_dir_edit)
        dir_row.addWidget(self.btn_browse)
        form.addRow("출력 폴더:", dir_row)
        controls_layout.addWidget(settings_group)

        # Decodo 미설정 안내 — 시작 전에 강등을 예고한다.
        self.decodo_hint_label = QLabel("")
        self.decodo_hint_label.setWordWrap(True)
        self.decodo_hint_label.setStyleSheet("color: #b45309;")
        self.decodo_hint_label.setVisible(False)
        controls_layout.addWidget(self.decodo_hint_label)

        # ── 제어 버튼 ──────────────────────────────────────────────
        btn_row = QGridLayout()
        btn_row.setHorizontalSpacing(8)
        btn_row.setVerticalSpacing(8)
        self.btn_start = QPushButton("수집 시작")
        self.btn_start.clicked.connect(self._on_start_clicked)
        self.btn_stop = QPushButton("정지")
        self.btn_stop.clicked.connect(self.stop_requested.emit)
        self.btn_open_result = QPushButton("결과 폴더 열기")
        self.btn_open_result.clicked.connect(self._on_open_result_clicked)
        for index, button in enumerate(
            (self.btn_start, self.btn_stop, self.btn_open_result)
        ):
            btn_row.addWidget(button, index // 3, index % 3)
        for column in range(3):
            btn_row.setColumnStretch(column, 1)
        left_layout.addLayout(btn_row)

        # ── 인스턴스 카드 영역 ─────────────────────────────────────
        instance_group = QGroupBox("인스턴스 현황")
        self._instance_area = QVBoxLayout(instance_group)
        self._instance_area.setContentsMargins(4, 4, 4, 4)
        self._instance_area.setSpacing(2)
        header = _InstanceCard(0)
        header.name_label.setText("이름")
        header.line_label.setText("회선")
        header.family_label.setText("현재 가족")
        header.status_label.setText("상태")
        header.totals_label.setText("누적")
        for label in (
            header.name_label,
            header.line_label,
            header.family_label,
            header.status_label,
            header.totals_label,
        ):
            label.setStyleSheet("font-weight: bold; color: #475569;")
        header.setStyleSheet("QFrame { background: #f1f5f9; }")
        self._instance_area.addWidget(header)
        self._instance_cards["header"] = header
        controls_layout.addWidget(instance_group)

        controls_scroll = QScrollArea()
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setWidget(controls)
        left_layout.addWidget(controls_scroll, stretch=1)

        # ── 로그 + 결과 표(collection_workspace 배치 관례) ─────────
        log_panel = QGroupBox("실시간 로그")
        log_layout = QVBoxLayout(log_panel)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        log_layout.addWidget(self.log_view)

        results_panel = QGroupBox("수집 결과 (확보 판매자)")
        results_layout = QVBoxLayout(results_panel)
        self.result_table = QTableWidget(0, len(SELLER_DISPLAY_FIELDS))
        self.result_table.setHorizontalHeaderLabels(
            [COLUMN_LABELS[key] for key in SELLER_DISPLAY_FIELDS]
        )
        result_header = self.result_table.horizontalHeader()
        result_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        result_header.setStretchLastSection(False)
        result_header.setMinimumSectionSize(110)
        for column, width in enumerate((130, 150, 170, 130, 150, 150, 220, 110)):
            self.result_table.setColumnWidth(column, width)
        self.result_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        results_layout.addWidget(self.result_table)

        layout.addWidget(
            collection_workspace(left_column, log_panel, results_panel), stretch=1
        )

    # ── 카테고리 트리 ───────────────────────────────────────────────

    def set_category_groups(
        self,
        groups: list[tuple[str, list[CategoryNode]]],
        fetched_at: str = "",
        total: int = 0,
    ) -> None:
        """트리 채움 — 기존 CategoryPanel.set_category_groups 와 같은 형식."""
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

    def set_cache_label(self, text: str) -> None:
        self.cache_label.setText(text)

    def _selected_items(self) -> list[QTreeWidgetItem]:
        """선택된 항목 중 카테고리 노드(id 보유)만 — 그룹 헤더 제외."""
        return [
            item
            for item in self.category_tree.selectedItems()
            if item.data(0, ROLE_ID)
        ]

    def _family_root_items(self) -> list[QTreeWidgetItem]:
        """가족 루트가 될 선택 항목 — 다른 선택 항목의 하위에 속한 선택 제외.

        예: 부모와 그 자손을 함께 선택해도 부모 1개 가족만 만든다(중복 수집
        방지). '선택된 최상위 노드 각각 = 1개 가족' 규칙의 다중 선택 해석이다.
        """
        selected = self._selected_items()
        selected_set = set(id(item) for item in selected)
        roots: list[QTreeWidgetItem] = []
        for item in selected:
            parent = item.parent()
            while parent is not None:
                if id(parent) in selected_set:
                    break
                parent = parent.parent()
            if parent is None:
                roots.append(item)
        return roots

    def _selected_families(self) -> list[list[tuple[str, str]]]:
        """선택 → 가족 목록. 가족 = 루트 노드 + 모든 후손(id 중복 첫 1회)."""
        families: list[list[tuple[str, str]]] = []
        for item in self._family_root_items():
            node = find_node(self._groups, str(item.data(0, ROLE_ID)))
            if node is None:
                continue
            family: list[tuple[str, str]] = []
            seen: set[str] = set()
            for target in (node, *flatten_descendants(node)):
                if target.id and target.id not in seen:
                    seen.add(target.id)
                    family.append((target.id, target.name))
            if family:
                families.append(family)
        return families

    def _on_tree_selection_changed(self) -> None:
        self._refresh_selection_preview()

    def _mode(self) -> str:
        """현재 배분 방식 — 콤보 인덱스 → 모드 문자."""
        index = self.mode_combo.currentIndex()
        if 0 <= index < len(_MODE_LABELS):
            return _MODE_LABELS[index][1]
        return MODE_FAMILY

    def _on_mode_changed(self) -> None:
        self._refresh_selection_preview()

    def _refresh_selection_preview(self) -> None:
        """선택 → 선택 라벨·카드 가족명 갱신(모드별 안내)."""
        if self._mode() == MODE_SHARD:
            selection = self._split_selection()
            if selection is None:
                self.selected_label.setText(
                    "선택: 없음 (분할할 카테고리 최상위 1개를 선택)"
                )
                self._families = []
                self._family_names = []
                return
            family, volumes = selection
            instance_count = self.spin_instances.value()
            units = plan_work_units(family, volumes, instance_count)
            summary = plan_summary(family, volumes, units)
            self._families = [list(unit.categories) for unit in units]
            # 통짜 샤드는 종전 표기(샤드 N)를 유지하고 조각만 단위 라벨로.
            self._family_names = [
                f"샤드 {index + 1} (카테고리 {len(unit.categories)}개)"
                if unit.kind == WORK_KIND_WHOLE
                else unit.label
                for index, unit in enumerate(units)
            ]
            if summary["volume_known"]:
                # §6 미리보기 — 사용자가 계획을 검증하는 정찰 철학.
                volume_note = (
                    f" — 총 물량 {summary['total_volume']:,}개 → 인스턴스 "
                    f"{instance_count} × 1단위 약 "
                    f"{max(1, summary['total_volume'] // summary['unit_count']):,}개"
                )
                if summary["split_category_count"]:
                    volume_note += (
                        f", 큰 카테고리 {summary['split_category_count']}개를"
                        " 조각 분할(목록 페이지 범위·판매자 슬라이스)"
                    )
                self.selected_label.setText(
                    f"선택: {family[0][1]} — 카테고리 {len(family):,}개를 "
                    f"작업 단위 {summary['unit_count']}개로 분할{volume_note}"
                )
                return
            # 물량 미지(productCount 없음) — 라운드로빈 균등 분할 안내(현행).
            if len(self._families) < instance_count:
                note = (
                    f" — 카테고리 {len(family)}개라 최대 {len(self._families)}"
                    " 인스턴스만 병렬로 동작합니다"
                    "(카테고리 새로고침 후 물량 정보가 오면 조각 분할)"
                )
            else:
                note = ""
            self.selected_label.setText(
                f"선택: {family[0][1]} — 카테고리 {len(family):,}개를 "
                f"{len(self._families)}개 샤드로 분할{note}"
            )
            return
        self._families = self._selected_families()
        self._family_names = [family[0][1] for family in self._families]
        if not self._families:
            self.selected_label.setText(
                "선택: 없음 (최상위 카테고리를 1개 이상 선택)"
            )
            return
        total_categories = sum(len(family) for family in self._families)
        self.selected_label.setText(
            f"선택: 가족 {len(self._families)}개 · 총 카테고리 {total_categories:,}개"
        )

    def _split_selection(self) -> tuple[list[tuple[str, str]], dict[str, int]] | None:
        """분할 모드용 단일 가족 + 카테고리별 물량(productCount, §4).

        루트가 여러 개 선택돼 있으면 첫 번째만 본다(나머지는 무시하고
        시작 검증에서 다시 안내한다). volumes 는 productCount 가 붙은
        카테고리만 담는 — 없으면 빈 사전(계획은 라운드로빈 폴백).
        """
        roots = self._family_root_items()
        if not roots:
            return None
        item = roots[0]
        node = find_node(self._groups, str(item.data(0, ROLE_ID)))
        if node is None:
            return None
        family: list[tuple[str, str]] = []
        volumes: dict[str, int] = {}
        seen: set[str] = set()
        for target in (node, *flatten_descendants(node)):
            if target.id and target.id not in seen:
                seen.add(target.id)
                family.append((target.id, target.name))
                if target.product_count > 0:
                    volumes[target.id] = int(target.product_count)
        return (family, volumes) if family else None

    def _single_split_family(self) -> list[tuple[str, str]] | None:
        """분할 모드용 단일 가족 목록(호환 래퍼) — 물량은 함께 읽지 않는다."""
        selection = self._split_selection()
        return selection[0] if selection is not None else None

    def _probe_volumes(self, family: list[tuple[str, str]]) -> dict[str, int]:
        """물량 미지 가족의 계획 조사(§4 2순위) — 목록 1페이지 probe.

        트리 응답에 productCount 가 없으면(2026-10-07 실측: 전 노드 미부착)
        카테고리당 목록 1페이지로 페이지 수를 읽어 물량을 추정한다 — 직접
        회선이 차단돼 있으면 Decodo 조사 전용 회선(sid i990)으로 재시도,
        같은 가족을 24시간 이내에 조사했으면 캐시를 재사용한다(쿠팡 접촉
        최소화). 조사 중에는 진행 상황을 로그로 남기고 이벤트 루프를 돌려
        UI가 멈춘 것처럼 보이지 않게 한다. 실패하면 빈 사전(라운드로빈
        균등 분할로 시작 — 계획은 근사일 뿐이므로 안전하다).
        """
        from PyQt6.QtWidgets import QApplication

        from app.core.config import DEFAULT_OUTPUT_DIR
        from app.core.coupang.volume_probe import (
            PROBE_CACHE_FILENAME,
            plan_volume_probe,
        )

        self.append_log(
            f"[계획] 물량 정보(productCount)가 없어 목록 1페이지 조사를"
            f" 합니다 — 카테고리 {len(family)}개(카테고리당 1회,"
            " 직접 회선 → 차단 시 Decodo 조사 회선)."
        )

        def progress(name: str, volume: int, index: int, total: int) -> None:
            QApplication.processEvents()
            self.append_log(
                f"[계획] ({index}/{total}) {name}: 약 {volume:,}개"
            )

        try:
            return plan_volume_probe(
                family,
                on_progress=progress,
                on_cached=lambda: self.append_log(
                    "[계획] 최근 조사 결과(24시간 이내)를 재사용합니다."
                ),
                cache_path=DEFAULT_OUTPUT_DIR / PROBE_CACHE_FILENAME,
            )
        except Exception as error:  # noqa: BLE001 - 조사 실패는 균등 분할 폴백
            self.append_log(
                f"[계획] 물량 조사 실패({type(error).__name__}: {error})"
                " — 라운드로빈 균등 분할로 시작합니다."
            )
            return {}

    # ── 인스턴스 카드 ───────────────────────────────────────────────

    def set_instance_count_preview(self, count: int) -> None:
        """시작 전 인스턴스 수만큼 카드를 미리 그린다."""
        self._rebuild_cards(max(1, int(count)))

    def _rebuild_cards(self, count: int) -> None:
        self._clear_instance_cards()
        for number in range(1, count + 1):
            self._make_instance_card(number)

    def _clear_instance_cards(self) -> None:
        """인스턴스 카드만 제거(헤더 행은 유지)."""
        for key, card in list(self._instance_cards.items()):
            if key == "header":
                continue
            self._instance_area.removeWidget(card)
            card.setParent(None)
            card.deleteLater()
            del self._instance_cards[key]

    def _make_instance_card(self, number: int) -> _InstanceCard:
        card = _InstanceCard(number)
        card.family_name_lookup = self._family_name_for
        self._instance_area.addWidget(card)
        self._instance_cards[str(number)] = card
        return card

    def _family_name_for(self, family_index) -> str:
        """카드 표기용 가족명 — 확정된 선택이 없으면 빈 문자열(기본 표기)."""
        index = int(family_index)
        if 0 <= index < len(self._family_names):
            return self._family_names[index]
        return ""

    def update_instance(self, instance_id: str, state_str: str, summary: dict) -> None:
        """워커 instance_state_changed 수신 — 카드를 만들거나 갱신한다."""
        key = str(instance_id)
        card = self._instance_cards.get(key)
        if card is None:
            card = self._make_instance_card(int(key) if key.isdigit() else 1)
        card.update_state(state_str, summary)

    def append_unit_names(self, labels: list) -> None:
        """실행 중 배출된 작업 단위(판매자 조각) 라벨을 카드 표기에 추가.

        배출 규칙(§5.3)이 대기 큐에 늘린 단위는 시작 시점 계획에 없어
        _family_names 범위 밖 인덱스가 된다 — 워커의 units_appended 시그널로
        라벨을 받아 카드 표기가 기본 라벨로 떨어지지 않게 한다.
        """
        self._family_names.extend(str(label) for label in labels)

    # ── 상태 머신 ───────────────────────────────────────────────────

    def set_state(self, state: str) -> None:
        self._state = state
        self._apply_state()

    def _apply_state(self) -> None:
        s = self._state
        startable = s in ("idle", "finished", "failed")
        self.btn_start.setEnabled(startable)
        self.btn_stop.setEnabled(s == "running")
        self.btn_open_result.setEnabled(True)
        settings_enabled = startable
        for widget in (
            self.spin_instances,
            self.output_dir_edit,
            self.mode_combo,
        ):
            widget.setEnabled(settings_enabled)
        self.btn_browse.setEnabled(settings_enabled)
        self.category_tree.setEnabled(settings_enabled)
        self.btn_refresh_categories.setEnabled(
            settings_enabled and s != "loading_categories"
        )

    def set_loading_categories(self, loading: bool) -> None:
        self.btn_refresh_categories.setEnabled(not loading)
        self.category_tree.setEnabled(not loading)
        if loading:
            self.cache_label.setText(
                "카테고리 목록을 불러오는 중... (쿠팡 접속, 약 30초)"
            )

    def set_external_busy(self, busy: bool) -> None:
        """다른 탭 작업 실행 시 조작을 안전하게 잠근다."""
        self._external_busy = busy
        if busy:
            for button in (
                self.btn_start,
                self.btn_stop,
                self.btn_refresh_categories,
            ):
                button.setEnabled(False)
            for widget in (self.spin_instances, self.output_dir_edit, self.mode_combo):
                widget.setEnabled(False)
            self.btn_browse.setEnabled(False)
            self.category_tree.setEnabled(False)
        else:
            self._apply_state()

    # ── 설정/데이터 ────────────────────────────────────────────────

    def output_dir(self) -> str:
        return self.output_dir_edit.text().strip()

    def build_run_config(
        self, output_dir: Path | str | None = None, instance_count: int | None = None
    ):
        """선택된 가족/샤드로 ParallelRunConfig 를 만든다. 선택 없으면 None+안내."""
        # 지연 import — 패널 모듈 로드 시 코어 의존을 최소화한다.
        from app.core.coupang.parallel_manager import ParallelRunConfig

        shard_mode = self._mode() == MODE_SHARD
        unit_specs: list[dict] | None = None
        root_family: list[tuple[str, str]] | None = None
        if shard_mode:
            selection = self._split_selection()
            if selection is None:
                self.append_log(
                    "[시작] 분할 모드에서는 카테고리 최상위 노드 1개를 선택하세요."
                )
                return None
            family, volumes = selection
            count = (
                int(instance_count)
                if instance_count is not None
                else self.spin_instances.value()
            )
            if not volumes:
                # §4 2순위 — 트리에 productCount 가 없으면 목록 1페이지
                # 조사로 물량을 추정한다(카테고리당 요청 1회, 직접 회선).
                volumes = self._probe_volumes(family)
            # 물량 가중 작업 단위(§5) — 물량 유무에 따라 조각 분할이 결정된다.
            # 미지면 라운드로빈 whole 유닛(현행과 동일 결과).
            units = plan_work_units(family, volumes, count)
            families = [list(unit.categories) for unit in units]
            unit_specs = [unit_to_dict(unit) for unit in units]
            root_family = [tuple(pair) for pair in family]
            # probe 가 시작 시점에 계획을 다시 세우면 미리보기 때보다 단위가
            # 늘어난다 — 카드 표기도 새 계획의 라벨로 갱신한다(기본 라벨 방지).
            self._family_names = [
                f"샤드 {index + 1} (카테고리 {len(unit.categories)}개)"
                if unit.kind == WORK_KIND_WHOLE
                else unit.label
                for index, unit in enumerate(units)
            ]
            if len(families) < count:
                self.append_log(
                    f"[시작] 작업 단위 {len(families)}개라 인스턴스 "
                    f"{len(families)}개로 축소합니다 — 이 가족은 더 잘게"
                    " 쪼갤 만큼 크지 않습니다(물량 조사에 실패했으면"
                    " 카테고리 새로고침 후 다시 시도해도 됩니다)."
                )
                count = len(families)
        else:
            families = self._selected_families()
            if not families:
                self.append_log(
                    "[시작] 카테고리를 선택하세요 — 최상위 노드 각각이 1개 가족입니다."
                )
                return None
            count = (
                int(instance_count)
                if instance_count is not None
                else self.spin_instances.value()
            )
        chosen_dir = Path(output_dir) if output_dir else Path(self.output_dir())
        if not str(chosen_dir) or str(chosen_dir) == ".":
            self.append_log("[시작] 출력 폴더를 지정하세요.")
            return None
        try:
            return ParallelRunConfig(
                families=families,
                output_dir=chosen_dir,
                instance_count=count,
                shard_mode=shard_mode,
                unit_specs=unit_specs,
                root_family=root_family,
            )
        except ValueError as error:
            self.append_log(f"[시작] 실행 설정이 올바르지 않습니다: {error}")
            return None

    # ── 버튼 동작 ───────────────────────────────────────────────────

    def _on_start_clicked(self) -> None:
        if self._state != "idle":
            return
        config = self.build_run_config()
        if config is None:
            return  # build_run_config 이 안내 로그를 남겼다
        # payload 는 직렬화 가능한 값만 — preflight·재개 판단은 main_window.
        self.start_requested.emit(
            {
                "output_dir": str(config.output_dir),
                "instance_count": config.instance_count,
                "family_count": len(config.families),
                "interval_minutes": config.interval_minutes,
                "shard_mode": config.shard_mode,
            }
        )

    def _on_open_result_clicked(self) -> None:
        folder = self.output_dir()
        if not folder:
            self.append_log("[결과 열기] 저장 폴더가 비어 있습니다.")
            return
        self.open_output_requested.emit(folder)

    def _browse_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "출력 폴더 선택", self.output_dir()
        )
        if path:
            self.output_dir_edit.setText(path)

    # ── Decodo 강등 예고 ────────────────────────────────────────────

    def _on_instance_count_changed(self, count: int) -> None:
        self.set_instance_count_preview(count)
        self._refresh_selection_preview()  # 샤드 수 미리보기 갱신
        self._update_decodo_hint()

    def _decodo_ready(self) -> bool:
        try:
            from app.core import decodo
        except Exception:  # noqa: BLE001 - 모듈 부재는 미설정과 같다
            return False
        try:
            return bool(decodo.credentials_ready())
        except Exception:  # noqa: BLE001 - 설정 파일 손상도 미설정과 같다
            return False

    def _update_decodo_hint(self) -> None:
        """Decodo 미설정 + 인스턴스 수>1 → 강등 예고 라벨."""
        needed = self.spin_instances.value() > 1 and not self._decodo_ready()
        if needed:
            self.decodo_hint_label.setText(
                "Decodo 계정이 설정되지 않았습니다 — 시작하면 인스턴스 1개"
                "(직접 회선)로 강등됩니다. 설정 탭에서 Decodo 계정을 저장하면 "
                "인스턴스를 늘릴 수 있습니다."
            )
            self.decodo_hint_label.setVisible(True)
        else:
            self.decodo_hint_label.setVisible(False)

    # ── 로그/표시 ───────────────────────────────────────────────────

    def append_log(self, msg: str) -> None:
        log_line(f"[병렬] {msg}")
        timestamp = datetime.now().astimezone().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{timestamp}] {msg}")

    def add_seller_rows(self, rows: list) -> None:
        """워커 sellers_appended 수신 — 결과 표에 행을 붙인다."""
        for row in rows:
            self.add_record(row)

    def add_record(self, record: dict) -> None:
        row = self.result_table.rowCount()
        self.result_table.insertRow(row)
        for col, key in enumerate(SELLER_DISPLAY_FIELDS):
            value = record.get(key, "")
            if key == "power_seller":
                value = "✓" if value else ""
            self.result_table.setItem(row, col, QTableWidgetItem(str(value)))

    def clear_results(self) -> None:
        """시작 시 결과 표·로그·인스턴스 카드를 초기화한다(헤더 유지)."""
        self.result_table.setRowCount(0)
        self.log_view.clear()
        self._clear_instance_cards()
