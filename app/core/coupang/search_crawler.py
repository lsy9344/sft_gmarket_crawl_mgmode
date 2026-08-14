"""Coupang 키워드 검색 수집 엔진 — Qt 비의존 코어.

기존 OMP 엔진(CoupangCrawler)의 검증된 구성요소를 재사용한다:
- Camoufox 브라우저 생성/정리, 웜업, 자연스러운 행동 시뮬레이션
- 스토어 API 파이프라인(individualInfo → getStoreReview) 과 저장 단계

차이점(검색 전용):
- 수집 소스가 /np/omp 피드가 아니라 /np/search 결과 페이지
- 페이지네이션이 없으므로(실측) 정렬(sorter) 순회로 상품 셋 확장
- 상품 목록은 DOM 카드에서 추출, 로켓 상품은 설정에 따라 제외

실측 근거: docs/coupang/SEARCH_POC_FINDINGS.md rev.3
외부 기준점: docs/coupang/EXTERNAL_RESEARCH.md (딜레이/백오프/세션 규율)
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from urllib.parse import quote

from app.core.base import CancelledError  # noqa: F401 - 호출 측 예외 계약 재노출
from app.core.coupang.crawler import CoupangCrawler, _RunError
from app.core.coupang.search_parser import (
    DOM_EXTRACTION_JS,
    SearchProduct,
    is_blocked,
    parse_extracted,
)
from app.core.coupang.crawler import SHOP_SESSION_URL  # noqa: F401 - 문서화용 재사용 상수
from app.models.coupang_records import CoupangRunConfig, CoupangRunSummary

SEARCH_PHASES = ("warmup", "search_collect", "vendor_mapping", "business_info", "save")

SEARCH_URL = "https://www.coupang.com/np/search?q={q}"
DEFAULT_SORTERS = ("saleCountDesc", "salePriceAsc", "salePriceDesc", "latestAsc")
SORTER_NAMES = {
    "scoreDesc": "쿠팡 랭킹순",
    "saleCountDesc": "판매량순",
    "salePriceAsc": "낮은가격순",
    "salePriceDesc": "높은가격순",
    "latestAsc": "최신순",
}
# 실패 백오프 — EXTERNAL_RESEARCH §4 확정 파라미터 (30/60/90, 3회 후 중단)
BACKOFF_SECONDS = (30, 60, 90)



@dataclass
class SearchRunConfig(CoupangRunConfig):
    """키워드 검색 수집 실행 설정."""

    keyword: str = ""
    sorters: tuple[str, ...] = DEFAULT_SORTERS
    exclude_rocket: bool = True
    page_delay_min: float = 15.0
    page_delay_max: float = 20.0

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.keyword.strip():
            raise ValueError("keyword must not be empty")
        if not self.sorters:
            raise ValueError("sorters must not be empty")
        unknown = [s for s in self.sorters if s not in SORTER_NAMES]
        if unknown:
            raise ValueError(f"unknown sorters: {unknown}")
        if self.page_delay_min < 0 or self.page_delay_max < self.page_delay_min:
            raise ValueError("invalid page delay range")


class SearchCrawler(CoupangCrawler):
    """키워드 검색 수집 엔진.

    Phase 1  홈 웜업 (기존 방식)
    Phase 2  정렬별 검색 페이지 수집 — DOM 카드 추출 + 로켓 제외
    Phase 3  vendor 매핑 (individualInfo API, 기존 Phase 4 재사용)
    Phase 4  사업자정보 수집 (getStoreReview API, 기존 Phase 5 재사용)
    Phase 5  저장 (기존 CoupangRecord 스키마)
    """

    # 테스트에서 오버라이드 가능
    _backoff_seconds = BACKOFF_SECONDS

    def _phase(self, name: str) -> None:
        idx = SEARCH_PHASES.index(name) + 1
        from app.core.coupang.crawler import _safe_callback
        _safe_callback(self._on_phase, "on_phase", self._on_log, name, idx, len(SEARCH_PHASES))

    def _run_pipeline(self, page, summary: CoupangRunSummary) -> None:
        config: SearchRunConfig = self.config

        # Phase 1: 웜업 (기존 방식 재사용)
        self._phase("warmup")
        self._log("Phase 1: 웜업 시작...")
        from app.core.coupang.crawler import COUPANG_HOME
        page.goto(COUPANG_HOME, wait_until="domcontentloaded", timeout=30000)
        self.control.sleep(2 * self._wait_scale)
        self._natural_interaction(page, config.warmup_time)
        self.control.checkpoint()
        self._log("  Akamai 검증 완료.")

        # Phase 2: 정렬별 검색 수집
        self._phase("search_collect")
        self._log(f"Phase 2: 검색 수집 — '{config.keyword}' "
                  f"({len(config.sorters)}개 정렬, 로켓 {'제외' if config.exclude_rocket else '포함'})")
        products: dict[str, SearchProduct] = {}
        rocket_removed = 0

        for idx, sorter in enumerate(config.sorters):
            self.control.checkpoint()
            url = SEARCH_URL.format(q=quote(config.keyword))
            if sorter != "scoreDesc":
                url += f"&sorter={sorter}"
            name = SORTER_NAMES[sorter]
            self._log(f"  [{idx + 1}/{len(config.sorters)}] {name} 로드...")

            items = self._load_search_page(page, url, sorter, name)
            kept = [p for p in items if not (config.exclude_rocket and p.rocket)]
            rocket_removed += len(items) - len(kept)
            fresh = 0
            for p in kept:
                if p.dedup_key not in products:
                    products[p.dedup_key] = p
                    fresh += 1
            self._log(f"    {name}: {len(items)}개 중 로켓 {len(items) - len(kept)}개 제외, "
                      f"신규 {fresh}개 (누적 {len(products)})")
            self._progress("search_collect", idx + 1, len(config.sorters))

            if idx < len(config.sorters) - 1:
                delay = random.uniform(config.page_delay_min, config.page_delay_max)
                self._log(f"    딜레이 {delay:.0f}초...")
                self.control.sleep(delay)

        summary.products_seen = len(products)
        self._emit_stats(summary)
        self._log(f"  수집 완료: 고유 상품 {len(products)}개 "
                  f"(로켓 제외 {rocket_removed}개)")
        if not products:
            raise _RunError("수집된 상품이 없습니다.", reason="no_items")

        viids = [p.vendor_item_id for p in products.values() if p.vendor_item_id]
        if not viids:
            raise _RunError("vendorItemId 를 확보하지 못했습니다.", reason="no_items")
        self._log(f"  vendorItemId {len(viids)}개 → 스토어 매핑 준비")

        # Phase 3~5: 기존 스토어 API 파이프라인 재사용
        self._phase("vendor_mapping")
        self._log("Phase 3: vendor 매핑...")
        all_vendors = self._map_vendors(page, viids, summary)
        self.control.checkpoint()
        unique_vendor_ids = sorted(all_vendors.keys())
        summary.unique_vendors = len(unique_vendor_ids)
        self._emit_stats(summary)
        self._log(f"  고유 판매자: {len(unique_vendor_ids)}명")

        self._phase("business_info")
        self._log(f"Phase 4: 사업자정보 수집 ({len(unique_vendor_ids)}명)...")
        self._fetch_business_info(page, unique_vendor_ids, all_vendors, summary)
        self.control.checkpoint()

        self._phase("save")
        self._log("Phase 5: 결과 저장...")
        self._save_results(summary, partial=bool(summary.error))

    def _load_search_page(self, page, url: str, sorter: str, name: str) -> list[SearchProduct]:
        """검색 페이지 로드 + DOM 추출. 실패 시 30/60/90초 백오프(3회) 후 중단."""
        last_error = ""
        for attempt in range(len(self._backoff_seconds) + 1):
            self.control.checkpoint()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                self.control.sleep(random.uniform(2.5, 4.0) * self._wait_scale)
                self._natural_interaction(page, random.uniform(4.0, 7.0) * self._wait_scale)
                html = page.content()
            except Exception as e:  # noqa: BLE001 - 브라우저 경계 오류 구조화
                last_error = f"{type(e).__name__}: {e}"
                self._log(f"    [{name}] 로드 실패: {last_error}")
                if attempt < len(self._backoff_seconds):
                    wait = self._backoff_seconds[attempt]
                    self._log(f"    백오프 {wait}초 후 재시도 ({attempt + 1}/3)...")
                    self.control.sleep(wait)
                    continue
                raise _RunError(
                    f"검색 페이지 로드 실패 ({name}): {last_error}", reason="error"
                ) from e

            blocked, reason = is_blocked(html)
            if blocked:
                # 밀어붙이지 않는다 — 즉시 중단 (EXTERNAL_RESEARCH 차단 규율)
                raise _RunError(
                    f"쿠팡 차단 감지 ({name}): {reason}. "
                    "수집을 중단하고 충분한 쿨다운 후 재시도하세요.",
                    reason="blocked",
                )

            try:
                rows = page.evaluate(DOM_EXTRACTION_JS)
            except Exception as e:  # noqa: BLE001 - evaluate 경계
                rows = []
                last_error = f"DOM 추출 실패: {type(e).__name__}: {e}"
                self._log(f"    {last_error}")

            items = parse_extracted(rows)
            if items:
                return items

            if attempt < len(self._backoff_seconds):
                wait = self._backoff_seconds[attempt]
                self._log(f"    상품 0건 — 백오프 {wait}초 후 재시도 ({attempt + 1}/3)...")
                self.control.sleep(wait)
                continue

        raise _RunError(
            f"검색 결과를 가져오지 못했습니다 ({name}). {last_error}",
            reason="no_items",
        )
