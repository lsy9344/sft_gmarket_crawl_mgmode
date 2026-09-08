"""Gmarket 카테고리 트리 로드 QThread 워커: 코어 ↔ Qt signal 브리지.

트리 로드는 Cloudflare 없는 정적 페이지 GET 들(대분류 약 60장)이라 브라우저
런타임이 필요 없다 — Control(취소/일시정지)만 받는다.
"""

from __future__ import annotations

import traceback

from PyQt6.QtCore import QThread, pyqtSignal

from app.core.base import CancelledError, Control
from app.core.gmarket_categories import (
    GmarketCategoryFetchError,
    GmarketCategoryTreeCache,
    GmarketCategoryTreeFetcher,
    count_nodes,
)


class GmarketCategoryWorker(QThread):
    """Gmarket 카테고리 트리를 불러와 캐시에 저장."""

    log_message = pyqtSignal(str)
    tree_loaded = pyqtSignal(object, str, int)  # roots, fetched_at, total
    error_occurred = pyqtSignal(str)

    def __init__(self, cache: GmarketCategoryTreeCache, control: Control,
                 parent=None) -> None:
        super().__init__(parent)
        self._cache = cache
        # Control 은 caller(메인 윈도우) 소유 — 절대 새로 만들지 않는다.
        self._control = control

    def run(self) -> None:
        try:
            fetcher = GmarketCategoryTreeFetcher(
                control=self._control,
                on_log=self.log_message.emit,
            )
            roots = fetcher.fetch()
            fetched_at = self._cache.save(roots)
            total = count_nodes(roots)
            self.tree_loaded.emit(roots, fetched_at, total)
        except CancelledError:
            self.log_message.emit("[카테고리 트리] 취소됨")
        except GmarketCategoryFetchError as e:
            self.error_occurred.emit(str(e))
        except Exception as e:  # noqa: BLE001 - 워커 최상위 예외 격리
            self.log_message.emit(traceback.format_exc())
            self.error_occurred.emit(f"{type(e).__name__}: {e}")
