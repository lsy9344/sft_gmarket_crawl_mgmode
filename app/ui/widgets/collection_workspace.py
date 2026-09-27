"""수집 탭 공통 배치: 위에는 설정과 로그, 아래에는 넓은 결과 표."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QSplitter, QWidget


def collection_workspace(controls: QWidget, log: QWidget, results: QWidget) -> QSplitter:
    """설정/로그를 같은 너비로 두고 결과 표에는 전체 너비를 준다."""
    upper = QSplitter(Qt.Orientation.Horizontal)
    upper.setObjectName("collectionUpperSplitter")
    upper.setChildrenCollapsible(False)
    upper.setHandleWidth(8)
    upper.addWidget(controls)
    upper.addWidget(log)
    upper.setStretchFactor(0, 1)
    upper.setStretchFactor(1, 1)
    upper.setSizes([500, 500])

    workspace = QSplitter(Qt.Orientation.Vertical)
    workspace.setObjectName("collectionWorkspace")
    workspace.setChildrenCollapsible(False)
    workspace.setHandleWidth(8)
    workspace.addWidget(upper)
    workspace.addWidget(results)
    workspace.setStretchFactor(0, 3)
    workspace.setStretchFactor(1, 2)
    workspace.setSizes([520, 340])
    return workspace
