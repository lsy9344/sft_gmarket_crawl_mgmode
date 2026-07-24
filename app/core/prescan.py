"""Phase 0: 사전 조사(Pre-scan) 로직 (WORK_ORDER §3).

StealthySession 을 1회만 생성/종료하며 선택된 모든 카테고리의 리스팅을 순회,
goodscode 를 추출하고 collected_ids 와 대조하여 신규 건수를 산출한다.
추출된 codes 는 PrescanResult 에 담겨 Phase 1 에서 재사용된다(중복 fetch 방지).
"""

from __future__ import annotations

import time
from collections.abc import Callable

from app.core import config
from app.core.base import Control
from app.core.storage import Storage
from app.models.records import (
    STATUS_BLOCKED,
    STATUS_COLLECTABLE,
    STATUS_COMPLETED,
    STATUS_EMPTY,
    PrescanResult,
)
from app.utils.helpers import contains_bot_challenge, extract_goodscodes

# 콜백 타입
LogFn = Callable[[str], None]
ProgressFn = Callable[[str, int, int], None]  # (category_name, current, total)


def _noop_log(_msg: str) -> None:  # pragma: no cover
    pass


def _noop_progress(_name: str, _cur: int, _total: int) -> None:  # pragma: no cover
    pass


class Prescanner:
    """Phase 0 사전 조사 실행기."""

    def __init__(
        self,
        storage: Storage,
        control: Control | None = None,
        on_log: LogFn = _noop_log,
        on_progress: ProgressFn = _noop_progress,
    ) -> None:
        self.storage = storage
        self.control = control or Control()
        self.on_log = on_log
        self.on_progress = on_progress

    def run(self, categories: list[config.CategoryDef]) -> list[PrescanResult]:
        """선택 카테고리 전체를 조사하여 PrescanResult 목록 반환.

        취소 시 CancelledError 를 전파한다(세션은 with 블록에서 안전 종료).
        """
        collected = self.storage.load_collected_ids()
        results: list[PrescanResult] = []
        total = len(categories)

        if total == 0:
            return results

        # StealthySession 은 무거운 의존성(Chromium)이라 지연 import
        from scrapling.fetchers import StealthySession

        self.on_log("[Pre-scan] StealthySession 시작...")
        with StealthySession(headless=True) as session:
            # Cloudflare 쿠키 획득 warmup (§3.2 순서 2)
            self.control.checkpoint()
            self.on_log("[Pre-scan] gmarket.co.kr warmup (Cloudflare 쿠키 획득)...")
            try:
                session.fetch(config.WARMUP_URL, timeout=config.WARMUP_TIMEOUT_MS)
            except Exception as e:  # noqa: BLE001 - warmup 실패해도 개별 fetch 시도
                self.on_log(f"[Pre-scan] warmup 경고: {e}")
            time.sleep(config.WARMUP_WAIT)

            for idx, cat in enumerate(categories, start=1):
                self.control.checkpoint()
                self.on_progress(cat.name, idx, total)
                self.on_log(f"[Pre-scan] ({idx}/{total}) {cat.name} 리스팅 조사 중...")

                codes, blocked = self._fetch_codes(session, cat)
                result = self._build_result(cat, codes, collected, blocked)
                results.append(result)

                self.on_log(
                    f"[Pre-scan] {cat.name}: 전체 {result.total_codes}건 / "
                    f"신규 {result.new_codes}건 / 이미수집 {result.already_collected}건 "
                    f"[{result.status_label}]"
                )

        self.on_log("[Pre-scan] 세션 종료. 조사 완료.")
        return results

    # ── 내부 ───────────────────────────────────────────────────────
    def _fetch_codes(self, session, cat: config.CategoryDef) -> tuple[list[str], bool]:
        """단일 카테고리 리스팅 fetch → (goodscode 목록, blocked 여부).

        blocked=True 는 재시도를 모두 소진하도록 200 OK 로 정상 파싱하지 못했음을
        의미한다(네트워크 오류/403/봇 감지/예상외 상태코드). 이 경우 total_codes==0
        이어도 '상품 없음'이 아니라 '차단/오류'로 표시해야 한다(§6).
        200 OK 로 받았지만 goodscode 가 실제로 0개인 경우만 blocked=False.
        """
        for attempt in range(config.LISTING_MAX_RETRIES + 1):
            self.control.checkpoint()
            kwargs: dict = {"timeout": config.SUPERDEAL_TIMEOUT_MS}
            if cat.is_best:
                kwargs["wait_selector"] = config.BEST_WAIT_SELECTOR
                kwargs["timeout"] = config.BEST_TIMEOUT_MS

            try:
                r = session.fetch(cat.url, **kwargs)
            except Exception as e:  # noqa: BLE001 - 재시도 루프에서 다양한 fetch 예외를 포괄 처리
                self.on_log(f"[Pre-scan] {cat.name} fetch 오류: {e}")
                if attempt < config.LISTING_MAX_RETRIES:
                    self._wait_blocked(cat.name)
                    continue
                return [], True

            status = getattr(r, "status", None)
            html = getattr(r, "html_content", "") or ""

            # Cloudflare 차단(403) 또는 봇 감지 → 대기 후 재시도 (§6)
            if status == 403 or contains_bot_challenge(html):
                self.on_log(f"[Pre-scan] {cat.name} 차단 감지, {config.CLOUDFLARE_WAIT}초 대기...")
                if attempt < config.LISTING_MAX_RETRIES:
                    self._wait_blocked(cat.name)
                    continue
                return [], True

            if status == 200:
                return extract_goodscodes(html), False

            # 기타 상태코드
            self.on_log(f"[Pre-scan] {cat.name} 예상외 상태코드: {status}")
            if attempt < config.LISTING_MAX_RETRIES:
                self._wait_blocked(cat.name)
                continue
            return [], True

        return [], True

    def _wait_blocked(self, name: str) -> None:
        """차단 대기: 취소 반응성을 위해 1초 단위로 분할 대기."""
        for _ in range(config.CLOUDFLARE_WAIT):
            self.control.checkpoint()
            time.sleep(1)

    @staticmethod
    def _build_result(
        cat: config.CategoryDef, codes: list[str], collected: set[str], blocked: bool = False
    ) -> PrescanResult:
        total = len(codes)
        new_codes = [c for c in codes if c not in collected]
        new_count = len(new_codes)
        already = total - new_count

        if total == 0:
            status = STATUS_BLOCKED if blocked else STATUS_EMPTY
        elif new_count == 0:
            status = STATUS_COMPLETED
        else:
            status = STATUS_COLLECTABLE

        return PrescanResult(
            category_name=cat.name,
            source=cat.source,
            total_codes=total,
            new_codes=new_count,
            already_collected=already,
            codes=codes,
            status=status,
        )
