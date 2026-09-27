"""Coupang 수집 엔진 — Qt 비의존 코어.

수집 모드 (2026-08-24 컨셉 전환 이후 카테고리 선택이 기본):

  카테고리 모드  — 카테고리 선택 → /np/categories/{id}?page=1..N 순회 수집
                   (비로그인 SSR. 종료는 빈 페이지 연속 2회 + 부트 셸 지속
                   판정이 담당 — 과거의 "17페이지 상한"은 실측(BRIGHTDATA_
                   AKAMAI_REVIEW §8: 19페이지 유효)으로 무효 확인)
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
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from app.core.coupang import blockguard
from app.core.coupang.crawler import CoupangCrawler, _RunError, _safe_callback
from app.core.coupang.resume_store import (
    STATUS_LISTING_DONE,
    ResumeStore,
    product_from_payload,
)
from app.core.coupang.search_parser import (
    DOM_EXTRACTION_JS,
    SearchProduct,
    is_blocked,
    keyword_block_reason,
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
# PLP 최대 페이지 — 상한 자체는 종료 판정(빈 페이지 연속 2회 + 셸 지속)이
# 담당하므로 목록 끝 이후의 여분 페이지는 471B 셸로 빠르게 끝난다(§8 실측).
# 구버전 "실측 상한 ~17" 주석은 §8(냉동과일 19페이지 유효)로 폐기.
# 한도는 안전장치다. 목록 끝은 빈 페이지 연속 2회로만 확정한다. 한도에
# 닿았는데 그 확인이 안 되면(마지막에 상품이 있거나 빈 페이지가 1회뿐)
# 일부 수집으로 알린다.
PLP_MAX_PAGES_LIMIT = 200


def _is_target_closed_error(error: Exception) -> bool:
    return type(error).__name__ == "TargetClosedError"


def _unreadable_nonrocket_count(rows: list[dict]) -> int:
    """상품 카드가 왔지만 판매자 매핑에 필요한 ID를 못 읽은 건수."""
    unreadable = 0
    for row in rows:
        if not isinstance(row, dict):
            unreadable += 1
            continue
        if row.get("rocket"):
            continue
        item = parse_extracted([row])
        if not item or not item[0].vendor_item_id:
            unreadable += 1
    return unreadable


# 부트스트랩 셸 — Camoufox+프록시 내비게이션의 ~1/3 빈도로 오는 JS 부트 페이지.
# 실측(2026-09-09, BRIGHTDATA_AKAMAI_REVIEW §3·§8): 셸 본문 471B~3.4KB,
# 정상 목록 ~600KB+ — 차단이 아니므로 대기 후 같은 URL 재내비게이션으로
# 흡수하고(1~2회 회복), 끝까지 지속하면 실제 목록 끝으로 판정한다(471B 반복).
# 차단 응답(Access Denied 등)은 is_blocked 가 먼저 걸러내므로 이 크기
# 기준과 충돌하지 않는다.
PLP_SHELL_HTML_BYTES = 10_000
SHELL_RENAVIGATE_MAX = 2

# 홈 메가메뉴의 카테고리 앵커 클릭 JS — 첫 카테고리 진입을 딥링크 goto 대신
# 자연 내비게이션으로 수행하기 위한 것. href 를 path 기준 정확 일치로 비교해
# 접두사 충돌(예: 19428 vs 194282)을 방지한다.
CATEGORY_LINK_CLICK_JS = """
(cid) => {
    const wanted = '/np/categories/' + cid;
    for (const a of document.querySelectorAll('a[href*="/np/categories/"]')) {
        const href = a.getAttribute('href') || '';
        if (href.split('?')[0] === wanted) { a.click(); return true; }
    }
    return false;
}
"""

# 차단 응답 분류 힌트 — 스냅샷 진단 로그/오류 메시지용 (순서 = 판정 우선순위).
# 실측(2026-08-28 타 PC 차단 사례): 465자 짜리 짧은 응답이 소프트 블록으로
# 감지됐고, 원인 분류가 로그에 없어 IP 차단 여부 판단이 어려웠다.
BLOCKED_DIAGNOSES = (
    ("access denied", "Akamai 접근 거부 — IP 평판 차단으로 추정"),
    ("reference #", "Akamai 접근 거부 — IP 평판 차단으로 추정"),
    ("자동화된 테스트", "자동화 감지 챌린지 페이지"),
    ("보안 절차", "자동화 감지 챌린지 페이지"),
    ("captcha", "캡차 챌린지 페이지"),
    ("사용권한", "접근 권한 오류 페이지"),
)


def classify_blocked_html(html: str) -> str:
    """차단 응답 HTML 을 키워드로 분류한다 (진단 메시지용).

    판정에 실패하면 빈 문자열 — 호출자는 분류 없는 메시지를 그대로 쓴다.
    """
    low = (html or "").lower()
    for keyword, diagnosis in BLOCKED_DIAGNOSES:
        if keyword.lower() in low:
            return diagnosis
    return ""


_AKAMAI_REFERENCE_RE = re.compile(r"Reference #[\w.]+")


def extract_akamai_reference(html: str) -> str:
    """Akamai 차단 응답의 Reference # 식별자 추출 (없으면 빈 문자열).

    Reference 는 차단 건을 엣지 단위로 대조하는 진단 정보다 — 차단 상태
    기록(coupang_block_state.json)과 로그에 함께 남긴다.
    """
    m = _AKAMAI_REFERENCE_RE.search(html or "")
    return m.group(0) if m else ""


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
    # 종료 판정(빈 페이지 연속 2회 + 셸 지속)이 실제 목록 끝을 담당한다.
    # 기본값은 큰 카테고리도 끝까지 읽도록 한도에 가깝게 둔다.
    max_pages: int = PLP_MAX_PAGES_LIMIT
    # 하위 카테고리 포함 수집 — (카테고리ID, 이름) 순서 쌍 (부모 다음 순회)
    subcategories: tuple[tuple[str, str], ...] = ()
    category_cooldown_min: float = 30.0
    category_cooldown_max: float = 60.0
    # 진행 저장·재개 (WORK_ORDER_CATEGORY_RESUME) — None 이면 재개 없이 기존 동작.
    # decodo_run 이 카테고리 폴더의 ResumeStore 를 주입하며, 시도(회선) 간에
    # 같은 저장소를 공유한다. 키워드 모드에서는 무시한다.
    resume_store: ResumeStore | None = None
    # 이 PC 회선 차단의 전역 기록 폴더 — decodo_run 이 출력 루트를 주입한다.
    # 판매자 단계(회선 IP) 차단은 카테고리와 무관한 기계 전역 상태이므로, 여기에도
    # 기록·조회해 다른 카테고리가 판매자 단계 연속 실패로 차단을 알아차리는
    # 일을 막는다(검토 2026-09-18). None 이면 카테고리 폴더 기록만 본다.
    global_block_state_dir: Path | None = None
    # 로그인 세션 강제 — True 면 비로그인 세션에서 수집을 거부한다
    # (전용 계정 1회 로그인 후 사용. 로그인 세션은 Akamai 신뢰 한도가 높다).
    require_login: bool = False

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
        for cid, _name in self.subcategories:
            if not str(cid).strip().isdigit():
                raise ValueError(f"subcategory id must be numeric: {cid}")
        if not isinstance(self.require_login, bool):
            # TRY004 무시 — 설정 검증은 ValueError 관례
            raise ValueError("require_login must be a bool")  # noqa: TRY004
        if self.category_cooldown_min < 0 or self.category_cooldown_max < self.category_cooldown_min:
            raise ValueError("invalid category cooldown range")

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

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # 마지막 차단 응답의 Akamai Reference # — run() 종료 시 차단 상태에 기록
        self._last_block_reference = ""

    def _active_cooldown(self) -> tuple[Path, float]:
        """적용할 쿨다운 (기록 폴더, 잔여초).

        카테고리 폴더 기록을 먼저 보고, 없으면 전역 기록(이 PC 회선 차단 —
        다른 카테고리·트리 로드가 남긴 것)을 본다.
        """
        local = Path(self.config.output_dir)
        remaining = blockguard.cooldown_remaining_seconds(local)
        if remaining > 0:
            return local, remaining
        global_dir = getattr(self.config, "global_block_state_dir", None)
        if global_dir is None or Path(global_dir) == local:
            return local, 0.0
        global_dir = Path(global_dir)
        return global_dir, blockguard.cooldown_remaining_seconds(global_dir)

    def run(self) -> CoupangRunSummary:
        """차단 쿨다운 게이트 + 차단 상태 기록 후 부모 파이프라인 실행.

        차단 감지 후 쿨다운 미경과 재실행은 실패 확률이 높을뿐 아니라 IP 평판을
        추가로 깎는다(2026-08-30 실측 — 차단 4.8시간 뒤 재실행, 재차 차단).
        """
        gate_dir, remaining = self._active_cooldown()
        if remaining > 0:
            state = blockguard.read_block_state(gate_dir) or {}
            blocked_at = str(state.get("blocked_at", "?"))
            scope = ""
            if gate_dir != Path(self.config.output_dir):
                scope = (
                    "이 기록은 다른 카테고리·트리 로드에서 감지된 이 PC 회선 차단입니다. "
                )
            summary = CoupangRunSummary()
            summary.error = (
                f"이전 차단({blocked_at}) 후 쿨다운 중입니다 — 약 "
                f"{blockguard.format_remaining(remaining)} 후 재시도하세요. "
                f"{scope}쿨다운 중 재실행은 IP 평판을 더 나쁘게 만듭니다."
            )
            summary.termination_reason = "block_cooldown"
            self._log(f"실행 차단: {summary.error}")
            self._emit_stats(summary)
            return summary
        self._last_block_reference = ""
        self._direct_session = False
        summary = super().run()
        if summary.termination_reason == "blocked":
            proxy_listing_block = (
                bool(getattr(self.config, "proxy", None))
                and not getattr(self, "_direct_session", False)
            )
            if proxy_listing_block:
                # Decodo 플래그 IP 거부(F2) — 홈 IP 12시간 쿨다운을 걸면
                # 같은 폴더에서 회선 교체 재시도가 막힌다.
                self._log(
                    "  프록시 회선 거부 — 12시간 쿨다운을 기록하지 않습니다. "
                    "다른 회선으로 재시도할 수 있습니다."
                )
            else:
                path = blockguard.record_block(
                    self.config.output_dir,
                    reason=summary.error or "",
                    reference=self._last_block_reference,
                )
                # 회선 IP(판매자 단계) 차단임을 표시 — decodo_run 이 이 사유로는
                # Decodo 회선을 교체하지 않는다(작업지시서 §3.3: 목록 단계까지
                # 되돌리지 않는다). 브라우저 세션이 아니라 홈 IP 가 막힌 것이므로
                # 회선 교체·목록 재요청은 무의미하다.
                summary.blocked_direct = True
                self._log(
                    f"  차단 상태 기록: {path} — "
                    f"{blockguard.DEFAULT_BLOCK_COOLDOWN_HOURS:.0f}시간 쿨다운 후 재시도하세요."
                )
                global_dir = getattr(self.config, "global_block_state_dir", None)
                if global_dir and Path(global_dir) != Path(self.config.output_dir):
                    # 이 PC 회선 차단은 카테고리와 무관하게 유효 — 전역 기록도
                    # 남겨 다른 카테고리가 목록 수집을 끝까지 한 뒤 판매자 단계
                    # 연속 실패로 차단을 알아차리는 낭비를 막는다.
                    global_path = blockguard.record_block(
                        global_dir,
                        reason=summary.error or "",
                        reference=self._last_block_reference,
                    )
                    if global_path:
                        self._log(f"  전역 차단 기록: {global_path}")
        return summary

    def _phase(self, name: str) -> None:
        idx = SEARCH_PHASES.index(name) + 1
        _safe_callback(self._on_phase, "on_phase", self._on_log, name, idx, len(SEARCH_PHASES))

    # ── 파이프라인 ─────────────────────────────────────────────────────────

    def _run_pipeline(self, page, summary: CoupangRunSummary) -> None:
        config: SearchRunConfig = self.config

        # Phase 1: 웜업 (기존 방식 재사용)
        self._phase("warmup")
        self._log("Phase 1: 웜업 시작...")
        self._warmup_home(page)
        self._log("  Akamai 검증 완료.")

        products: dict[str, SearchProduct] = {}
        rocket_removed = 0
        page1_html = ""

        # 진행 저장·재개 (WORK_ORDER_CATEGORY_RESUME) — 카테고리 전용 모드에서만.
        # 저장된 상품을 먼저 복원하고, 목록 단계 완료 상태면 목록을 다시 읽지
        # 않는다(작업지시서 §3.3).
        store = getattr(config, "resume_store", None)
        store_active = (
            store is not None
            and config.category_only
            and bool(config.category_id.strip())
        )
        resuming_listing = False
        if store_active:
            for payload in store.load_products():
                restored = product_from_payload(payload)
                products.setdefault(restored.dedup_key, restored)
            if store.status == STATUS_LISTING_DONE and store.has_state():
                resuming_listing = False
                if products:
                    self._log(
                        f"[재개] 목록 단계는 이미 완료 — 저장된 상품 {len(products):,}개를 "
                        "사용합니다 (목록 재요청 없음)"
                    )
                else:
                    resuming_listing = True
                    store.reset_empty_streak()
                    self._log("[재개] 저장된 상품이 0개라 목록을 처음부터 다시 확인합니다")
            elif store.has_state():
                resuming_listing = True
                self._log(
                    f"[재개] 이어서 수집 — {store.preview_text()} | "
                    f"페이지 {store.last_item_page}을 경계로 다시 확인한 뒤 이어갑니다"
                )
                store.reset_empty_streak()

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
        # 선택 카테고리 + 하위 카테고리 큐 순회 (중복 상품은 자동 제거)
        self._phase("category_collect")
        queue: list[tuple[str, str]] = []
        if config.category_id:
            label0 = config.category_name.strip() or config.category_id
            queue.append((config.category_id.strip(), label0))
        queue.extend((str(cid).strip(), str(name)) for cid, name in config.subcategories)
        hit_page_cap = False
        # 재개: 목록 단계가 이미 완료된 실행이면 목록을 다시 읽지 않는다.
        skip_listing = store_active and store.status == STATUS_LISTING_DONE and bool(products)
        if skip_listing:
            hit_page_cap = store.listing_end_reason == "page_cap"
            if not products:
                raise _RunError(
                    "저장된 상품 목록이 없습니다 — 진행 기록이 손상되었을 수 있습니다. "
                    "'처음부터 다시 수집'을 사용하세요.",
                    reason="no_items",
                )
        elif queue:
            self._log(f"Phase 4: 카테고리 PLP 수집 — {queue[0][1]} 외 {len(queue) - 1}개 "
                      f"(카테고리당 최대 {config.max_pages}페이지)")
        for cidx, (cid, cname) in enumerate(queue if not skip_listing else ()):
            self._log(f"  [{cidx + 1}/{len(queue)}] 카테고리 '{cname}' ({cid}) 시작")
            resume_here = (
                store_active
                and cidx == 0
                and str(store.category_id) == cid
            )
            # 세션 내 첫 PLP 진입만 홈 메뉴 클릭으로 시도 (딥링크 회피).
            # 이후 카테고리/페이지는 이미 신뢰가 형성된 세션에서 이동한다.
            # 재개 시에는 1페이지가 아닌 깊은 페이지로 곧장 이동하므로 클릭
            # 진입 규칙을 적용하지 않는다.
            click_entry = (
                False
                if (resume_here and resuming_listing)
                else (self._click_entry_first_page(page, cid) if cidx == 0 else False)
            )
            empty_streak = 0
            start_page = 1
            if resume_here and resuming_listing:
                boundary = store.last_item_page
                start_page = boundary + 1
                if boundary > 0:
                    # 경계 페이지 재확인 — 새 회선에서 마지막 저장 페이지를 다시
                    # 읽어 상품 ID로 중복 제거한 뒤 미완료 페이지로 간다(§3.2).
                    self._log(
                        f"  [재개] 저장된 경계 페이지 {boundary}을 다시 확인합니다..."
                    )
                    url_b = CATEGORY_URL.format(cid=cid, page=boundary)
                    items_b, _ = self._load_listing_page(
                        page, url_b, f"{cname} p{boundary}(경계확인)", retry_empty=False,
                    )
                    kept_b = [p for p in items_b if not (config.exclude_rocket and p.rocket)]
                    fresh_b = self._merge(products, kept_b)
                    store.record_page(boundary, kept_b)
                    if fresh_b > 0:
                        store.set_listing_shifted()
                        self._log(
                            f"  [재개] 경계 페이지에서 신규 상품 {fresh_b}개 — 목록 순서가 "
                            "바뀌었습니다. 누락 여부 확인이 불가하면 일부 수집으로 표시합니다"
                        )
                    else:
                        self._log("  [재개] 경계 페이지 일치 — 이어서 수집합니다")
            for pno in range(start_page, config.max_pages + 1):
                self.control.checkpoint()
                url = CATEGORY_URL.format(cid=cid, page=pno)
                self._log(f"    [page {pno}] 로드...")
                items, _ = self._load_listing_page(
                    page, url, f"{cname} p{pno}",
                    retry_empty=False,
                    skip_goto=(pno == 1 and click_entry))
                kept = [p for p in items if not (config.exclude_rocket and p.rocket)]
                rocket_removed += len(items) - len(kept)
                fresh = self._merge(products, kept)
                self._log(f"    [page {pno}] 신규 {fresh}개 (누적 {len(products)})")
                self._progress("category_collect", pno, config.max_pages)
                # 정상으로 읽은 페이지는 한 거래로 저장 — 저장 전에는 다음 페이지로
                # 넘어가지 않는다(작업지시서 §3.2). 저장 실패 시 예외가 올라와
                # 이 페이지는 미완료로 남는다.
                if resume_here:
                    store.record_page(pno, kept)
                if not items:
                    empty_streak += 1
                    if empty_streak >= PLP_EMPTY_TOLERANCE:
                        self._log(f"    빈 페이지 {empty_streak}회 연속 — '{cname}' 종료")
                        break
                else:
                    empty_streak = 0
                self._delay_between_pages(is_last=(pno == config.max_pages))
            else:
                if empty_streak < PLP_EMPTY_TOLERANCE:
                    hit_page_cap = True
                    self._log(
                        f"    '{cname}' 최대 {config.max_pages}페이지에 도달 — "
                        "빈 페이지 연속으로 목록 끝을 확인하지 못해 "
                        "일부 수집으로 표시합니다"
                    )
            if resume_here:
                # 목록 단계 완료 기록 — 이후 판매자 단계에서 끊겨도 목록을
                # 다시 읽지 않는다(작업지시서 §3.3).
                store.mark_listing_done("page_cap" if hit_page_cap else "empty")
            if cidx < len(queue) - 1:
                cooldown = random.uniform(config.category_cooldown_min,
                                          config.category_cooldown_max)
                self._log(f"  카테고리 전환 쿨다운 {cooldown:.0f}초...")
                self.control.sleep(cooldown)
        if not queue:
            self._log("Phase 4: 카테고리 수집 — 미지정, 건너뜀")

        # 수집 완료 집계
        summary.products_seen = len(products)
        self._emit_stats(summary)
        self._log(f"수집 완료: 고유 상품 {len(products)}개 (로켓 제외 {rocket_removed}개)")
        if not products:
            if config.category_only and not hit_page_cap and rocket_removed > 0:
                summary.termination_reason = "empty_category"
                self._phase("save")
                self._save_results(summary, partial=False)
                if store_active and summary.json_path and not summary.save_error:
                    store.mark_finished()
                self._log("  읽은 상품이 모두 로켓배송 — 빈 결과로 완료")
                return
            raise _RunError("수집된 상품이 없습니다.", reason="no_items")

        viids = [p.vendor_item_id for p in products.values() if p.vendor_item_id]
        if not viids:
            raise _RunError("vendorItemId 를 확보하지 못했습니다.", reason="no_items")
        self._log(f"vendorItemId {len(viids)}개 → 스토어 매핑 준비")

        # ── 2차 전환: 프록시 사용 중이면 판매자 API 단계는 회선 IP 세션으로 ──
        # 실측(BRIGHTDATA_AKAMAI_REVIEW §9): 목록·매핑(individualInfo)은 프록시
        # IP 로 200 이지만 판매자정보(getStoreReview)는 프록시 IP 에서 403 —
        # Akamai 가 엔드포인트별로 다른 규칙을 둔다. scratchpad 2-pass 방식의
        # 앱 반영: 프록시 세션을 닫고 비프록시(회선 IP) 세션을 새로 연다.
        if getattr(config, "proxy", None):
            page = self._switch_to_direct_session(page)

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
        if hit_page_cap and not summary.termination_reason:
            summary.termination_reason = "page_limit"
        summary.termination_reason = summary.termination_reason or "success"
        partial = bool(summary.error) or summary.termination_reason == "page_limit"
        if store_active and summary.termination_reason == "success":
            # 보수적 판정(작업지시서 §3.4·§5.6) — 누락 증명이 없으면 '완료'로
            # 쓰지 않는다: ① 재개 뒤 경계 페이지에서 목록 변동이 관측됐거나
            # ② 확인되지 못한 판매자·매핑 누락 vendorItemId 가 남아 있으면
            # 일부 수집으로 표시한다.
            saved_mapping = store.load_mapping()
            mapped_vendors = {
                payload.get("vendorId")
                for payload in saved_mapping.values()
                if isinstance(payload, dict)
            } - {None}
            confirmed = store.confirmed_sellers()
            pending = [v for v in unique_vendor_ids
                       if v in mapped_vendors and v not in confirmed]
            unmapped = [v for v in viids if v not in saved_mapping]
            if pending:
                summary.termination_reason = "resume_pending"
                self._log(
                    f"  [재개] 미확인 판매자 {len(pending)}명 남음 — "
                    "'일부 수집(재개·미확인 판매자)'으로 표시합니다"
                )
            elif unmapped:
                # 개별정보 배치가 연속 한도 미만으로 실패해 매핑이 부분 완료된
                # 상태다(2026-09-16 검토). 이 판매자들은 결과에 없으므로
                # '완료'로 쓰고 진행 파일을 봉인하면 누락이 확정된다.
                summary.termination_reason = "mapping_pending"
                self._log(
                    f"  [재개] 매핑 누락 vendorItemId {len(unmapped)}건 — "
                    "'일부 수집(매핑 누락)'으로 표시하고 재개 대상에 남깁니다"
                )
            elif store.listing_shifted:
                summary.termination_reason = "resume_shifted"
                self._log(
                    "  [재개] 목록 변동 감지 — "
                    "'일부 수집(재개·목록 변동 미확인)'으로 표시합니다"
                )
        if summary.termination_reason in (
            "resume_shifted", "resume_pending", "mapping_pending",
        ):
            partial = True
        self._save_results(summary, partial=partial)
        if (store_active
                and summary.json_path
                and not summary.save_error
                and not summary.error
                and summary.termination_reason == "success"):
            store.mark_finished()

    # ── 수집 헬퍼 ──────────────────────────────────────────────────────────

    def _warmup_home(self, page) -> None:
        """홈 진입 + 차단·로그인 검사 + 자연 행동 — 세션 시작의 공통 규율.

        1차(목록) 세션과 2차(회선 IP 전환) 세션 모두 같은 웜업을 거친다.
        """
        from app.core.coupang.crawler import COUPANG_HOME
        page.goto(COUPANG_HOME, wait_until="domcontentloaded", timeout=30000)
        self.control.sleep(2 * self._wait_scale)
        self._verify_warmup_ok(page)
        self._check_login_state(page)
        self._natural_interaction(page, self.config.warmup_time)
        self.control.checkpoint()

    def _switch_to_direct_session(self, page):
        """프록시 세션(1차 목록) → 회선 IP 세션(2차 판매자 API) 전환.

        실측(BRIGHTDATA_AKAMAI_REVIEW §9): 판매자정보 API(getStoreReview)는
        프록시 IP 에서 403, 회선 IP 에서 200 — Akamai 가 엔드포인트별로 다른
        규칙을 둔다. 프록시 세션을 닫고 같은 영속 프로필로 비프록시 세션을
        새로 열어 웜업한 뒤, 새 page 를 반환한다(scratchpad 2-pass 방식).
        """
        self._direct_session = True
        self._log(
            "  [프록시] 2차 전환 — 판매자 API 단계는 회선 IP 세션으로 진행 "
            "(프록시 IP getStoreReview 403 실측)"
        )
        if getattr(self.config, "require_login", False):
            self._log(
                "  [프록시] 주의: 로그인 세션 + 프록시 조합은 계정 보안 경보 "
                "가능성이 있습니다. 비로그인 목록 수집에만 프록시를 사용하세요."
            )
        old_browser, old_cm = self._browser, self._cm
        try:
            if old_cm is not None:
                old_cm.__exit__(None, None, None)
            elif old_browser is not None:
                old_browser.close()
        except Exception as e:  # noqa: BLE001 - 이전 세션 정리는 진행을 막지 않음
            self._log(f"  이전 세션 종료 실패({type(e).__name__}: {e}) — 새 세션으로 진행")
        for direct_attempt in (1, 2):
            browser = None
            cm = None
            try:
                browser, cm = self._create_browser(with_proxy=False)
                self._browser, self._cm = browser, cm
                new_page = browser.new_page()
                self._attach_http_status_logger(new_page)
                self._log("Phase 4.5: 2차 세션 웜업 (회선 IP)...")
                self._warmup_home(new_page)
                self._log("  2차 세션 준비 완료.")
                return new_page
            except Exception as e:  # noqa: BLE001 - 외부 브라우저 기동 경계
                if not _is_target_closed_error(e) or direct_attempt >= 2:
                    raise
                self._log("  2차 브라우저가 닫혔습니다 — 2차 세션을 다시 엽니다.")
                try:
                    if cm is not None:
                        cm.__exit__(None, None, None)
                    elif browser is not None:
                        browser.close()
                except Exception:  # noqa: BLE001 - 실패한 세션 정리 best-effort
                    pass
                self._browser, self._cm = None, None
                self.control.sleep(2 * self._wait_scale)

        raise RuntimeError("2차 세션을 열지 못했습니다")

    def _check_login_state(self, page) -> None:
        """홈에서 로그인 세션 여부를 확인하고 require_login 정책을 적용한다.

        로그인 세션은 Akamai 신뢰 한도가 비로그인보다 높다(외부 실운영 도구
        OpenCLI 도 로그인 세션을 전제). require_login 이 켜져 있으면 판별
        불가(None)도 로그인 세션을 보장할 수 없으므로 안전하게 중단한다.
        """
        from app.core.coupang.login import evaluate_login_state

        state = evaluate_login_state(page)
        if state is True:
            self._log("  로그인 세션 확인 — 계정 신뢰 등급으로 수집합니다.")
            return
        require_login = bool(getattr(self.config, "require_login", False))
        if state is None:
            self._log("  로그인 상태를 확인할 수 없습니다.")
        else:
            self._log("  비로그인 세션입니다 — 익명 신뢰 등급으로 수집합니다.")
        if require_login:
            raise _RunError(
                "쿠팡 로그인 세션을 확인할 수 없습니다. '쿠팡 로그인' 버튼으로 "
                "1회 로그인한 뒤 "
                "다시 실행하세요. (로그인 세션은 차단 임계가 높지만, 전용 계정을 "
                "사용하고 하루 볼륨 상한을 지키세요)",
                reason="login_required",
            )

    def _verify_warmup_ok(self, page) -> None:
        """웜업 직후 홈 HTML 차단 검사 — 홈부터 거부면 IP 평판 차단으로 조기 중단.

        기존에는 홈 검증 없이 "Akamai 검증 완료"를 로그해, 홈 자체가 차단된
        세션인지 첫 PLP 진입만 거부된 것인지 로그로 구분이 불가능했다.
        홈 차단은 세션 시작부터 거부된 것이므로 이후 단계를 진행하지 않는다.
        """
        try:
            html = page.content()
        except Exception as e:  # noqa: BLE001 - 브라우저 경계 오류 시 검사 생략
            if _is_target_closed_error(e):
                raise
            self._log(f"  홈 콘텐츠 확인 실패({type(e).__name__}) — 차단 검사 생략")
            return
        blocked, reason = is_blocked(html)
        if not blocked:
            return
        diagnosis = self._dump_blocked_page(page, "home_warmup", html)
        hint = f" — {diagnosis}" if diagnosis else ""
        raise _RunError(
            f"홈 웜업 단계에서 차단 감지: {reason}{hint}. "
            "IP 평판 차단 가능성 — 쿨다운 후 재시도하세요.",
            reason="blocked",
        )

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

    def _dump_blocked_page(self, page, name: str, html: str) -> str:
        """차단 응답 원본을 output_dir 에 저장하고 분류 결과를 반환한다 (진단용).

        차단된 HTML 은 지금까지 버려졌는데, Akamai 챌린지 껍데기인지
        리다이렉트/오류 페이지인지 원인이 그 내용에 그대로 드러난다.
        저장 실패는 수집 흐름을 막지 않는다(best-effort). 반환값은 사람이
        읽는 분류 문자열이며, 분류에 실패하면 빈 문자열.
        """
        out_dir = Path(getattr(self.config, "output_dir", "") or ".")
        safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", name)
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = out_dir / f"blocked_{safe_name}_{ts}.html"
        diagnosis = classify_blocked_html(html)
        reference = extract_akamai_reference(html)
        if reference:
            self._last_block_reference = reference
        try:
            path.write_text(html, encoding="utf-8")
        except OSError as e:
            self._log(f"    [차단 진단] 응답 저장 실패: {e}")
            return diagnosis
        reference_note = f" | {reference}" if reference else ""
        self._log(f"    [차단 진단] 차단 응답 저장: {path} ({diagnosis}{reference_note}) "
                  f"(최종 URL: {getattr(page, 'url', '?')})")
        return diagnosis

    def _click_entry_first_page(self, page, cid: str) -> bool:
        """첫 카테고리 page 1 을 홈 내부 링크 클릭으로 진입 시도 (딥링크 회피).

        세션 시작 직후의 첫 동작이 카테고리 URL 직접 이동(goto)이면 Akamai
        점수에 불리하다(2026-08-28 타 PC 첫 PLP 즉시 차단 사례). 홈 웜업 직후
        메가메뉴 앵커를 클릭해 referrer 체인을 만든다. 어떤 이유로든 실패하면
        False 를 반환하고 호출자가 기존 goto 경로로 진행하므로 회귀가 없다.
        """
        try:
            clicked = bool(page.evaluate(CATEGORY_LINK_CLICK_JS, str(cid)))
            if not clicked:
                self._log("    [진입] 홈에 카테고리 링크 없음 — 직접 접속으로 진행")
                return False
            page.wait_for_url(f"**/np/categories/{cid}*", timeout=20000)
            self._log("    [진입] 홈 메뉴 클릭으로 PLP 진입")
            return True
        except Exception as e:  # noqa: BLE001 - 클릭 진입은 최선 노력 (goto 폴백 있음)
            if _is_target_closed_error(e):
                raise
            self._log(f"    [진입] 메뉴 클릭 실패({type(e).__name__}) — 직접 접속으로 진행")
            return False

    def _load_listing_page(self, page, url: str, name: str,
                             retry_empty: bool = True,
                             skip_goto: bool = False) -> tuple[list[SearchProduct], str]:
        """목록 페이지 로드 + DOM 추출. 백오프 30/60/90초(3회) 후 중단.

        retry_empty=False 면 빈 페이지를 재시도하지 않고 그대로 반환
        (PLP 상한 도달은 빈 페이지가 정상 신호 — rev.13 실측).
        skip_goto=True 면 첫 시도에서 goto 를 건너뛴다 — 메뉴 클릭 진입처럼
        페이지가 이미 목표 URL 에 있을 때 사용하며, 재시도 시에는 goto 로 복귀한다.
        부트스트랩 셸(상품 0건 + 작은 본문)은 재내비게이션으로 흡수하고, 지속
        시 목록 끝으로 판정한다 — PLP_SHELL_HTML_BYTES 주석의 실측 근거 참조.
        """
        last_error = ""
        for attempt in range(len(self._backoff_seconds) + 1):
            self.control.checkpoint()
            if skip_goto:
                skip_goto = False
            else:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                except Exception as e:  # noqa: BLE001 - 브라우저 경계 오류 구조화
                    if _is_target_closed_error(e):
                        raise
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
            self.control.sleep(random.uniform(2.5, 4.0) * self._wait_scale)
            self._natural_interaction(page, random.uniform(4.0, 7.0) * self._wait_scale)
            html = page.content()

            # 차단 분류 — 키워드 차단(Access Denied/사용권한 등)만 하드 스톱.
            # 작은 본문은 차단이 아니라 부트스트랩 셸일 수 있다(§3 실측) —
            # 소프트 블록 크기 규칙 대신 아래 셸 규칙으로 흡수한다.
            kw_reason = keyword_block_reason(html)
            if kw_reason:
                # 밀어붙이지 않는다 — 즉시 중단 (EXTERNAL_RESEARCH 차단 규율)
                diagnosis = self._dump_blocked_page(page, name, html)
                hint = f" — {diagnosis}" if diagnosis else ""
                raise _RunError(
                    f"쿠팡 차단 감지 ({name}): {kw_reason}{hint}. "
                    "수집을 중단하고 충분한 쿨다운 후 재시도하세요.",
                    reason="blocked",
                )

            extraction_error = ""
            extraction_exception: Exception | None = None
            try:
                rows = page.evaluate(DOM_EXTRACTION_JS)
                items = parse_extracted(rows)
                unreadable = _unreadable_nonrocket_count(rows) if self.config.category_only else 0
                if unreadable:
                    raise ValueError(f"로켓 외 상품 카드 {unreadable}개의 판매자 ID를 읽지 못했습니다")
            except Exception as e:  # 추출 경계
                if _is_target_closed_error(e):
                    raise
                extraction_exception = e
                extraction_error = f"DOM 추출 실패: {type(e).__name__}: {e}"
                last_error = extraction_error
                self._log(f"    {last_error}")
                items = []
            if items:
                return items, html

            # ── 부트스트랩 셸 흡수 (실측 §3·§8) ────────────────────────────
            # 상품 0건 + 비정상적으로 작은 본문(정상 목록 ~600KB+, 셸 471B~3.4KB)
            # 은 차단이 아니라 JS 부트 페이지다 — 대기 후 같은 URL 을
            # 재내비게이션해 흡수한다. 셸이 끝까지 지속하면 실제 목록 끝으로
            # 판정해 빈 페이지로 반환한다(빈 페이지 연속 종료 판정과 연동).
            if len(html.encode("utf-8")) < PLP_SHELL_HTML_BYTES:
                html = self._renavigate_shell(page, url, name, html)
                if len(html.encode("utf-8")) < PLP_SHELL_HTML_BYTES and not extraction_error:
                    # 셸 지속 = 목록 끝 판정 — 백오프로 시간을 낭비하지 않는다
                    return [], html
                try:
                    rows = page.evaluate(DOM_EXTRACTION_JS)
                    items = parse_extracted(rows)
                    unreadable = _unreadable_nonrocket_count(rows) if self.config.category_only else 0
                    if unreadable:
                        raise ValueError(f"로켓 외 상품 카드 {unreadable}개의 판매자 ID를 읽지 못했습니다")
                except Exception as e:  # 추출 경계
                    if _is_target_closed_error(e):
                        raise
                    extraction_exception = e
                    extraction_error = f"DOM 추출 실패: {type(e).__name__}: {e}"
                    last_error = extraction_error
                    self._log(f"    {last_error}")
                    items = []
                if items:
                    return items, html
                # 회복했지만 상품 0 — 빈 페이지로 기존 로직 진행

            # 정상 크기의 HTML 안에 상품 링크가 있는데 파싱 결과가 0건이면
            # 쿠팡 응답을 받았지만 DOM 추출이 실패한 것으로 본다. 카테고리
            # 모드(retry_empty=False)에서도 이 경우만 기존 백오프로 재확인해
            # 일시적인 렌더/추출 실패가 목록 끝으로 기록되지 않게 한다.
            suspicious_empty = bool(
                extraction_error
                or (
                    len((html or "").encode("utf-8")) >= PLP_SHELL_HTML_BYTES
                    and "/vp/products/" in (html or "")
                )
            )
            if not retry_empty and not suspicious_empty:
                return [], html

            if attempt < len(self._backoff_seconds):
                wait = self._backoff_seconds[attempt]
                retry_reason = "상품 추출 결과 의심" if suspicious_empty else "상품 0건"
                self._log(
                    f"    {retry_reason} — 백오프 {wait}초 후 재시도 ({attempt + 1}/3)..."
                )
                self.control.sleep(wait)
                continue
            if suspicious_empty:
                detail = extraction_error or (
                    "상품 링크가 포함된 목록 HTML에서 상품을 추출하지 못했습니다"
                )
                raise _RunError(
                    f"목록 페이지 상품 추출 실패 ({name}): {detail}",
                    reason="error",
                ) from extraction_exception
            # 재시도 소진: 빈 페이지 자체는 차단이 아님 (PLP 상한 도달의 정상 신호)
            return [], html

        raise _RunError(
            f"목록 페이지를 가져오지 못했습니다 ({name}). {last_error}",
            reason="no_items",
        )

    def _renavigate_shell(self, page, url: str, name: str, html: str) -> str:
        """부트스트랩 셸 감지 — 대기 후 같은 URL 재내비게이션 (실측 §3 규칙).

        재내비게이션해도 셸(작은 본문)이 지속하면 마지막 html 을 그대로
        반환하고 호출자가 '목록 끝'으로 판정한다(§8: 471B 셸 반복 = 페이지
        소진). 재내비게이션 중 차단이 감지되면 기존 규율대로 즉시 중단한다.
        """
        for shell_attempt in range(1, SHELL_RENAVIGATE_MAX + 1):
            self.control.checkpoint()
            self._log(
                f"    [{name}] 부트스트랩 셸 감지 (본문 {len(html.encode('utf-8')):,}B) — "
                f"대기 후 재내비게이션 ({shell_attempt}/{SHELL_RENAVIGATE_MAX})..."
            )
            self.control.sleep(random.uniform(2.0, 4.0) * self._wait_scale)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:  # noqa: BLE001 - 재내비게이션 실패는 빈 페이지로 흡수
                self._log(f"    [{name}] 재내비게이션 실패({type(e).__name__}: {e})")
                return html
            self.control.sleep(random.uniform(2.5, 4.0) * self._wait_scale)
            self._natural_interaction(page, random.uniform(3.0, 6.0) * self._wait_scale)
            html = page.content()
            kw_reason = keyword_block_reason(html)
            if kw_reason:
                diagnosis = self._dump_blocked_page(page, name, html)
                hint = f" — {diagnosis}" if diagnosis else ""
                raise _RunError(
                    f"쿠팡 차단 감지 ({name}): {kw_reason}{hint}. "
                    "수집을 중단하고 충분한 쿨다운 후 재시도하세요.",
                    reason="blocked",
                )
            if len(html.encode("utf-8")) >= PLP_SHELL_HTML_BYTES:
                self._log(f"    [{name}] 재내비게이션으로 회복 (본문 {len(html.encode('utf-8')):,}B)")
                return html
        return html
