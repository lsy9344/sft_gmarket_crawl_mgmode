"""진행 상황 패널 위젯 (카테고리 진행 / 진행률 바 / 경과·ETA·속도 / 성공·실패).

WORK_ORDER §7.3 UI 구성 기준. QTimer 로 1초마다 경과 시간을 갱신한다.
"""

from __future__ import annotations

import time

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QGroupBox,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from app.utils.helpers import format_clock


class ProgressPanel(QWidget):
    """수집 진행 상황을 실시간으로 보여주는 패널."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # 시간/속도 계산용 상태
        self._start_time: float | None = None
        self._cat_total = 0            # 전체 카테고리 수
        self._item_total = 0           # 현재 카테고리의 아이템 총 수
        self._processed_base = 0       # 이전 카테고리들까지 누적 처리 건수
        self._current_item = 0         # 현재 카테고리에서 처리된 건수
        self._grand_total = 0          # 호출자가 알려준 실제 전체 처리 대상 수(있으면 우선 사용)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._on_tick)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        group = QGroupBox("진행 상황")
        group_layout = QVBoxLayout(group)

        self._category_label = QLabel("카테고리: -")
        group_layout.addWidget(self._category_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        group_layout.addWidget(self._progress_bar)

        self._time_label = QLabel("경과: 00:00  |  예상 남은 시간: --:--  |  속도: -")
        group_layout.addWidget(self._time_label)

        self._counts_label = QLabel("성공: 0  |  실패: 0")
        group_layout.addWidget(self._counts_label)

        root.addWidget(group)

    # ── 공개 API ─────────────────────────────────────────────────────
    def set_category(self, name: str, current: int, total: int) -> None:
        """현재 진행 중인 카테고리 정보 갱신 (current/total 은 카테고리 순번)."""
        # 새 카테고리로 넘어가면 직전 카테고리 처리량을 누적에 반영
        self._processed_base += self._current_item
        self._current_item = 0
        self._cat_total = total
        self._category_label.setText(f"카테고리: {name} ({current}/{total})")

    def set_item_progress(self, current: int, total: int) -> None:
        """현재 카테고리 내 아이템 진행률 갱신 (total==0 방어)."""
        self._current_item = current
        self._item_total = total
        if total <= 0:
            self._progress_bar.setRange(0, 100)
            self._progress_bar.setValue(0)
            self._progress_bar.setFormat("0/0 (0%)")
        else:
            self._progress_bar.setRange(0, total)
            self._progress_bar.setValue(current)
            pct = int(current * 100 / total)
            self._progress_bar.setFormat(f"{current}/{total} ({pct}%)")
        self._update_time_label()

    def set_counts(self, success: int, fail: int) -> None:
        self._counts_label.setText(f"성공: {success:,}  |  실패: {fail:,}")

    def start(self, grand_total: int = 0) -> None:
        """카운터/진행률 초기화 후 시작 시각 기록 및 타이머 시작.

        grand_total 을 전달하면(예: Pre-scan 신규 건수 합계) ETA 계산이 카테고리
        수 × 마지막 카테고리 크기라는 부정확한 추정 대신 실제 총 대상 수를 쓴다.
        """
        self.reset()
        self._grand_total = max(0, grand_total)
        self._start_time = time.monotonic()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def reset(self) -> None:
        """모든 상태/표시를 초기값으로 되돌린다."""
        self._timer.stop()
        self._start_time = None
        self._cat_total = 0
        self._item_total = 0
        self._processed_base = 0
        self._current_item = 0
        self._grand_total = 0
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setFormat("")
        self._category_label.setText("카테고리: -")
        self._time_label.setText("경과: 00:00  |  예상 남은 시간: --:--  |  속도: -")
        self._counts_label.setText("성공: 0  |  실패: 0")

    # ── 내부 계산 ────────────────────────────────────────────────────
    def _on_tick(self) -> None:
        self._update_time_label()

    def _elapsed(self) -> float:
        if self._start_time is None:
            return 0.0
        return max(0.0, time.monotonic() - self._start_time)

    def _update_time_label(self) -> None:
        elapsed = self._elapsed()
        processed = self._processed_base + self._current_item

        # ETA / 속도는 best-effort. grand_total 은 카테고리 수 × 카테고리당 아이템 수로 추정.
        eta_text = "--:--"
        speed_text = "-"

        if elapsed > 0 and processed > 0:
            per_item = elapsed / processed
            speed_text = f"{processed / elapsed:.1f}건/초"

            # 실제 전체 대상 수(grand_total)를 우선 사용, 없으면 근사치로 대체.
            grand_total = self._grand_total or (self._cat_total * self._item_total)
            remaining = grand_total - processed
            if grand_total > 0 and remaining > 0:
                eta_text = format_clock(per_item * remaining)
            elif grand_total > 0:
                eta_text = "00:00"

        self._time_label.setText(
            f"경과: {format_clock(elapsed)}  |  "
            f"예상 남은 시간: {eta_text}  |  속도: {speed_text}"
        )
