"""Coupang 수집 엔진 — Qt 비의존 코어.

수집 모드 (2026-08-24 컨셉 전환 이후 카테고리 선택이 기본):

  카테고리 모드  — 카테고리 선택 → /np/categories/{id}?page=1..N 순회 수집
                   (비로그인 SSR, 실측 상한 ~17페이지, rev.11~24)
  키워드 모드    — 층1 SRP 정렬 4종 + 층2 가격 밴드 (기존 3층 구조 잔존,
                   CLI 등에서만 사용. 페이지네이션은 비로그인 미제공 — rev.24)

기존 OMP 엔진(CoupangCrawler) 재사용 요소:
- Camoufox 브라우저 생성/정리, 웜업, 자연스러운 행동 시뮬레이션
- 스토어 API 파이프라인(individualInfo → getStoreReview) 과 저장 단계

운영 규율(EXTERNAL_RESEARCH): 페이지 간 15~20초 랜덤 딜레이,
백오프 30/60/90초(3회), 차단 감지 시 즉시 중단.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from urllib.parse import quote

from app.core.base import CancelledError  # noqa: F401 - 호출 측 예외 계약 재노출
from app.core.coupang.crawler import CoupangCrawler, _RunError, _safe_callback
from app.core.coupang.search_parser import (
    DOM_EXTRACTION_JS,
    SearchProduct,
    is_blocked,
    parse_extracted,
    parse_price_bands,
)
from app.models.coupang_records import CoupangRunConfig, CoupangRunSummary

SEARCH_PHASES = (
    "warmup",
    "search_collect",
    "price_bands",
    "category_collect",
    "vendor_mapping",
    "business_info",
    "save",
)

SEARCH_URL = "https://www.coupang.com/np/search?q={q}"
CATEGORY_URL = "https://www.coupang.com/np/categories/{cid}?page={page}"
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
# PLP 빈 페이지 연속 허용 한도 (일시 렌더 실패 대비, 상한 도달 시 종료용)
PLP_EMPTY_TOLERANCE = 2
# PLP 최대 페이지 (실측 상한 ~17, rev.13)
PLP_MAX_PAGES_LIMIT = 50


@dataclass
class SearchRunConfig(CoupangRunConfig):
    """수집 실행 설정 — 카테고리 전용(기본) 또는 키워드(층1/2 포함) 모드."""

    keyword: str = ""
    category_name: str = ""
    sorters: tuple[str, ...] = DEFAULT_SORTERS
    exclude_rocket: bool = True
    page_delay_min: float = 15.0
    page_delay_max: float = 20.0
    include_price_bands: bool = True
    category_id: str = ""
    max_pages: int = 17

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.keyword.strip() and not self.category_id.strip():
            raise ValueError("keyword or category_id is required")
        if not self.sorters:
            raise ValueError("sorters must not be empty")
        unknown = [s for s in self.sorters if s not in SORTER_NAMES]
        if unknown:
            raise ValueError(f"unknown sorters: {unknown}")
        if self.page_delay_min < 0 or self.page_delay_max < self.page_delay_min:
            raise ValueError("invalid page delay range")
        if self.category_id and not self.category_id.strip().isdigit():
            raise ValueError("category_id must be numeric")
        if not 1 <= self.max_pages <= PLP_MAX_PAGES_LIMIT:
            raise ValueError(f"max_pages must be 1..{PLP_MAX_PAGES_LIMIT}")

    @property
    def category_only(self) -> bool:
        """키워드 없이 카테고리 PLP 만 수집하는 모드."""
        return not self.keyword.strip()


class SearchCrawler(CoupangCrawler):
    """수집 엔진.

    카테고리 모드: Phase 1 웜업 → Phase 4 카테고리 PLP 순회 → Phase 5~7
    키워드 모드:   Phase 1~7 전체 (층1 SRP 정렬 + 층2 가격 밴드 + 층3 PLP)
    """

    # 테스트에서 오버라이드 가능
    _backoff_seconds = BACKOFF_SECONDS

    def _phase(self, name: str) -> None:
        idx = SEARCH_PHASES.index(name) + 1
        _safe_callback(self._on_phase, "on_phase", self._on_log, name, idx, len(SEARCH_PHASES))

    # ── 파이프라인 ─────────────────────────────────────────────────────────

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

        products: dict[str, SearchProduct] = {}
        rocket_removed = 0
        page1_html = ""

        # Phase 2: 층1 — SRP 정렬별 수집 (키워드 모드만)
        self._phase("search_collect")
        if config.category_only:
            self._log("Phase 2: SRP 수집 — 카테고리 전용 모드, 건너뜀")
        else:
            self._log(f"Phase 2: SRP 수집 — '{config.keyword}' "
                      f"({len(config.sorters)}개 정렬, 로켓 {'제외' if config.exclude_rocket else '포함'})")
            for idx, sorter in enumerate(config.sorters):
                self.control.checkpoint()
                url = SEARCH_URL.format(q=quote(config.keyword))
                if sorter != "scoreDesc":
                    url += f"&sorter={sorter}"
                name = SORTER_NAMES[sorter]
                self._log(f"  [{idx + 1}/{len(config.sorters)}] {name} 로드...")

                items, html = self._load_listing_page(page, url, name)
                if idx == 0:
                    page1_html = html
                kept = [p for p in items if not (config.exclude_rocket and p.rocket)]
                rocket_removed += len(items) - len(kept)
                fresh = self._merge(products, kept)
                self._log(f"    {name}: {len(items)}개 중 로켓 {len(items) - len(kept)}개 제외, "
                          f"신규 {fresh}개 (누적 {len(products)})")
                self._progress("search_collect", idx + 1, len(config.sorters))
                self._delay_between_pages(is_last=(idx == len(config.sorters) - 1))

        # Phase 3: 층2 — 가격 밴드 수집 (키워드 모드만)
        self._phase("price_bands")
        if config.category_only:
            self._log("Phase 3: 가격 밴드 수집 — 카테고리 전용 모드, 건너뜀")
        elif config.include_price_bands:
            bands = parse_price_bands(page1_html)
            self._log(f"Phase 3: 가격 밴드 수집 — {len(bands)}개 밴드 감지")
            for idx, (lo, hi) in enumerate(bands):
                self.control.checkpoint()
                url = (SEARCH_URL.format(q=quote(config.keyword))
                       + f"&isPriceRange=true&minPrice={lo}&maxPrice={hi}")
                label = f"가격 {lo}~{hi}원"
                self._log(f"  [{idx + 1}/{len(bands)}] {label} 로드...")
                items, _ = self._load_listing_page(page, url, label)
                kept = [p for p in items if not (config.exclude_rocket and p.rocket)]
                rocket_removed += len(items) - len(kept)
                fresh = self._merge(products, kept)
                self._log(f"    {label}: 신규 {fresh}개 (누적 {len(products)})")
                self._progress("price_bands", idx + 1, len(bands))
                self._delay_between_pages(is_last=(idx == len(bands) - 1))
            if not bands:
                self._log("  가격 밴드 미감지 — 건너뜀")
        else:
            self._log("Phase 3: 가격 밴드 수집 — 비활성화, 건너뜀")

        # Phase 4: 층3 — 카테고리 PLP 순회 (카테고리 모드의 본체)
        self._phase("category_collect")
        if config.category_id:
            label = config.category_name.strip() or config.category_id
            self._log(f"Phase 4: 카테고리 '{label}' ({config.category_id}) PLP 수집 "
                      f"(최대 {config.max_pages}페이지)")
            empty_streak = 0
            for pno in range(1, config.max_pages + 1):
                self.control.checkpoint()
                url = CATEGORY_URL.format(cid=config.category_id.strip(), page=pno)
                self._log(f"  [page {pno}] 로드...")
                items, _ = self._load_listing_page(page, url, f"PLP p{pno}", retry_empty=False)
                kept = [p for p in items if not (config.exclude_rocket and p.rocket)]
                rocket_removed += len(items) - len(kept)
                fresh = self._merge(products, kept)
                self._log(f"  [page {pno}] 신규 {fresh}개 (누적 {len(products)})")
                self._progress("category_collect", pno, config.max_pages)
                if not items:
                    empty_streak += 1
                    if empty_streak >= PLP_EMPTY_TOLERANCE:
                        self._log(f"  빈 페이지 {empty_streak}회 연속 — PLP 종료")
                        break
                else:
                    empty_streak = 0
                self._delay_between_pages(is_last=(pno == config.max_pages))
        else:
            self._log("Phase 4: 카테고리 수집 — 미지정, 건너뜀")

        # 수집 완료 집계
        summary.products_seen = len(products)
        self._emit_stats(summary)
        self._log(f"수집 완료: 고유 상품 {len(products)}개 (로켓 제외 {rocket_removed}개)")
        if not products:
            raise _RunError("수집된 상품이 없습니다.", reason="no_items")

        viids = [p.vendor_item_id for p in products.values() if p.vendor_item_id]
        if not viids:
            raise _RunError("vendorItemId 를 확보하지 못했습니다.", reason="no_items")
        self._log(f"vendorItemId {len(viids)}개 → 스토어 매핑 준비")

        # Phase 5~7: 기존 스토어 API 파이프라인 재사용
        self._phase("vendor_mapping")
        self._log("Phase 5: vendor 매핑...")
        all_vendors = self._map_vendors(page, viids, summary)
        self.control.checkpoint()
        unique_vendor_ids = sorted(all_vendors.keys())
        summary.unique_vendors = len(unique_vendor_ids)
        self._emit_stats(summary)
        self._log(f"  고유 판매자: {len(unique_vendor_ids)}명")

        self._phase("business_info")
        self._log(f"Phase 6: 사업자정보 수집 ({len(unique_vendor_ids)}명)...")
        self._fetch_business_info(page, unique_vendor_ids, all_vendors, summary)
        self.control.checkpoint()

        self._phase("save")
        self._log("Phase 7: 결과 저장...")
        self._save_results(summary, partial=bool(summary.error))
        summary.termination_reason = summary.termination_reason or "success"

    # ── 수집 헬퍼 ──────────────────────────────────────────────────────────

    def _merge(self, products: dict[str, SearchProduct], items: list[SearchProduct]) -> int:
        fresh = 0
        for p in items:
            if p.dedup_key not in products:
                products[p.dedup_key] = p
                fresh += 1
        return fresh

    def _delay_between_pages(self, is_last: bool) -> None:
        config: SearchRunConfig = self.config
        if is_last:
            return
        delay = random.uniform(config.page_delay_min, config.page_delay_max)
        self._log(f"    딜레이 {delay:.0f}초...")
        self.control.sleep(delay)

    def _load_listing_page(self, page, url: str, name: str,
                             retry_empty: bool = True) -> tuple[list[SearchProduct], str]:
        """목록 페이지 로드 + DOM 추출. 백오프 30/60/90초(3회) 후 중단.

        retry_empty=False 면 빈 페이지를 재시도하지 않고 그대로 반환
        (PLP 상한 도달은 빈 페이지가 정상 신호 — rev.13 실측).
        """
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
                    f"목록 페이지 로드 실패 ({name}): {last_error}", reason="error"
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
                return items, html

            if not retry_empty:
                return [], html

            if attempt < len(self._backoff_seconds):
                wait = self._backoff_seconds[attempt]
                self._log(f"    상품 0건 — 백오프 {wait}초 후 재시도 ({attempt + 1}/3)...")
                self.control.sleep(wait)
                continue
            # 재시도 소진: 빈 페이지 자체는 차단이 아님 (PLP 상한 도달의 정상 신호)
            return [], html

        raise _RunError(
            f"목록 페이지를 가져오지 못했습니다 ({name}). {last_error}",
            reason="no_items",
        )
