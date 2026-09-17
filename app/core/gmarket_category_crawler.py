"""Gmarket 전체 카테고리 탭 — Phase A 리스팅(goodscode 수집) 엔진 (Qt 비의존).

Phase A 는 Bright Data Web Unlocker 로 `/n/list` 리스팅을 fetch 한다
(2026-09-08 실측, docs/gmarket/ACCESS_ROUTES_RESEARCH_20260908.md §6):
- 로컬 IP 평판(사무실/데이터센터)과 무관하게 Cloudflare Managed Challenge 를
  통과하며, `country:"kr"` 출발로 국내 한글 카탈로그를 받는다. 브라우저 불필요.
- 리스팅 URL: `https://www.gmarket.co.kr/n/list?category={code}` —
  `categoryCode=` 는 홈 리다이렉트라 반드시 레거시 `category=` 를 쓴다.
- 페이지네이션: `&k=0&p={n}&keep-ssid=y` (`&page=N` 은 무시된다 — 연속
  동일 페이지 실측). 상품이 없으면 200 + goodscode 0 이므로 기존처럼 연속
  빈 페이지 허용치로 종료한다.
- 주의(실측): 대분류(L-code) 페이지는 레거시 CategoryLarge 변형(광고 링크
  7개뿐인 셸)으로 응답할 수 있다 → 빈 페이지로 취급하고, 수집 대상은 상품
  목록이 보장되는 중/소분류(M/S)를 권장한다.
- goodscode 는 기존 `extract_goodscodes()`(정규식)로 뽑는다.

수집 결과는 PrescanResult 목록으로 만들어져, 기존 Phase B
(build_crawl_plan + SellerCrawler + Storage) 파이프라인에 그대로 흘러간다.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import requests

from app.core import brightdata, config
from app.models.records import (
    SOURCE_CATEGORY,
    STATUS_BLOCKED,
    STATUS_COLLECTABLE,
    STATUS_COMPLETED,
    STATUS_EMPTY,
    PrescanResult,
)
from app.utils.helpers import contains_bot_challenge, extract_goodscodes

LogFn = Callable[[str], None]
ProgressFn = Callable[[str, int, int], None]  # (label, current_page, total_pages)


def _noop_log(_msg: str) -> None:  # pragma: no cover
    pass


def _noop_progress(_label: str, _cur: int, _total: int) -> None:  # pragma: no cover
    pass


@dataclass(frozen=True)
class CategoryTarget:
    """수집 대상 카테고리 1개 (worker/UI 가 트리 선택에서 구성)."""

    code: str
    label: str                 # "대분류 > 중분류 > 소분류" — 파일명/source 라벨


@dataclass
class ListingOutcome:
    """카테고리 1개의 리스팅 결과 (worker 가 collected_ids 와 대조해 상태 확정)."""

    label: str
    codes: list[str] = field(default_factory=list)
    blocked: bool = False


def category_list_url(code: str, page: int | None = None) -> str:
    """카테고리 리스팅 URL (1페이지는 파라미터 없음, 이후 `&k=0&p=N&keep-ssid=y`).

    실측(2026-09-08): `&page=N` 은 무시되고 같은 페이지가 반복된다. 유효한
    페이지 이동은 `&k=0&p=N&keep-ssid=y` 뿐이다 (ACCESS_ROUTES_RESEARCH §6).
    """
    url = config.CATEGORY_LIST_URL.format(code)
    if page and page > 1:
        url += f"&k=0&p={page}&keep-ssid=y"
    return url


@dataclass(frozen=True)
class GmarketCategoryRunConfig:
    """Gmarket 카테고리 탭 실행 설정 (Coupang SearchRunConfig 에 대응)."""

    output_dir: Path
    output_prefix: str                      # 결과 파일명 prefix
    targets: tuple[CategoryTarget, ...]     # 선택(하위 포함 시 확장된) 수집 대상
    max_pages: int = config.CATEGORY_MAX_PAGES
    page_delay_min: float = config.CATEGORY_PAGE_DELAY_MIN
    page_delay_max: float = config.CATEGORY_PAGE_DELAY_MAX
    phase2_delay: float = config.DEFAULT_DELAY
    # Phase B(판매자정보) 카테고리당 최대 수집 수 캡 (build_crawl_plan 의 max_items)
    max_items: int = 5000

    def __post_init__(self) -> None:
        if not self.targets:
            raise ValueError("수집 대상 카테고리가 없습니다")
        for t in self.targets:
            if not str(t.code).strip().isdigit():
                raise ValueError(f"잘못된 카테고리 코드: {t.code}")
            if not str(t.label).strip():
                raise ValueError(f"카테고리 라벨이 비어 있습니다: {t.code}")
        if not 1 <= self.max_pages <= 200:
            raise ValueError(f"max_pages 범위 오류: {self.max_pages}")
        if self.page_delay_min < 0 or self.page_delay_max < self.page_delay_min:
            raise ValueError("페이지 딜레이 범위 오류")
        if self.phase2_delay < 0:
            raise ValueError("phase2_delay 는 0 이상이어야 합니다")
        if self.max_items < 1:
            raise ValueError("max_items 는 1 이상이어야 합니다")


def build_result_from_codes(
    label: str, codes: list[str], collected: set[str], blocked: bool = False
) -> PrescanResult:
    """goodscode 목록 + collected_ids 대조 → PrescanResult (Prescanner 와 동일 판정).

    blocked=True 는 fetch 자체가 실패(네트워크/403/봇)한 경우 — 빈 결과여도
    '상품 없음'이 아니라 '차단/오류'로 표기해 재시도를 유도한다. 기확보
    codes 가 있어도 fetch 가 실패한 페이지가 있는 것이므로 '수집 가능'이
    아니라 '차단/오류'로 표기해 재시도를 유도한다 — 절반만 읽은 목록을
    정상 완료로 오판해 나머지를 영구 누락시키지 않기 위함이다.
    """
    total = len(codes)
    new_codes = [c for c in codes if c not in collected]
    new_count = len(new_codes)
    already = total - new_count

    if blocked:
        status = STATUS_BLOCKED
    elif total == 0:
        status = STATUS_EMPTY
    elif new_count == 0:
        status = STATUS_COMPLETED
    else:
        status = STATUS_COLLECTABLE

    return PrescanResult(
        category_name=label,
        source=SOURCE_CATEGORY,
        total_codes=total,
        new_codes=new_count,
        already_collected=already,
        codes=codes,
        status=status,
    )


class GmarketCategoryLister:
    """Phase A: Web Unlocker로 대상 카테고리들의 goodscode 목록 수집.

    브라우저(Chromium)가 필요 없고 로컬 IP 평판에 좌우되지 않는다. 취소/
    일시정지는 control checkpoint(건/페이지 경계)로 처리한다.
    """

    def __init__(
        self,
        control=None,
        on_log: LogFn = _noop_log,
        on_progress: ProgressFn = _noop_progress,
        max_pages: int = config.CATEGORY_MAX_PAGES,
        page_delay_min: float = config.CATEGORY_PAGE_DELAY_MIN,
        page_delay_max: float = config.CATEGORY_PAGE_DELAY_MAX,
    ) -> None:
        self.control = control
        self.on_log = on_log
        self.on_progress = on_progress
        self.max_pages = max(1, max_pages)
        self.page_delay_min = max(0.0, page_delay_min)
        self.page_delay_max = max(self.page_delay_min, page_delay_max)
        # Unlocker 요청 3요소 — collect() 에서 설정 탭 값으로 확정된다.
        # 기본값은 config 상수(_fetch_page 단위 테스트가 직접 호출할 때 필요).
        self._token = ""
        self._zone = config.BRIGHTDATA_ZONE
        self._country = config.BRIGHTDATA_COUNTRY

    # ── 공개 API ──────────────────────────────────────────────────────────
    def collect(self, targets: list[CategoryTarget]) -> list[ListingOutcome]:
        """대상 카테고리를 순회하며 goodscode 목록 수집.

        반환 순서 = targets 순서. 토큰이 없으면 즉시 RuntimeError —
        설정 탭에서 자신의 Bright Data API 토큰을 입력하면 그 계정 키로
        사용량이 차감된다(환경변수·output/brightdata_token.txt 도 이전과
        같은 우선순위 폴백으로 유효).
        """
        outcomes: list[ListingOutcome] = []
        if not targets:
            return outcomes

        settings = brightdata.load_settings()
        token = brightdata.resolve_api_token(settings)
        if not token:
            raise RuntimeError(
                "Bright Data API 토큰이 설정되지 않았습니다. 앱 '설정' 탭에서 "
                "API 토큰을 입력·저장한 뒤 다시 시도하세요. (환경변수 "
                "BRIGHTDATA_API_TOKEN 또는 output/brightdata_token.txt 도 인식합니다)"
            )
        self._token = token
        self._zone, self._country = (
            settings.unlocker_zone or config.BRIGHTDATA_ZONE,
            settings.country or config.BRIGHTDATA_COUNTRY,
        )

        self._checkpoint()
        self.on_log(
            f"[카테고리 리스팅] Web Unlocker 시작 (zone={self._zone}, "
            f"country={self._country}, 토큰 "
            f"{brightdata.masked_token(token)} — 입력된 계정 키로 차감)..."
        )
        with requests.Session() as session:
            for idx, target in enumerate(targets, start=1):
                self._checkpoint()
                self.on_log(
                    f"[리스팅] ({idx}/{len(targets)}) {target.label} "
                    f"(최대 {self.max_pages}페이지) 조사 중..."
                )
                outcome = self._collect_one(session, target)
                outcomes.append(outcome)
                self.on_log(
                    f"[리스팅] {target.label}: goodscode {len(outcome.codes)}개 "
                    f"{'(차단/오류)' if outcome.blocked else ''}"
                )

        self.on_log("[카테고리 리스팅] 완료.")
        return outcomes

    # ── 내부 ──────────────────────────────────────────────────────────────
    def _collect_one(self, session: requests.Session,
                     target: CategoryTarget) -> ListingOutcome:
        """단일 카테고리 리스팅 페이지 1..N 순회 → ListingOutcome."""
        seen: list[str] = []
        seen_set: set[str] = set()
        empty_streak = 0

        for page in range(1, self.max_pages + 1):
            self._checkpoint()
            self.on_progress(target.label, page, self.max_pages)
            url = category_list_url(target.code, page if page > 1 else None)
            self.on_log(f"  [{target.label}] page {page} 로드...")

            codes, blocked = self._fetch_page(session, url, target)
            if blocked:
                return ListingOutcome(label=target.label, codes=seen, blocked=True)

            fresh = [c for c in codes if c not in seen_set]
            if fresh:
                empty_streak = 0
                for c in fresh:
                    seen_set.add(c)
                    seen.append(c)
                self.on_log(
                    f"  [{target.label}] page {page}: {len(codes)}개 중 신규 {len(fresh)}개 "
                    f"(누적 {len(seen)})"
                )
            else:
                empty_streak += 1
                self.on_log(
                    f"  [{target.label}] page {page}: 신규 0개 — 연속 {empty_streak}회"
                )
                if empty_streak >= config.CATEGORY_EMPTY_PAGE_TOLERANCE:
                    self.on_log(f"  [{target.label}] 빈 페이지 연속 — 종료")
                    break

            if page < self.max_pages:
                self._sleep(random.uniform(self.page_delay_min, self.page_delay_max))

        return ListingOutcome(label=target.label, codes=seen, blocked=False)

    def _fetch_page(self, session: requests.Session, url: str,
                    target: CategoryTarget) -> tuple[list[str], bool]:
        """Unlocker 1요청 → (goodscode 목록, blocked 여부).

        blocked=True 는 재시도 소진 후에도 오류/차단으로 페이지를 읽지 못한
        경우. 200 으로 정상 수신됐으면(코드 0개여도) blocked=False.

        Unlocker 실패는 200 + 빈 본문 + `x-brd-error` 헤더로 포장되기도 한다
        (BRIGHTDATA_AKAMAI_REVIEW §2) — 상태 코드와 함께 헤더를 검사한다.
        200 수신 후 goodscode 0개("빈 껍데기")는 차단도 목록 끝일 수도 있지만
        대량 실측(ACCESS_ROUTES_RESEARCH §9)에서 재시도 1회로 343건 중 278건이
        회복됐다 — 빈 페이지를 연속 종료 판정에 넣기 전 1회 재요청으로 회복을
        시도한다(2026-09-09 검토 반영).
        """
        payload = {
            "zone": self._zone,
            "url": url,
            "format": "raw",
            "country": self._country,
        }
        headers = {"Authorization": f"Bearer {self._token}"}
        empty_retried = False

        for attempt in range(config.LISTING_MAX_RETRIES + 1):
            self._checkpoint()
            try:
                r = session.post(
                    config.BRIGHTDATA_API_URL,
                    headers=headers,
                    json=payload,
                    timeout=config.BRIGHTDATA_TIMEOUT,
                )
            except Exception as e:  # noqa: BLE001 - 재시도 루프에서 다양한 fetch 예외 포괄
                self.on_log(f"  [{target.label}] fetch 오류: {e}")
                if attempt < config.LISTING_MAX_RETRIES:
                    self._wait_blocked(target.label)
                    continue
                return [], True

            html = r.text or ""
            resp_headers = getattr(r, "headers", None) or {}
            brd_error = str(resp_headers.get("x-brd-error", "") or "").strip()
            if r.status_code != 200 or brd_error or contains_bot_challenge(html):
                detail = f"HTTP {r.status_code}"
                if brd_error:
                    detail += f", x-brd-error: {brd_error[:80]}"
                self.on_log(
                    f"  [{target.label}] 차단/오류 감지({detail}), "
                    f"{config.UNLOCKER_RETRY_WAIT}초 대기..."
                )
                if attempt < config.LISTING_MAX_RETRIES:
                    self._wait_blocked(target.label)
                    continue
                return [], True

            if "CategoryLargeFuction" in html:
                # 대분류(L-code) 레거시 변형 — 실측(2026-09-08): 상품 목록 없이
                # 광고 goodscode 링크 7개만 담은 CategoryLarge 셸이 온다.
                # 광고 링크를 상품으로 오인하지 않게 빈 페이지로 취급한다
                # (연속 빈 페이지 허용치로 순회 종료).
                self.on_log(f"  [{target.label}] 레거시 대분류 변형 페이지 — 목록 없음, 건너뜀")
                return [], False

            codes = extract_goodscodes(html)
            if codes:
                return codes, False

            # ── 빈 껍데기 회복 재시도 (200 + 코드 0) ─────────────────────
            # 1회만 재요청한다 — 그래도 비면 빈 페이지로 반환해 호출자의
            # 연속 빈 페이지 종료 판정이 그대로 적용된다.
            if not empty_retried:
                empty_retried = True
                self.on_log(
                    f"  [{target.label}] 빈 응답(200, 코드 0) — "
                    f"{config.UNLOCKER_EMPTY_RETRY_WAIT}초 후 1회 재요청..."
                )
                self._wait_seconds(config.UNLOCKER_EMPTY_RETRY_WAIT)
                continue
            return [], False

        return [], True

    def _wait_blocked(self, name: str) -> None:
        """재시도 대기: 취소 반응성을 위해 1초 단위 분할 대기."""
        self._wait_seconds(config.UNLOCKER_RETRY_WAIT)

    def _wait_seconds(self, seconds: float) -> None:
        for _ in range(max(0, int(seconds))):
            self._checkpoint()
            time.sleep(1)

    def _checkpoint(self) -> None:
        if self.control is not None:
            self.control.checkpoint()

    def _sleep(self, seconds: float) -> None:
        if self.control is not None:
            self.control.sleep(seconds)
        else:
            time.sleep(seconds)
