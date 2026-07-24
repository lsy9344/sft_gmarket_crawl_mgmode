"""설정 패널 위젯 (카테고리 선택 / 수집 수량 / 요청 간격 / 저장 경로).

WORK_ORDER §7.3 UI 구성 기준. main_window 가 이 패널을 임포트하여 배치한다.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.core import config
from app.models.records import SOURCE_BEST

# 한 행에 배치할 카테고리 체크박스 최대 개수 (약 6개마다 줄바꿈)
_COLUMNS_PER_ROW = 6


class SettingsPanel(QWidget):
    """수집 설정 입력 패널.

    - "전체 선택" 체크박스 + 카테고리별 체크박스(베스트/슈퍼딜 구분)
    - 수집 수량 / 요청 간격 / 저장 경로 입력
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # 체크박스 → CategoryDef 매핑 (선택 순서는 config.ALL_CATEGORIES 를 따른다)
        self._checkbox_by_category: dict = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        group = QGroupBox("설정")
        group_layout = QVBoxLayout(group)

        # ── 카테고리 선택 ────────────────────────────────────────────
        self._all_checkbox = QCheckBox("전체 선택")
        self._all_checkbox.setChecked(True)
        self._all_checkbox.stateChanged.connect(self._on_all_toggled)
        group_layout.addWidget(self._all_checkbox)

        best_cats = [c for c in config.ALL_CATEGORIES if c.source == SOURCE_BEST]
        superdeal_cats = [c for c in config.ALL_CATEGORIES if c.source != SOURCE_BEST]

        group_layout.addWidget(self._build_category_section("베스트", best_cats))
        group_layout.addWidget(self._build_category_section("슈퍼딜", superdeal_cats))

        group_layout.addWidget(self._hline())

        # ── 수집 수량 ────────────────────────────────────────────────
        max_row = QHBoxLayout()
        max_row.addWidget(QLabel("수집 수량"))
        self._max_items_spin = QSpinBox()
        self._max_items_spin.setRange(1, 1000)
        self._max_items_spin.setValue(config.DEFAULT_MAX_ITEMS)
        max_row.addWidget(self._max_items_spin)
        max_row.addStretch(1)
        group_layout.addLayout(max_row)

        # ── 요청 간격(초) ────────────────────────────────────────────
        delay_row = QHBoxLayout()
        delay_row.addWidget(QLabel("요청 간격(초)"))
        self._delay_spin = QDoubleSpinBox()
        self._delay_spin.setRange(0.1, 5.0)
        self._delay_spin.setSingleStep(0.1)
        self._delay_spin.setDecimals(1)
        self._delay_spin.setValue(config.DEFAULT_DELAY)
        delay_row.addWidget(self._delay_spin)
        delay_row.addStretch(1)
        group_layout.addLayout(delay_row)

        # ── 저장 경로 ────────────────────────────────────────────────
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("저장 경로"))
        self._output_edit = QLineEdit(str(config.DEFAULT_OUTPUT_DIR))
        path_row.addWidget(self._output_edit, 1)
        self._browse_button = QPushButton("찾아보기")
        self._browse_button.clicked.connect(self._on_browse)
        path_row.addWidget(self._browse_button)
        group_layout.addLayout(path_row)

        root.addWidget(group)

    # ── 내부 UI 헬퍼 ─────────────────────────────────────────────────
    def _build_category_section(self, title: str, categories: list) -> QGroupBox:
        """카테고리 체크박스들을 약 6개 단위로 감싸는 그리드 섹션 생성."""
        box = QGroupBox(title)
        grid = QGridLayout(box)
        for index, category in enumerate(categories):
            checkbox = QCheckBox(category.name)
            checkbox.setChecked(True)
            checkbox.stateChanged.connect(self._on_category_toggled)
            self._checkbox_by_category[category] = checkbox
            row, col = divmod(index, _COLUMNS_PER_ROW)
            grid.addWidget(checkbox, row, col)
        return box

    @staticmethod
    def _hline() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    # ── 시그널 핸들러 ────────────────────────────────────────────────
    def _on_all_toggled(self, state: int) -> None:
        checked = state == Qt.CheckState.Checked.value
        for checkbox in self._checkbox_by_category.values():
            checkbox.blockSignals(True)
            checkbox.setChecked(checked)
            checkbox.blockSignals(False)

    def _on_category_toggled(self, _state: int) -> None:
        """개별 체크박스 변경 시 '전체 선택' 상태를 동기화."""
        all_checked = all(cb.isChecked() for cb in self._checkbox_by_category.values())
        self._all_checkbox.blockSignals(True)
        self._all_checkbox.setChecked(all_checked)
        self._all_checkbox.blockSignals(False)

    def _on_browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "저장 경로 선택", self._output_edit.text()
        )
        if directory:
            self._output_edit.setText(directory)

    # ── 공개 API ─────────────────────────────────────────────────────
    def selected_categories(self) -> list:
        """체크된 카테고리 목록. 순서는 config.ALL_CATEGORIES 를 따른다."""
        return [
            category
            for category in config.ALL_CATEGORIES
            if self._checkbox_by_category[category].isChecked()
        ]

    def max_items(self) -> int:
        return self._max_items_spin.value()

    def delay(self) -> float:
        return self._delay_spin.value()

    def output_dir(self) -> str:
        return self._output_edit.text()

    def set_controls_enabled(self, enabled: bool) -> None:
        """수집 실행 중 모든 입력 위젯 활성/비활성 전환."""
        self._all_checkbox.setEnabled(enabled)
        for checkbox in self._checkbox_by_category.values():
            checkbox.setEnabled(enabled)
        self._max_items_spin.setEnabled(enabled)
        self._delay_spin.setEnabled(enabled)
        self._output_edit.setEnabled(enabled)
        self._browse_button.setEnabled(enabled)
