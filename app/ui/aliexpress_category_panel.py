"""AliExpress 카테고리 탭 패널 — UI/UX Pro Max 디자인 및 2단계 무인 파이프라인.

기능:
1. 카테고리 선택: 계층형 트리 선택(식품, 패션, 가전 등) 및 직접 URL/키워드 입력 모드
2. 수집 설정: 최대 페이지, 회선 자동 순환 주기, 딜레이, 출력 디렉토리
3. 실시간 제어: 수집 시작, 일시정지, 재개, 취소, 결과 폴더 열기
4. 실시간 대시보드: Phase 진행 게이지, 고유 판매자 수, 7대 필드 채움률 배지
5. 실시간 데이터 테이블: 대표자, 이메일, 사업자번호, 상호 등 즉시 렌더링
6. 실시간 로그 창: 세션 로테이션 및 파이프라인 상태 출력
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core import config
from app.core.aliexpress_category_crawler import (
    AliexpressCategoryRunConfig,
    AliexpressCrawlSummary,
)
from app.ui.widgets.collection_workspace import collection_workspace

ROLE_ID = 0x0100
ROLE_URL = 0x0101
ROLE_NAME = 0x0102

DISPLAY_COLUMNS = [
    "상호명",
    "대표자",
    "사업자번호",
    "이메일",
    "전화번호",
    "스토어명",
    "상품명",
    "수집시각"
]
COLUMN_KEYS = [
    "company_name",
    "ceo_name",
    "business_number",
    "email",
    "phone",
    "store_name",
    "product_title",
    "collected_at"
]


class AliexpressCategoryPanel(QWidget):
    """'Ali 카테고리' 탭 위젯."""

    start_requested = pyqtSignal(AliexpressCategoryRunConfig)
    pause_requested = pyqtSignal()
    resume_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    open_output_requested = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._state = "idle"
        self._last_output_dir: str = str(config.DEFAULT_OUTPUT_DIR / "aliexpress")
        self._build_ui()
        self._load_category_tree()
        self._apply_state()

    def _build_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(12, 12, 12, 12)

        # ── 1. 상단 안내 및 상태 배너 카드 ─────────────────────────────────
        banner_frame = QFrame()
        banner_frame.setStyleSheet("""
            QFrame {
                background-color: #f8fafc;
                border: 1px solid #e2e8f0;
                border-radius: 8px;
                padding: 10px;
            }
        """)
        banner_layout = QHBoxLayout(banner_frame)
        banner_layout.setContentsMargins(8, 4, 8, 4)

        info_icon = QLabel("🛒")
        info_icon.setStyleSheet("font-size: 24px;")
        banner_layout.addWidget(info_icon)

        info_text = QLabel(
            "<b>AliExpress 카테고리 무인 수집기</b> &nbsp;|&nbsp; "
            "대량 무인 수집(Decodo 주거용 회선 자동 순환)과 비용 없는 로컬 회선 안전 수집(0원 수집)을 모두 지원합니다.<br>"
            "<span style='color: #475569;'>• 프록시 등록 시: <b>25건 단위 자동 회선 순환</b>으로 플랫폼 차단 방지 "
            "• 프록시 미등록 시: <b>안전 지연 시간(3~4초)</b> 적용 로컬 수집 "
            "• 쿠팡 카테고리와 동일한 규격(이메일, 대표자, 사업자번호 등 7대 핵심 정보)을 100% 완전 무인으로 수집합니다.</span>"
        )
        info_text.setWordWrap(True)
        info_text.setStyleSheet("color: #1e293b; font-size: 12px; line-height: 1.4;")
        banner_layout.addWidget(info_text, stretch=1)

        self.badge_status = QLabel("🟢 회선 게이트웨이 준비완료")
        self.badge_status.setStyleSheet("""
            background-color: #ecfdf5;
            color: #065f46;
            border: 1px solid #a7f3d0;
            border-radius: 6px;
            padding: 4px 10px;
            font-size: 11px;
            font-weight: bold;
        """)
        banner_layout.addWidget(self.badge_status)

        main_layout.addWidget(banner_frame)

        # ── 2. 작업 영역: 왼쪽 조작 패널 / 오른쪽 로그 / 아래 결과표 ────────
        controls_container = QWidget()
        controls_layout = QVBoxLayout(controls_container)
        controls_layout.setContentsMargins(0, 0, 8, 0)
        controls_layout.setSpacing(10)

        # ── [좌측] 카테고리 선택 컨테이너 ───────────────────────────────
        left_container = QGroupBox("카테고리 대상 선택")
        left_layout = QVBoxLayout(left_container)
        left_layout.setSpacing(6)

        self.input_tabs = QTabWidget()

        # 탭 A: 계층형 트리 선택
        tree_tab = QWidget()
        tree_tab_layout = QVBoxLayout(tree_tab)
        tree_tab_layout.setContentsMargins(4, 4, 4, 4)
        tree_tab_layout.setSpacing(4)

        self.tree_search_box = QLineEdit()
        self.tree_search_box.setPlaceholderText("🔍 카테고리 검색 (예: 야채, 과일, 여성 의류, 가전)...")
        self.tree_search_box.setClearButtonEnabled(True)
        self.tree_search_box.textChanged.connect(self._filter_tree)
        tree_tab_layout.addWidget(self.tree_search_box)

        self.category_tree = QTreeWidget()
        self.category_tree.setHeaderLabels(["카테고리명", "구분"])
        self.category_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.category_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.category_tree.setStyleSheet("""
            QTreeWidget {
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 4px;
                background-color: #ffffff;
            }
            QTreeWidget::item {
                padding: 4px;
            }
            QTreeWidget::item:selected {
                background-color: #e0f2fe;
                color: #0369a1;
            }
        """)
        self.category_tree.itemSelectionChanged.connect(self._on_tree_selected)
        tree_tab_layout.addWidget(self.category_tree)

        self.input_tabs.addTab(tree_tab, "카테고리 트리에서 선택")

        # 탭 B: 직접 URL / 키워드 입력
        direct_tab = QWidget()
        direct_tab_layout = QVBoxLayout(direct_tab)
        direct_tab_layout.setContentsMargins(6, 6, 6, 6)
        direct_tab_layout.setSpacing(8)

        direct_tab_layout.addWidget(QLabel("수집할 카테고리 명칭:"))
        self.input_direct_name = QLineEdit()
        self.input_direct_name.setPlaceholderText("예: 여성 캐주얼 드레스, 캠핑용품 등")
        direct_tab_layout.addWidget(self.input_direct_name)

        direct_tab_layout.addWidget(QLabel("알리익스프레스 카테고리/검색 URL:"))
        self.input_direct_url = QLineEdit()
        self.input_direct_url.setPlaceholderText("https://ko.aliexpress.com/w/wholesale-...html?categoryTab=...")
        direct_tab_layout.addWidget(self.input_direct_url)

        direct_hint = QLabel(
            "💡 웹 브라우저에서 원하는 알리익스프레스 카테고리로 이동한 뒤,\n"
            "주소창의 URL을 그대로 복사하여 붙여넣으면 해당 카테고리를 전량 수집합니다."
        )
        direct_hint.setStyleSheet("color: #64748b; font-size: 11px;")
        direct_tab_layout.addWidget(direct_hint)
        direct_tab_layout.addStretch()

        self.input_tabs.addTab(direct_tab, "직접 URL 입력")
        self.input_tabs.currentChanged.connect(self._on_input_tab_changed)
        self.input_direct_name.textChanged.connect(self._on_direct_input_changed)

        left_layout.addWidget(self.input_tabs)

        # 선택된 카테고리 요약 라벨
        self.selected_target_label = QLabel("선택된 카테고리: 없음 (트리에서 선택하거나 직접 입력하세요)")
        self.selected_target_label.setStyleSheet("font-weight: bold; color: #2563eb; padding: 2px 4px;")
        left_layout.addWidget(self.selected_target_label)

        controls_layout.addWidget(left_container)

        # ── 수집 옵션 설정 컨테이너 ─────────────────────────────────────
        options_container = QGroupBox("수집 옵션 및 파라미터")
        options_layout = QVBoxLayout(options_container)
        options_layout.setSpacing(8)

        form_layout = QFormLayout()
        form_layout.setSpacing(10)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.spin_max_pages = QSpinBox()
        self.spin_max_pages.setRange(1, 100)
        self.spin_max_pages.setValue(30)
        self.spin_max_pages.setSuffix(" 페이지")
        self.spin_max_pages.setToolTip("탐색할 최대 카테고리 페이지 수입니다. (기본 30페이지)")
        form_layout.addRow("최대 탐색 페이지:", self.spin_max_pages)

        self.spin_rotation_batch = QSpinBox()
        self.spin_rotation_batch.setRange(10, 100)
        self.spin_rotation_batch.setValue(25)
        self.spin_rotation_batch.setSuffix(" 건 마다")
        self.spin_rotation_batch.setToolTip("플랫폼 차단을 예방하기 위해 프록시 회선 세션을 자동 교체하는 주기입니다.")
        form_layout.addRow("회선 자동 순환:", self.spin_rotation_batch)

        self.edit_output_dir = QLineEdit(self._last_output_dir)
        btn_browse = QPushButton("찾아보기...")
        btn_browse.clicked.connect(self._browse_output_dir)
        out_layout = QHBoxLayout()
        out_layout.setContentsMargins(0, 0, 0, 0)
        out_layout.addWidget(self.edit_output_dir)
        out_layout.addWidget(btn_browse)
        form_layout.addRow("저장 폴더:", out_layout)

        options_layout.addLayout(form_layout)

        # 수집 안전 규율 배너
        safety_box = QFrame()
        safety_box.setStyleSheet("""
            QFrame {
                background-color: #f1f5f9;
                border: 1px dashed #cbd5e1;
                border-radius: 6px;
                padding: 8px;
            }
        """)
        s_layout = QVBoxLayout(safety_box)
        s_layout.setContentsMargins(4, 4, 4, 4)
        s_layout.addWidget(QLabel("<b>🛡 품질 & 안전성 자동화 보장:</b>"))
        s_text = QLabel(
            "• <b>완전 무인 100%</b>: 화면 캡차 풀기 등 사용자 수동 개입 제로<br>"
            "• <b>스마트 캐싱</b>: 기확보된 판매자 정보 자동 재활용으로 중복 최소화<br>"
            "• <b>실시간 내구성</b>: 1건 수집 즉시 CSV/JSON 동시 기록 (유실 방지)"
        )
        s_text.setStyleSheet("color: #334155; font-size: 11px;")
        s_layout.addWidget(s_text)
        options_layout.addWidget(safety_box)
        controls_layout.addWidget(options_container)

        # ── 3. 액션 제어 버튼 바 ────────────────────────────────────────
        action_bar = QGridLayout()
        action_bar.setHorizontalSpacing(8)
        action_bar.setVerticalSpacing(6)

        self.btn_start = QPushButton("▶ 수집 시작")
        self.btn_start.setStyleSheet("""
            QPushButton {
                background-color: #2563eb;
                color: white;
                font-weight: bold;
                font-size: 13px;
                padding: 8px 18px;
                border-radius: 6px;
            }
            QPushButton:hover { background-color: #1d4ed8; }
            QPushButton:disabled { background-color: #94a3b8; }
        """)
        self.btn_start.clicked.connect(self._on_start_clicked)
        action_bar.addWidget(self.btn_start, 0, 0, 1, 2)

        self.btn_pause = QPushButton("⏸ 일시정지")
        self.btn_pause.clicked.connect(self.pause_requested.emit)
        action_bar.addWidget(self.btn_pause, 1, 0)

        self.btn_resume = QPushButton("▶ 재개")
        self.btn_resume.clicked.connect(self.resume_requested.emit)
        action_bar.addWidget(self.btn_resume, 1, 1)

        self.btn_cancel = QPushButton("⏹ 취소")
        self.btn_cancel.clicked.connect(self.cancel_requested.emit)
        action_bar.addWidget(self.btn_cancel, 2, 0)

        self.btn_open_folder = QPushButton("📁 결과 폴더 열기")
        self.btn_open_folder.clicked.connect(self._open_output_folder)
        action_bar.addWidget(self.btn_open_folder, 2, 1)

        action_box = QGroupBox("수집 제어")
        action_box.setLayout(action_bar)

        # ── 4. 실시간 대시보드 메트릭 카드 ───────────────────────────────
        metrics_frame = QFrame()
        metrics_frame.setStyleSheet("""
            QFrame {
                background-color: #ffffff;
                border: 1px solid #e2e8f0;
                border-radius: 8px;
                padding: 6px;
            }
        """)
        metrics_layout = QVBoxLayout(metrics_frame)
        metrics_layout.setContentsMargins(8, 6, 8, 6)
        metrics_layout.setSpacing(6)

        top_progress_layout = QHBoxLayout()
        self.label_phase = QLabel("준비 완료 (수집 시작 대기)")
        self.label_phase.setStyleSheet("font-weight: bold; color: #1e293b;")
        self.label_progress = QLabel("진행: 0 / 0건 (0%)")
        self.label_progress.setStyleSheet("color: #64748b; font-size: 12px;")
        top_progress_layout.addWidget(self.label_phase)
        top_progress_layout.addStretch()
        top_progress_layout.addWidget(self.label_progress)
        metrics_layout.addLayout(top_progress_layout)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(8)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                background-color: #f1f5f9;
                border-radius: 4px;
            }
            QProgressBar::chunk {
                background-color: #2563eb;
                border-radius: 4px;
            }
        """)
        metrics_layout.addWidget(self.progress_bar)

        # 4개 지표 카드
        cards_layout = QGridLayout()
        cards_layout.setHorizontalSpacing(8)
        cards_layout.setVerticalSpacing(8)

        self.card_total = self._create_stat_card("수집된 상품", "0건")
        self.card_vendors = self._create_stat_card("고유 입점 판매자", "0개사")
        self.card_emails = self._create_stat_card("이메일 확보율", "0% (0건)")
        self.card_biznums = self._create_stat_card("사업자번호 확보율", "0% (0건)")

        cards_layout.addWidget(self.card_total, 0, 0)
        cards_layout.addWidget(self.card_vendors, 0, 1)
        cards_layout.addWidget(self.card_emails, 1, 0)
        cards_layout.addWidget(self.card_biznums, 1, 1)

        metrics_layout.addLayout(cards_layout)
        metrics_box = QGroupBox("실시간 수집 현황")
        metrics_box_layout = QVBoxLayout(metrics_box)
        metrics_box_layout.setContentsMargins(0, 0, 0, 0)
        metrics_box_layout.addWidget(metrics_frame)
        controls_layout.addWidget(metrics_box)
        controls_layout.addStretch()

        controls_scroll = QScrollArea()
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setFrameShape(QFrame.Shape.NoFrame)
        controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        controls_scroll.setWidget(controls_container)

        controls_panel = QWidget()
        controls_panel_layout = QVBoxLayout(controls_panel)
        controls_panel_layout.setContentsMargins(0, 0, 0, 0)
        controls_panel_layout.setSpacing(8)
        controls_panel_layout.addWidget(action_box)
        controls_panel_layout.addWidget(controls_scroll, stretch=1)

        # ── 5. 하단 뷰 (실시간 결과 테이블 & 로그 분할) ───────────────────
        # 결과 테이블
        table_container = QWidget()
        t_layout = QVBoxLayout(table_container)
        t_layout.setContentsMargins(0, 0, 0, 0)
        t_head = QLabel("<b>실시간 수집 결과 미리보기 (최근 수집순)</b>")
        t_layout.addWidget(t_head)

        self.result_table = QTableWidget(0, len(DISPLAY_COLUMNS))
        self.result_table.setHorizontalHeaderLabels(DISPLAY_COLUMNS)
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.result_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.result_table.horizontalHeader().setStretchLastSection(True)
        self.result_table.setColumnWidth(0, 150)  # 상호명
        self.result_table.setColumnWidth(1, 100)  # 대표자
        self.result_table.setColumnWidth(2, 120)  # 사업자번호
        self.result_table.setColumnWidth(3, 180)  # 이메일
        self.result_table.setColumnWidth(4, 130)  # 전화번호
        self.result_table.setColumnWidth(5, 140)  # 스토어명
        t_layout.addWidget(self.result_table)
        # 오른쪽 로그 콘솔
        log_container = QWidget()
        l_layout = QVBoxLayout(log_container)
        l_layout.setContentsMargins(0, 0, 0, 0)
        l_head = QLabel("<b>실시간 실행 로그</b>")
        l_layout.addWidget(l_head)

        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumBlockCount(1000)
        self.log_text.setStyleSheet("""
            QPlainTextEdit {
                background-color: #0f172a;
                color: #f8fafc;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 11px;
                border-radius: 6px;
                padding: 6px;
            }
        """)
        l_layout.addWidget(self.log_text)
        workspace = collection_workspace(controls_panel, log_container, table_container)
        main_layout.addWidget(workspace, stretch=1)

    def _create_stat_card(self, title: str, initial_value: str) -> QFrame:
        card = QFrame()
        card.setStyleSheet("""
            QFrame {
                background-color: #f8fafc;
                border: 1px solid #e2e8f0;
                border-radius: 6px;
                padding: 4px;
            }
        """)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)

        t_lbl = QLabel(title)
        t_lbl.setStyleSheet("color: #64748b; font-size: 11px;")
        v_lbl = QLabel(initial_value)
        v_lbl.setStyleSheet("color: #0f172a; font-size: 14px; font-weight: bold;")
        v_lbl.setObjectName("value_label")

        layout.addWidget(t_lbl)
        layout.addWidget(v_lbl)
        return card

    # ── 카테고리 트리 로드 및 필터 ─────────────────────────────────────
    def _load_category_tree(self) -> None:
        candidates: list[Path] = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "app" / "resources" / "aliexpress_category_tree.json")
        candidates.append(Path(__file__).resolve().parents[2] / "app" / "resources" / "aliexpress_category_tree.json")
        candidates.append(config.PROJECT_ROOT / "app" / "resources" / "aliexpress_category_tree.json")
        candidates.append(Path.cwd() / "app" / "resources" / "aliexpress_category_tree.json")

        tree_file: Path | None = None
        for p in candidates:
            if p.exists() and p.is_file():
                tree_file = p
                break

        if tree_file is None:
            self.append_log("[안내] 기본 카테고리 트리를 불러올 수 없습니다. '직접 URL 입력' 탭을 이용해 주세요.")
            self.input_tabs.setCurrentIndex(1)
            return

        try:
            with open(tree_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            if not isinstance(data, list) or not data:
                raise ValueError("카테고리 트리가 비어 있거나 올바르지 않습니다.")

            self.category_tree.clear()
            for cat in data:
                top_item = QTreeWidgetItem([cat.get("name", ""), "대분류"])
                top_item.setData(0, ROLE_ID, cat.get("id", ""))
                top_item.setData(0, ROLE_URL, cat.get("url", ""))
                top_item.setData(0, ROLE_NAME, cat.get("name", ""))

                for sub in cat.get("children", []):
                    sub_item = QTreeWidgetItem([sub.get("name", ""), "소분류"])
                    sub_item.setData(0, ROLE_ID, sub.get("id", ""))
                    sub_item.setData(0, ROLE_URL, sub.get("url", ""))
                    sub_item.setData(0, ROLE_NAME, sub.get("name", ""))
                    top_item.addChild(sub_item)

                self.category_tree.addTopLevelItem(top_item)

            self.category_tree.expandAll()

            # 기본으로 첫 번째 소분류(야채) 선택
            if self.category_tree.topLevelItemCount() > 0:
                first_top = self.category_tree.topLevelItem(0)
                if first_top.childCount() > 0:
                    first_sub = first_top.child(0)
                    first_sub.setSelected(True)
                    self.category_tree.setCurrentItem(first_sub)
                    self._on_tree_selected()
        except Exception as e:
            self.append_log(f"[카테고리 트리 오류] {e}")
            self.append_log("[안내] 기본 카테고리 트리를 불러올 수 없습니다. '직접 URL 입력' 탭을 이용해 주세요.")
            self.input_tabs.setCurrentIndex(1)

    def _filter_tree(self, query: str) -> None:
        query = query.strip().lower()
        root_count = self.category_tree.topLevelItemCount()
        for i in range(root_count):
            top_item = self.category_tree.topLevelItem(i)
            top_match = query in top_item.text(0).lower()
            any_child_match = False
            for j in range(top_item.childCount()):
                child_item = top_item.child(j)
                child_match = query in child_item.text(0).lower()
                child_item.setHidden(not (child_match or top_match))
                if child_match:
                    any_child_match = True
            top_item.setHidden(not (top_match or any_child_match))

    def _on_tree_selected(self) -> None:
        items = self.category_tree.selectedItems()
        if not items:
            self.selected_target_label.setText("선택된 카테고리: 없음 (트리에서 세부 카테고리를 선택하세요)")
            self.selected_target_label.setStyleSheet("font-weight: bold; color: #64748b; padding: 2px 4px;")
            return
        item = items[0]
        url = item.data(0, ROLE_URL)
        name = item.data(0, ROLE_NAME)
        if url:
            self.selected_target_label.setText(f"선택된 카테고리: <b>{name}</b>")
            self.selected_target_label.setStyleSheet("font-weight: bold; color: #16a34a; padding: 2px 4px;")

    def _on_input_tab_changed(self, index: int) -> None:
        if index == 0:
            self._on_tree_selected()
        else:
            self._on_direct_input_changed()

    def _on_direct_input_changed(self) -> None:
        if self.input_tabs.currentIndex() == 1:
            name = self.input_direct_name.text().strip()
            if name:
                self.selected_target_label.setText(f"직접 입력 대상: <b>{name}</b>")
                self.selected_target_label.setStyleSheet("font-weight: bold; color: #7c3aed; padding: 2px 4px;")
            else:
                self.selected_target_label.setText("직접 입력 대상: (카테고리 명칭과 URL을 입력하세요)")
                self.selected_target_label.setStyleSheet("font-weight: bold; color: #64748b; padding: 2px 4px;")

    def _browse_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "수집 결과 저장 폴더 선택", self.edit_output_dir.text()
        )
        if path:
            self.edit_output_dir.setText(path)
            self._last_output_dir = path

    # ── 제어 및 상태 전환 ─────────────────────────────────────────────
    def _on_start_clicked(self) -> None:
        target_name = ""
        target_url = ""

        # 현재 탭 확인
        if self.input_tabs.currentIndex() == 0:  # 트리 탭
            selected = self.category_tree.selectedItems()
            if not selected:
                self.append_log("[안내] 수집할 카테고리를 트리에서 선택하세요.")
                return
            item = selected[0]
            target_name = item.data(0, ROLE_NAME) or item.text(0)
            target_url = item.data(0, ROLE_URL) or ""
        else:  # 직접 입력 탭
            target_name = self.input_direct_name.text().strip()
            target_url = self.input_direct_url.text().strip()

        if not target_url:
            self.append_log("[오류] 유효한 카테고리 URL이 선택되거나 입력되지 않았습니다.")
            return
        if not target_name:
            target_name = "Ali_카테고리"

        cfg = AliexpressCategoryRunConfig(
            output_dir=Path(self.edit_output_dir.text().strip()),
            category_name=target_name,
            category_url=target_url,
            max_pages=self.spin_max_pages.value(),
            rotation_batch_size=self.spin_rotation_batch.value(),
        )

        # 결과 화면 비우기는 시작이 수락된 뒤 main_window 에서 한다 —
        # 대화상자에서 취소하면 이전 결과가 보존된다(쿠팡 탭과 동일).
        self.start_requested.emit(cfg)

    def _open_output_folder(self) -> None:
        folder = self.edit_output_dir.text().strip()
        if not folder:
            self.append_log("[폴더 열기 오류] 저장 폴더가 비어 있습니다.")
            return
        self._last_output_dir = folder
        self.open_output_requested.emit(folder)

    def set_state(self, state: str) -> None:
        self._state = state
        self._apply_state()

    def _apply_state(self) -> None:
        # 완료·실패는 다시 시작할 수 있는 종료 상태다(쿠팡 탭과 동일).
        # IP 차단으로 중단된 뒤에도 우회 회선을 골라 다시 시작할 수 있어야 한다.
        is_startable = self._state in ("idle", "finished", "failed")
        is_running = self._state == "running"
        is_paused = self._state == "paused"

        self.btn_start.setEnabled(is_startable)
        self.btn_pause.setEnabled(is_running)
        self.btn_resume.setEnabled(is_paused)
        self.btn_cancel.setEnabled(is_running or is_paused)
        self.btn_open_folder.setEnabled(self._state != "cancelling")

        self.spin_max_pages.setEnabled(is_startable)
        self.spin_rotation_batch.setEnabled(is_startable)
        self.edit_output_dir.setEnabled(is_startable)
        self.category_tree.setEnabled(is_startable)
        self.input_tabs.setEnabled(is_startable)

    def set_external_busy(self, busy: bool) -> None:
        """다른 탭 작업 실행 시 조작을 안전하게 잠금 처리."""
        if busy:
            for b in (self.btn_start, self.btn_pause, self.btn_resume, self.btn_cancel):
                b.setEnabled(False)
            self.spin_max_pages.setEnabled(False)
            self.spin_rotation_batch.setEnabled(False)
            self.edit_output_dir.setEnabled(False)
            self.category_tree.setEnabled(False)
            self.input_tabs.setEnabled(False)
        else:
            self._apply_state()

    # ── 실시간 데이터 및 로그 슬롯 ─────────────────────────────────────
    def append_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.appendPlainText(f"[{stamp}] {message}")

    def set_phase(self, phase_name: str, cur_p: int, total_p: int) -> None:
        self.label_phase.setText(f"<b>{phase_name}</b> ({cur_p}/{total_p})")

    def set_progress(self, label: str, cur: int, total: int) -> None:
        pct = int(cur / max(1, total) * 100)
        self.progress_bar.setValue(pct)
        self.label_progress.setText(f"{label}: {cur:,} / {total:,} ({pct}%)")

    def add_record(self, record: dict) -> None:
        row = self.result_table.rowCount()
        self.result_table.insertRow(row)
        for col, key in enumerate(COLUMN_KEYS):
            val = record.get(key, "")
            item = QTableWidgetItem(str(val))
            if key in ("company_name", "ceo_name", "business_number") and val:
                item.setForeground(Qt.GlobalColor.darkGreen)
            self.result_table.setItem(row, col, item)

        # 최대 1000행 유지
        if self.result_table.rowCount() > 1000:
            self.result_table.removeRow(0)
        self.result_table.scrollToBottom()

    def update_stats(self, summary: AliexpressCrawlSummary) -> None:
        tot = summary.collected_items
        uniq = summary.unique_vendors
        em = summary.has_email
        bz = summary.has_business_number

        em_pct = em / max(1, tot) * 100
        bz_pct = bz / max(1, tot) * 100

        self.card_total.findChild(QLabel, "value_label").setText(f"{tot:,}건")
        self.card_vendors.findChild(QLabel, "value_label").setText(f"{uniq:,}개사")
        self.card_emails.findChild(QLabel, "value_label").setText(f"{em_pct:.1f}% ({em:,}건)")
        self.card_biznums.findChild(QLabel, "value_label").setText(f"{bz_pct:.1f}% ({bz:,}건)")

    def clear_results(self) -> None:
        self.result_table.setRowCount(0)
        self.progress_bar.setValue(0)
        self.label_phase.setText("수집 시작 중...")
        self.label_progress.setText("진행: 0 / 0건 (0%)")
        self.card_total.findChild(QLabel, "value_label").setText("0건")
        self.card_vendors.findChild(QLabel, "value_label").setText("0개사")
        self.card_emails.findChild(QLabel, "value_label").setText("0% (0건)")
        self.card_biznums.findChild(QLabel, "value_label").setText("0% (0건)")
