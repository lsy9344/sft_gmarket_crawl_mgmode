"""Coupang 카테고리 트리 로드 QThread 워커: categories 코어 ↔ Qt signal 브리지."""

from __future__ import annotations

import traceback

from PyQt6.QtCore import QThread, pyqtSignal

from app.core.base import Control
from app.core.coupang.categories import (
    CategoryFetchError,
    CategoryTreeCache,
    CategoryTreeFetcher,
)


class CategoryWorker(QThread):
    """쿠팡에서 카테고리 트리를 불러와 캐시에 저장 (전용 1회 세션)."""

    log_message = pyqtSignal(str)
    tree_loaded = pyqtSignal(object, str, int)  # groups, fetched_at, total
    error_occurred = pyqtSignal(str)

    def __init__(self, cache: CategoryTreeCache, control: Control,
                 browser_factory=None, parent=None) -> None:
        super().__init__(parent)
        self._cache = cache
        self._control = control
        self._browser_factory = browser_factory

    def run(self) -> None:
        try:
            fetcher = CategoryTreeFetcher(
                control=self._control,
                browser_factory=self._browser_factory,
                on_log=self.log_message.emit,
            )
            groups = fetcher.fetch()
            fetched_at = self._cache.save(groups)
            total = sum(n.count() for _, roots in groups for n in roots)
            self.tree_loaded.emit(groups, fetched_at, total)
        except CategoryFetchError as e:
            self.error_occurred.emit(str(e))
        except Exception as e:  # noqa: BLE001 - 워커 최상위 예외 격리
            self.log_message.emit(traceback.format_exc())
            self.error_occurred.emit(f"{type(e).__name__}: {e}")
