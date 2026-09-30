"""AliExpress 카테고리 탭 — Phase A(리스팅) + Phase B(판매자정보) 엔진 (Qt 비의존).

쿠팡 카테고리 탭(app/core/coupang/decodo_run.py)과 동일한 회선·재개 규율을 적용한다:
- Phase A: 대상 카테고리 1~max_pages 페이지네이션 순회하여 상품 목록 확보
- Plan: 스마트 판매자 중복 제거 및 수집 계획 확정 (시드 캐시 활용)
- Phase B: Decodo 한국 주거용 고정 회선(Sticky 1440min Session) 기반 브라우저 세션 운용
  - rotation_batch_size 건 단위 자동 회선 순환 + WAF punish 감지 시 즉시 세션 교체
    (교체 사이 최소 간격 스로틀 적용 — 급속 순환 억제. 회선 소모 예산은
    2026-09-29 폐지: 교체가 잦아도 수집은 계속 진행한다)
  - 회선 확보 시 실제 출발 국가를 확인해(concatenated _korea_proxy 규율) 한국이 아니면
    최대 MAX_GEO_ROTATIONS 회 새 회선으로 교체한다
  - 상품 데이터(mtop) 미수신 페이지는 기록하지 않고, 프록시 사용 시 같은 상품을
    새 회선에서 즉시 재확인한다 — 빈 껍데기를 확정 저장해 누락시키는 일을 막는다
  - 네트워크·미수신 실패는 영구 제외하지 않는다. 이전 버전의 실패 카운터가
    남아 있어도 재개 시 다시 확인한다
  - 새 회선에서도 상품 2건이 연속 실패하면 그 지점을 저장한 채 안전 중단한다.
    로컬 모드는 CONSECUTIVE_BLOCK_ABORT 상한을 사용한다.
    다음 시작(우회 회선 선택)에서 저장된 지점부터 이어서 수집된다
- 재개 규율(쿠팡 search_crawler 동일): 재개 경계는 상품이 확인된 마지막
  페이지(last_item_page)이며, 그 페이지를 다시 확인한 뒤 다음 페이지부터
  이어간다. 0건으로 기록된 페이지(소프트 차단·렌더 지연)는 미확인으로 간주해
  다시 읽고, 빈 페이지 연속 판정(empty_streak)은 새 회선에서 0부터 다시 관찰한다
- 시도(MAX_ATTEMPTS)마다 새 회선으로 재시작하며 진행 기록(resume.sqlite3)을 공유한다.
  시도 간 대기(RETRY_WAIT_SECONDS)는 취소·일시정지 가능한 분할 대기다
- 비동기 mtop API 인터셉트를 통한 공정위 7대 사업자 정보 추출
- 실시간 CSV append 저장 (중간 유실 0건) + JSON 주기 재작성 (대량 수집 I/O 완화)
"""

from __future__ import annotations

import asyncio
import csv
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from patchright.async_api import async_playwright

from app.core import config, decodo
from app.core.aliexpress_resume_store import (
    STATUS_FINISHED,
    STATUS_LISTING_DONE,
    AliexpressResumeStore,
    ResumeStoreError,
    ali_category_run_dir,
)
from app.core.base import CancelledError, Control

LogFn = Callable[[str], None]
ProgressFn = Callable[[str, int, int], None]  # (label, current, total)
PhaseFn = Callable[[str, int, int], None]     # (phase_name, current_phase, total_phases)
CollectedFn = Callable[[dict], None]
StatsFn = Callable[[object], None]
ErrorFn = Callable[[str], None]
ExitIpFn = Callable[[dict], object]           # decodo.fetch_exit_ip 와 동일 시그니처


def _noop(*_a, **_k) -> None:
    pass


# ── 쿠팡 decodo_run 과 동일한 시도·회선 규율 상수 ─────────────────────
MAX_ATTEMPTS = 3               # 카테고리 1건을 최대 몇 회(회선)까지 재시작할까
RETRY_WAIT_SECONDS = 90.0      # 시도(회선) 간 대기 — 제어 가능한 분할 대기
MAX_GEO_ROTATIONS = 3          # 세션 1개 확보 시 한국 확인 재시도 상한
CONSECUTIVE_BLOCK_ABORT = 10   # 연속 차단 상한 — 도달 시 안전 중단(저장 지점 보존)
PROXY_FAILURE_ABORT = 2        # 새 회선 재확인까지 실패한 상품이 연속 2건이면 중단
# 회선 교체 스로틀 — 세션 교체가 잦은 날에도 수집은 계속 진행한다(2026-09-29
# 사용자 결정: 회선 소모 예산은 폐지). 교체 사이 최소 간격만 둬 급속 순환으로
# 인한 차단 악화만 억제한다.
ROTATION_MIN_INTERVAL_SECONDS = 30.0   # 회선 교체(새 세션) 사이 최소 간격
PHASE1_PAGE_RETRIES = 3        # 목록 페이지 1장의 로드·차단 재시도 상한
PDP_ITEM_RETRIES = 3           # 상세 페이지 1건의 재시도 상한
PDP_RESPONSE_WAIT_MS = 5000    # mtop 응답을 기다리는 최대 시간(탐지 지연 완화)
# 과거 버전과의 import 호환용 상수. 네트워크·미수신 실패를 영구 제외하면
# 회선 문제로 상품이 사라질 수 있으므로 현재 크롤러에서는 사용하지 않는다.
ITEM_GIVE_UP_RUNS = 3
# 결과 JSON 전체 재작성 주기(건) — 매건 재작성은 대량 수집에서 O(n²) I/O 다.
# 50건 모일 때와 중단·완료 시점에만 재작성하고, CSV 는 매건 append 로 실시간을
# 유지한다. 재개 시 CSV/JSON 을 저장소에서 재구축하므로 유실은 없다.
JSON_FLUSH_INTERVAL = 50

HOME_WARMUP_URL = "https://ko.aliexpress.com/?spm=a2g0o.home.logo.1.6c2f52d1NGQ4SZ"

# CDP Network.setBlockedURLs 는 요청을 가로채지 않으므로 Playwright route 를
# 쓸 때 생기는 브라우저 캐시 비활성화 부작용이 없다. API/JS 요청은 건드리지
# 않고, 실제 사용량이 큰 이미지·영상·폰트·분석 호스트만 막는다.
ALI_RESOURCE_BLOCK_PATTERNS = (
    "https://ae-pic-a1.aliexpress-media.com/*",
    "https://gv-vod-cdn.aliexpress-media.com/*",
    "https://aplus.aliexpress.com/*",
    "*://assets.aliexpress-media.com/*.jpg*",
    "*://assets.aliexpress-media.com/*.jpeg*",
    "*://assets.aliexpress-media.com/*.png*",
    "*://assets.aliexpress-media.com/*.webp*",
    "*://assets.aliexpress-media.com/*.avif*",
    "*://assets.aliexpress-media.com/*.gif*",
    "*://*/*.mp4*",
    "*://*/*.webm*",
    "*://*/*.m3u8*",
    "*://*/*.m4s*",
    "*://*/*.woff*",
    "*://*/*.ttf*",
    "*://*/*.otf*",
    "*://*/*.eot*",
)

# 차단 중단 안내 — UI 다이얼로그와 동일 문구를 요약 error 로도 운반한다
BLOCKED_ABORT_GUIDE = (
    "IP 차단이 해소되지 않아 수집을 중단했습니다. "
    "다시 시작할 때 우회 회선(Decodo)을 선택하면 중단 지점부터 이어서 수집됩니다."
)
RESPONSE_ABORT_GUIDE = (
    "상품 응답이 반복해서 비어 있어 추가 요청을 멈췄습니다. "
    "원인을 IP 차단으로 단정하지 않고, 저장된 미처리 상품은 다시 시작할 때 확인합니다."
)


def _is_blocked_url(url: str) -> bool:
    """AliExpress WAF 차단 신호 URL(punish / tmd) 판정."""
    return "punish" in (url or "") or "_____tmd_____" in (url or "")


_PHONE_LABEL_ALIASES = frozenset(
    {
        # 한국어 표기 — 공백·괄호·구분기호는 아래 정규화 후 비교한다.
        "소비자상담전화번호",
        "전화번호",
        "연락처",
        "고객센터",
        "고객센터전화",
        "고객센터전화번호",
        "문의전화",
        "문의처",
        "상담전화",
        "상담전화번호",
        # 상품 정보에서 실제로 쓰이는 영어 표기만 허용한다.
        "phone",
        "phonenumber",
        "telephone",
        "telephonenumber",
        "tel",
        "contactnumber",
        "contactphonenumber",
        "customerservicephone",
        "customerservicephonenumber",
        "customerservicetelephone",
        "customerservicetelephonenumber",
        "customerservicehotline",
        "hotline",
    }
)

_PHONE_PLACEHOLDER_EXACT = frozenset(
    {
        "",
        "-",
        "na",
        "none",
        "null",
        "없음",
        "미기재",
        "참조",
        "상세참고",
        "상세참조",
        "상세페이지참고",
        "상세페이지참조",
        "상세설명참고",
        "상세설명참조",
        "상세페이지별도표기",
        "referto",
        "details",
        "detail",
        "referproductdetails",
        "refertoproductdetail",
        "refertoproductdetails",
        "seproductdetails",
        "seeproductdetail",
        "seeproductdetails",
        "seethedetail",
        "seethedetails",
    }
)

_PHONE_CANDIDATE_PATTERNS = (
    # 한국 국가번호 뒤에 국내번호가 붙어도 중간 구분기호가 없을 수 있다:
    # 82-1012345678, +82 1012345678 등. 이 패턴을 국내번호보다 먼저 본다.
    re.compile(r"(?<!\d)\+?82[./\s\-]\d{8,12}(?!\d)"),
    # 한국 유선·휴대전화 표기: 02-123-4567, 070 1234 5678 등
    re.compile(r"(?<!\d)0\d{1,3}[./\s()\-]*\d{3,4}[./\s()\-]*\d{4}(?!\d)"),
    # 국가번호가 붙은 전화: +82 2-1234-5678, +1 212-555-1234 등
    re.compile(r"(?<!\d)\+?\d{1,3}(?:[./\s\-]\d{1,4}){2,4}(?!\d)"),
    # 구분기호 없는 한국 전화번호
    re.compile(r"(?<!\d)0\d{7,10}(?!\d)"),
    # 8자리 지역·대표번호(1234-5678 등)
    re.compile(r"(?<!\d)\d{3,4}[./\s\-]\d{4}(?!\d)"),
    # 1588xxxx·1600xxxx 같은 8자리 대표번호는 구분기호가 없기도 하다.
    re.compile(r"(?<!\d)\d{8}(?!\d)"),
)


def _normalize_property_label(value: object) -> str:
    """속성명 비교용 정규화 — 값 자체는 임의 숫자 추출에 사용하지 않는다."""
    return re.sub(r"[\W_]+", "", str(value or "").casefold())


def _is_phone_label(value: object) -> bool:
    return _normalize_property_label(value) in _PHONE_LABEL_ALIASES


def _is_phone_placeholder(value: object) -> bool:
    raw = str(value or "").strip()
    compact = _normalize_property_label(raw)
    if compact in _PHONE_PLACEHOLDER_EXACT:
        return True
    return any(
        marker in compact
        for marker in (
            "상세참고",
            "상세참조",
            "상세설명참고",
            "상세설명참조",
            "상세페이지별도표기",
            "refertoproductdetail",
            "seeproductdetail",
            "seethedetail",
        )
    )


def _clean_phone_candidate(value: object) -> str:
    """전화번호 속성에서만 번호를 골라 원문 표기를 보존한다.

    상담 가능 시간처럼 번호 뒤에 설명이 붙는 값은 허용하되, 전화 라벨이
    없는 다른 사업자 속성의 숫자를 이 함수에 넘기지 않는 것을 호출 규약으로
    삼는다.
    """
    raw = str(value or "").strip()
    if _is_phone_placeholder(raw):
        return ""
    for pattern in _PHONE_CANDIDATE_PATTERNS:
        for match in pattern.finditer(raw):
            candidate = match.group(0).strip()
            digits = re.sub(r"\D", "", candidate)
            if 8 <= len(digits) <= 15:
                return re.sub(r"\s+", " ", candidate)
    return ""


def _iter_property_pairs(product_props: object):
    if not isinstance(product_props, dict):
        return
    showed_props = product_props.get("showedProps", [])
    if isinstance(showed_props, list):
        for prop in showed_props:
            if isinstance(prop, dict):
                yield prop.get("attrName", ""), prop.get("attrValue", "")
    props_map = product_props.get("showedPropsMap", {})
    if isinstance(props_map, dict):
        for prop in props_map.values():
            if isinstance(prop, dict):
                yield prop.get("attrName", ""), prop.get("attrValue", "")


def _extract_phone_from_product_props(product_props: object) -> str:
    """showedProps/showedPropsMap의 전화 라벨 값만 전화번호로 추출한다."""
    for name, value in _iter_property_pairs(product_props):
        if not _is_phone_label(name):
            continue
        phone = _clean_phone_candidate(value)
        if phone:
            return phone
    return ""


def _merge_cached_phone(cached_info: dict, current_phone: object) -> tuple[str, dict | None]:
    """현재 상품의 유효 전화번호를 캐시에 반영하되 다른 필드는 보존한다."""
    cached_phone = _clean_phone_candidate(cached_info.get("phone", ""))
    current = _clean_phone_candidate(current_phone)
    if not current:
        return cached_phone, None
    if cached_info.get("phone") == current:
        return current, None
    merged = dict(cached_info)
    merged["phone"] = current
    return current, merged


class _LineFailure(RuntimeError):
    """한국 우회 회선 확보 실패 — 시도를 중단하고 다른 회선으로 재시작 대상."""


COUPANG_DATASET_FIELDS = [
    "vendor_id",                 # 판매자/스토어 식별자
    "url",                       # 상품 상세 URL
    "store_name",                # 스토어명
    "company_name",              # 회사/상호명 (사업자명)
    "ceo_name",                  # 대표자명
    "business_number",           # 사업자등록번호
    "phone",                     # 사업자 전화번호 (고객센터/소비자상담)
    "email",                     # 사업자 이메일 주소
    "address",                   # 사업장 소재지 (주소)
    "ecommerce_report_number",   # 통신판매업신고번호
    "power_seller",              # 우수판매자 여부 (TRUE/FALSE)
    "power_seller_title",        # 우수판매자 배지 명칭
    "rating_count",              # 누적 판매량
    "thumb_up_ratio",            # 긍정 평가율 (만족도)
    "product_title",             # 상품명
    "price",                     # 판매 가격
    "collected_at"               # 수집 일시
]


@dataclass(frozen=True)
class AliexpressCategoryRunConfig:
    """AliExpress 카테고리 탭 실행 설정."""

    output_dir: Path
    category_name: str
    category_url: str
    max_pages: int = 30
    rotation_batch_size: int = 25
    delay: float = 2.0
    use_proxy: bool = True
    start_fresh: bool = False
    block_cooldown_seconds: float = 15.0  # 차단 감지 직후의 안전 대기(로컬 회선·재시도 직전)
    # 회선 교체 스로틀 — 새 세션을 열기 직전 최소 간격만큼 대기해 급속 순환을
    # 막는다(2026-09-29 사용자 보고: '회선 복구가 너무 자주 발생' 반복). 회선이
    # 많이 나와도 수집은 계속 진행한다(회선 소모 예산 폐지).
    rotation_min_interval_seconds: float = ROTATION_MIN_INTERVAL_SECONDS

    def __post_init__(self) -> None:
        if not self.category_name.strip():
            raise ValueError("카테고리 이름이 비어 있습니다.")
        if not self.category_url.strip():
            raise ValueError("카테고리 URL이 비어 있습니다.")
        if not 1 <= self.max_pages <= 100:
            raise ValueError("max_pages 는 1~100 사이여야 합니다.")
        if self.rotation_min_interval_seconds < 0:
            raise ValueError("rotation_min_interval_seconds 는 0 이상이어야 합니다.")


class _ResourceBlockFailure(RuntimeError):
    """브라우저 리소스 차단을 설치하지 못해 안전 중단하는 오류."""


async def _install_resource_blocks(context, page):
    """Chromium CDP 차단과 Network 도메인을 웜업 전에 설치한다."""
    new_cdp_session = getattr(context, "new_cdp_session", None)
    if not callable(new_cdp_session):
        raise _ResourceBlockFailure(
            "브라우저가 CDP 리소스 차단을 지원하지 않아 수집을 중단했습니다."
        )
    try:
        cdp = await new_cdp_session(page)
        await cdp.send("Network.enable")
        await cdp.send(
            "Network.setBlockedURLs",
            {"urls": list(ALI_RESOURCE_BLOCK_PATTERNS)},
        )
        return cdp
    except Exception as exc:  # noqa: BLE001 - CDP 구현별 예외를 안전 중단
        raise _ResourceBlockFailure(
            f"브라우저 리소스 차단 설치에 실패해 수집을 중단했습니다: {type(exc).__name__}"
        ) from exc


def _safe_mtop_error_code(payload: object) -> str | None:
    """mtop 응답의 ``ret`` 코드만 안전한 문자 집합으로 추출한다.

    응답 본문·메시지·토큰은 로그에 남기지 않는다. mtop은 ret 를 문자열
    배열로 주는 경우가 많고, 일부 응답은 code/errorCode 키를 사용한다.
    """
    if not isinstance(payload, dict):
        return None
    ret = payload.get("ret")
    candidates: list[object] = []
    if isinstance(ret, (list, tuple)):
        candidates.extend(ret)
    elif isinstance(ret, dict):
        candidates.extend((ret.get("code"), ret.get("errorCode")))
    elif ret is not None:
        candidates.append(ret)
    candidates.extend((payload.get("code"), payload.get("errorCode")))
    data = payload.get("data")
    if isinstance(data, dict):
        nested_ret = data.get("ret")
        if isinstance(nested_ret, (list, tuple)):
            candidates.extend(nested_ret)
        elif isinstance(nested_ret, str):
            candidates.append(nested_ret)

    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        # ``FAIL_SYS_xxx::message`` 에서 코드 앞부분만 취한다.
        normalized = candidate.strip()
        if not normalized:
            continue
        token_parts = normalized.split("::", 1)[0].split(":", 1)[0].split(None, 1)
        if not token_parts:
            continue
        token = token_parts[0]
        if re.fullmatch(r"[A-Z][A-Z0-9_-]{0,63}", token):
            return token
        if re.fullmatch(r"\d{1,8}", token):
            return token
    return None


@dataclass
class AliexpressCrawlSummary:
    """수집 결과 요약 통계.

    termination_reason 은 쿠팡 decodo_run 의 종료 사유 체계와 맞춘 값이다:
    success / empty / blocked / line_error / error / resume_store_error / cancelled
    """

    total_items: int = 0
    collected_items: int = 0
    unique_vendors: int = 0
    has_email: int = 0
    has_business_number: int = 0
    has_ceo_name: int = 0
    # 이전 버전 호환용 필드. 현재는 네트워크·미수신 상품을 영구 제외하지 않으므로 0이다.
    given_up_items: int = 0
    csv_file: Path | None = None
    json_file: Path | None = None
    elapsed_seconds: float = 0.0
    error: str | None = None
    cancelled: bool = False
    termination_reason: str = ""
    # CDP Network.loadingFinished 의 encodedDataLength 합계. 브라우저 측
    # 추정치이며 Decodo 청구량과 동일한 값은 아니다.
    estimated_browser_bytes: int = 0
    browser_bytes_by_session: list[int] = field(default_factory=list)


class AliexpressCategoryCrawler:
    """AliExpress 카테고리 2단계 수집 엔진.

    crawl() 은 내부적으로 최대 MAX_ATTEMPTS 회 시도하며 시도마다 새 우회 회선을
    쓴다. 진행 기록은 시도 사이에 공유되므로 차단으로 끊겨도 다음 시도·다음
    실행이 저장된 지점부터 이어서 수집한다.
    """

    def __init__(
        self,
        config: AliexpressCategoryRunConfig,
        control: Control | None = None,
        on_log: LogFn = _noop,
        on_phase: PhaseFn = _noop,
        on_progress: ProgressFn = _noop,
        on_collected: CollectedFn = _noop,
        on_stats: StatsFn = _noop,
        on_error: ErrorFn = _noop,
        exit_ip_fn: ExitIpFn | None = None,
    ) -> None:
        self.config = config
        self.control = control
        self.on_log = on_log
        self.on_phase = on_phase
        self.on_progress = on_progress
        self.on_collected = on_collected
        self.on_stats = on_stats
        self.on_error = on_error
        self._exit_ip_fn = exit_ip_fn or decodo.fetch_exit_ip
        # 재개 시 저장본을 테이블에 다시 흘릴 때 중복 행이 생기지 않게 하는 가드
        self._emitted_urls: set[str] = set()
        # 회선 교체 스로틀 기준 — 직전 세션 오픈 시각(monotonic). None 이면
        # 아직 첫 세션이라 간격을 적용하지 않는다.
        self._last_session_open_at: float | None = None

    def crawl(self) -> AliexpressCrawlSummary:
        """동기 인터페이스: 시도 루프를 이벤트 루프에서 실행."""
        return asyncio.run(self._run_with_attempts())

    def _checkpoint(self) -> None:
        """일시정지/취소 체크포인트."""
        if self.control:
            self.control.checkpoint()

    def _sleep(self, seconds: float) -> None:
        """취소·일시정지 가능한 대기. 제어 객체가 없으면 단순 대기."""
        if seconds <= 0:
            return
        if self.control:
            self.control.sleep(seconds)
        else:
            time.sleep(seconds)

    async def _await_gap(self, seconds: float) -> None:
        """이벤트 루프를 막지 않으면서 취소 가능한 대기(장시간 쿨다운용)."""
        remaining = float(seconds)
        while remaining > 0:
            self._checkpoint()
            chunk = min(5.0, remaining)
            await asyncio.sleep(chunk)
            remaining -= chunk

    def _new_summary(self, csv_file: Path, json_file: Path) -> AliexpressCrawlSummary:
        return AliexpressCrawlSummary(csv_file=csv_file, json_file=json_file)

    def _apply_store_stats(self, summary: AliexpressCrawlSummary, store: AliexpressResumeStore) -> None:
        """실패·중단 요약에도 저장 누적분이 보이도록 진행 기록에서 통계를 채운다."""
        try:
            summary.collected_items = store.item_result_count
            summary.unique_vendors = store.confirmed_seller_count
            records = store.load_item_results()
            summary.has_email = sum(1 for record in records if record.get("email"))
            summary.has_business_number = sum(
                1 for record in records if record.get("business_number")
            )
            summary.has_ceo_name = sum(1 for record in records if record.get("ceo_name"))
        except ResumeStoreError:
            pass

    # ── 시도 루프 (쿠팡 run_category_attempts 대응) ──────────────────────
    async def _run_with_attempts(self) -> AliexpressCrawlSummary:
        start_time = time.time()
        safe_name = re.sub(r'[\\/*?:"<>| ]', "_", self.config.category_name)
        run_dir = ali_category_run_dir(
            self.config.output_dir, self.config.category_name, self.config.category_url
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        csv_file = run_dir / f"ali_category_{safe_name}.csv"
        json_file = run_dir / f"ali_category_{safe_name}.json"

        summary = self._new_summary(csv_file, json_file)

        if self.config.use_proxy and decodo.sticky_proxy_dict() is None:
            summary.termination_reason = "error"
            summary.error = (
                "Decodo 계정(사용자명/비밀번호)이 없습니다. "
                "설정 탭에 입력·저장한 뒤 다시 시작하세요."
            )
            self.on_log(f"[Decodo] {summary.error}")
            summary.elapsed_seconds = round(time.time() - start_time, 1)
            return summary

        store = AliexpressResumeStore(run_dir)
        try:
            try:
                if self.config.start_fresh:
                    archived = store.archive()
                    if archived:
                        self.on_log(f"[아카이브] 이전 진행 기록 보관 완료: {archived.name}")
                store.open()
                if not self.config.start_fresh and store.status == STATUS_FINISHED and store.has_state():
                    # 구버전은 실패 카운터 상한 상품을 제외한 채 finished 로
                    # 봉인할 수 있었다. 목록에 상세 결과가 없는 상품이 남아
                    # 있으면 그 기록을 새 수집으로 아카이브하지 않고 재개한다.
                    pending_count = store.pending_product_count()
                    if pending_count:
                        store.mark_listing_done(end_reason="legacy_pending")
                        self.on_log(
                            f"[재개] 이전 완료 기록에 미확인 상품 {pending_count:,}건이 남아 있어 "
                            "제외하지 않고 다시 확인합니다."
                        )
                    else:
                        # 완료 봉인된 기록은 이어서 수집 대상이 아니다 — 다시 시작은
                        # 새 수집이다(쿠팡 decodo_run 과 동일). 기록은 아카이브로 보존.
                        archived = store.archive()
                        if archived:
                            self.on_log(
                                "[재개] 이 카테고리의 이전 실행은 완료됐습니다 — 진행 기록을 "
                                f"보관하고 처음부터 새로 수집합니다: {archived.name}"
                            )
                        store.open()
                mismatch = store.check_config(
                    self.config.category_name, self.config.category_url, self.config.max_pages
                )
            except ResumeStoreError as e:
                summary.termination_reason = "resume_store_error"
                summary.error = f"진행 기록을 사용할 수 없어 재개를 중단합니다: {e}"
                self.on_log(f"[재개] {summary.error}")
                summary.elapsed_seconds = round(time.time() - start_time, 1)
                return summary

            if mismatch:
                summary.termination_reason = "error"
                summary.error = (
                    f"저장된 진행 기록과 설정이 일치하지 않아 재개를 중단합니다. {mismatch} "
                    "'처음부터 다시 수집'을 선택하거나 설정을 되돌리세요."
                )
                self.on_log(f"[재개] {summary.error}")
                summary.elapsed_seconds = round(time.time() - start_time, 1)
                return summary

            if store.has_state() and not self.config.start_fresh:
                boundary = store.last_item_page
                self.on_log(
                    f"[작업 이어하기] 이전 진행 기록 발견: 1~{store.last_completed_page}페이지 기록 "
                    f"(상품 {store.product_count:,}건 확보 / 판매자 {store.confirmed_seller_count:,}개사 수집 완료) "
                    f"→ {boundary}페이지를 경계로 다시 확인한 뒤 이어 수집합니다."
                )

            self.on_log(f"[Ali 카테고리] 수집 시작: '{self.config.category_name}' (최대 {self.config.max_pages}페이지)")
            self.on_log(f"[설정] 저장 폴더: {run_dir}")

            for attempt in range(1, MAX_ATTEMPTS + 1):
                self._checkpoint()
                self.on_log(
                    f"[시도 {attempt}/{MAX_ATTEMPTS}] "
                    + ("우회 회선(Decodo 한국 고정)" if self.config.use_proxy else "로컬 회선 안전 모드")
                )
                try:
                    summary = await self._crawl_attempt(store, run_dir, csv_file, json_file)
                except CancelledError:
                    live = getattr(self, "_active_summary", None)
                    summary = live if live is not None else self._new_summary(csv_file, json_file)
                    summary.csv_file = csv_file
                    summary.json_file = json_file
                    if int(summary.collected_items or 0) == 0:
                        # 목록 단계 진행 중 취소 — 요약이 비어 있으면 저장 누적분을
                        # 채워 UI 가 실제 확보 규모를 보고하게 한다.
                        self._apply_store_stats(summary, store)
                    summary.cancelled = True
                    summary.termination_reason = "cancelled"
                    self.on_log("[Ali 카테고리] 사용자에 의해 수집이 취소되었습니다.")
                    break
                except ResumeStoreError as e:
                    # 진행 기록 쓰기 실패 — 회선을 바꿔도 같은 지점에서 다시
                    # 실패하므로 재시도하지 않는다(쿠팡 decodo_run 규율 동일).
                    summary = self._new_summary(csv_file, json_file)
                    summary.termination_reason = "resume_store_error"
                    summary.error = f"진행 기록 저장 실패로 중단했습니다: {e}"
                    self.on_log(f"[재개] {summary.error}")
                    return summary
                except _LineFailure as e:
                    summary = self._new_summary(csv_file, json_file)
                    summary.termination_reason = "line_error"
                    summary.error = str(e)
                    self._apply_store_stats(summary, store)
                    self.on_log(f"[Decodo] 시도 {attempt} 회선 확보 실패 — {e}")
                except _ResourceBlockFailure as e:
                    summary = self._new_summary(csv_file, json_file)
                    summary.termination_reason = "error"
                    summary.error = str(e)
                    self._apply_store_stats(summary, store)
                    self.on_log(f"[브라우저] {summary.error}")
                    break
                except Exception as e:
                    summary = self._new_summary(csv_file, json_file)
                    summary.termination_reason = "error"
                    summary.error = f"{type(e).__name__}: {e}"
                    self._apply_store_stats(summary, store)
                    self.on_log(f"[오류] 시도 {attempt} 실패: {summary.error}")
                else:
                    if summary.cancelled or summary.termination_reason in ("success", "empty"):
                        break
                    if int(summary.collected_items or 0) == 0:
                        # 목록 단계에서 끝난 시도 — 요약에 저장 누적분이라도
                        # 비치도록 채운다(시도 소진 시 마지막 요약이 최종 표시된다).
                        self._apply_store_stats(summary, store)
                    self.on_log(f"[Ali 카테고리] 시도 {attempt} 중단 ({summary.termination_reason})")

                if attempt >= MAX_ATTEMPTS:
                    self.on_log(f"[Ali 카테고리] {MAX_ATTEMPTS}회 모두 실패 — 마지막 상태로 종료합니다")
                    break
                self.on_log(f"[Ali 카테고리] {int(RETRY_WAIT_SECONDS)}초 후 새 회선으로 다시 시도합니다")
                try:
                    self._sleep(RETRY_WAIT_SECONDS)
                except CancelledError:
                    summary.cancelled = True
                    summary.termination_reason = "cancelled"
                    self.on_log("[Ali 카테고리] 재시도 대기 중 취소되었습니다.")
                    break
        finally:
            store.close()

        summary.elapsed_seconds = round(time.time() - start_time, 1)
        if summary.termination_reason == "success":
            self.on_log(
                f"\n[Ali 카테고리 완료] 총 {summary.collected_items:,}건 수집 완료 "
                f"(고유 판매자 {summary.unique_vendors}개사, 소요 {summary.elapsed_seconds}초)"
            )
            self.on_log(f"  -> 이메일 확보: {summary.has_email:,}건 | 사업자번호: {summary.has_business_number:,}건")
            self.on_log(f"  -> 파일: {csv_file}")
        return summary

    # ── 1회 시도: 브라우저 세션 1개(회선 1개)로 전체 파이프라인 실행 ────
    async def _crawl_attempt(
        self,
        store: AliexpressResumeStore,
        run_dir: Path,
        csv_file: Path,
        json_file: Path,
    ) -> AliexpressCrawlSummary:
        summary = self._new_summary(csv_file, json_file)
        # 취소·예외로 시도가 끊겨도 지금까지의 통계를 잃지 않게 요약을 보관한다
        self._active_summary = summary

        async with async_playwright() as p:
            session_counter = 0
            current_browser = None
            current_context = None
            current_page = None
            current_cdp = None
            current_session_bytes = 0
            browser_bytes_by_session: list[int] = []

            def update_browser_byte_summary() -> None:
                summary.estimated_browser_bytes = sum(browser_bytes_by_session)
                summary.browser_bytes_by_session = list(browser_bytes_by_session)

            async def close_session() -> None:
                nonlocal current_browser, current_context, current_page
                nonlocal current_cdp, current_session_bytes
                if current_cdp is not None:
                    browser_bytes_by_session.append(max(0, int(current_session_bytes)))
                    update_browser_byte_summary()
                    self.on_log(
                        "  [네트워크] 브라우저 측 수신 추정 "
                        f"{current_session_bytes:,}바이트 (청구량과 다른 측정값)"
                    )
                    current_cdp = None
                    current_session_bytes = 0
                if current_page:
                    try:
                        await current_page.close()
                    except Exception:
                        pass
                    current_page = None
                if current_context:
                    try:
                        await current_context.close()
                    except Exception:
                        pass
                    current_context = None
                if current_browser:
                    try:
                        await current_browser.close()
                    except Exception:
                        pass
                    current_browser = None

            async def open_new_session() -> None:
                """새 스티키 세션 확보. 우회 모드에서는 한국 출발 회선만 채택한다
                (쿠팡 _korea_proxy 와 동일 — 확인 실패 시 MAX_GEO_ROTATIONS 회 교체)."""
                nonlocal session_counter, current_browser, current_context, current_page
                nonlocal current_cdp, current_session_bytes
                await close_session()

                # 회선 교체 스로틀 — 직전 세션 오픈과 최소 간격을 두어 급속
                # 순환(새 회선 연속 채택)을 막는다. 첫 세션에는 적용하지
                # 않는다. 새 회선을 뽑는 행위 자체가 평판을 깎는다(실측).
                gap = float(
                    getattr(self.config, "rotation_min_interval_seconds", 0.0) or 0.0
                )
                last_open = self._last_session_open_at
                elapsed = (
                    time.monotonic() - last_open if last_open is not None else None
                )
                if gap > 0 and elapsed is not None and elapsed < gap:
                    wait_s = gap - elapsed
                    self.on_log(
                        f"[회선 순환 스로틀] 급속 교체 방지 — {wait_s:.0f}초 대기 후 "
                        "새 회선을 엽니다."
                    )
                    await self._await_gap(wait_s)
                self._last_session_open_at = time.monotonic()

                proxy = None
                if self.config.use_proxy:
                    last_geo_reason = ""
                    for geo in range(1, MAX_GEO_ROTATIONS + 1):
                        self._checkpoint()
                        session_counter += 1
                        sid = f"ali_cat_{int(time.time()) % 100000}_{session_counter}"
                        candidate = decodo.sticky_proxy_dict(session_id=sid)
                        if candidate is None:
                            raise _LineFailure(
                                "Decodo 계정(사용자명/비밀번호)을 읽지 못했습니다. "
                                "로컬 회선으로 잘못 수집하는 일을 막기 위해 중단합니다."
                            )
                        self.on_log(
                            f"[Decodo] 회선 확인 {geo}/{MAX_GEO_ROTATIONS} — {decodo.proxy_summary(candidate)}"
                        )
                        try:
                            info = await asyncio.to_thread(self._exit_ip_fn, candidate)
                        except CancelledError:
                            raise
                        except Exception as e:  # DecodoError 포함 — 다른 회선으로
                            last_geo_reason = str(e)
                            self.on_log(f"[Decodo] 회선 확인 실패 — {last_geo_reason}. 다른 회선으로 바꿉니다.")
                            continue
                        country = info.country_name or info.country_code or "?"
                        if info.is_korea:
                            self.on_log(f"[Decodo] 한국 회선 확인 — {info.ip} ({country})")
                            proxy = candidate
                            break
                        last_geo_reason = country
                        self.on_log(f"[Decodo] 한국이 아닌 회선 ({country}) — 새 회선으로 교체")
                    if proxy is None:
                        raise _LineFailure(
                            "한국 회선을 확보하지 못했습니다"
                            + (f" ({last_geo_reason})" if last_geo_reason else "")
                        )

                launch_args = {
                    "headless": True,
                    "args": [
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                    ]
                }
                if proxy:
                    launch_args["proxy"] = proxy

                current_browser = await p.chromium.launch(**launch_args)
                current_context = await current_browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                    locale="ko-KR",
                    viewport={"width": 1440, "height": 900}
                )
                await current_context.add_cookies([
                    {"name": "aep_usuc_f", "value": "region=KR&b_locale=ko_KR&site=kor&c_tp=KRW", "domain": ".aliexpress.com", "path": "/"},
                    {"name": "xman_us_f", "value": "x_locale=ko_KR&x_l=0", "domain": ".aliexpress.com", "path": "/"}
                ])
                current_page = await current_context.new_page()

                # 홈 웜업보다 먼저 설치해야 첫 페이지부터 이미지/영상/분석
                # 트래픽이 회선으로 내려가지 않는다. CDP 설치 실패는 차단
                # 없이 진행하면 곧바로 비용이 생기므로 안전하게 중단한다.
                try:
                    current_cdp = await _install_resource_blocks(current_context, current_page)
                    current_session_bytes = 0

                    def on_loading_finished(event: object) -> None:
                        nonlocal current_session_bytes
                        if not isinstance(event, dict):
                            return
                        encoded = event.get("encodedDataLength")
                        if isinstance(encoded, (int, float)) and encoded >= 0:
                            current_session_bytes += int(encoded)

                    current_cdp.on("Network.loadingFinished", on_loading_finished)
                except Exception as exc:  # noqa: BLE001 - 측정 설치도 비용 보호에 필요
                    if isinstance(exc, _ResourceBlockFailure):
                        current_cdp = None
                        await close_session()
                        raise
                    current_cdp = None
                    await close_session()
                    raise _ResourceBlockFailure(
                        f"브라우저 네트워크 측정 설치에 실패해 수집을 중단했습니다: {type(exc).__name__}"
                    ) from exc

                # 홈 웜업
                try:
                    await current_page.goto(HOME_WARMUP_URL, wait_until="domcontentloaded", timeout=35000)
                    await current_page.wait_for_timeout(2000)
                except Exception:
                    pass

            async def block_backoff() -> None:
                """차단 신호 직후의 안전 조치 — 우회 모드면 회선 교체, 로컬이면 쿨다운."""
                if self.config.use_proxy:
                    self.on_log("    보안 차단 감지 — 새 우회 회선으로 교체합니다")
                    await open_new_session()
                else:
                    self.on_log(
                        f"    보안 차단 감지 — {self.config.block_cooldown_seconds:.0f}초 안전 대기 후 재시도"
                    )
                    if current_page:
                        await current_page.wait_for_timeout(int(self.config.block_cooldown_seconds * 1000))

            # ── Phase 1: 카테고리 리스팅 순회 ────────────────────────────
            self.on_phase("Phase 1: 카테고리 목록 탐색", 1, 2)
            unique_products: dict[str, dict] = {}

            if store.status == STATUS_LISTING_DONE:
                for prod in store.load_products():
                    unique_products[prod["id"]] = prod
                self.on_log(f"[Phase 1 건너뛰기] 이미 카테고리 목록 수집 완료됨 (총 {len(unique_products):,}개 상품 복원)")
            else:
                if store.last_completed_page > 0:
                    for prod in store.load_products():
                        unique_products[prod["id"]] = prod
                    self.on_log(
                        f"  [목록 복원] 1~{store.last_completed_page}페이지 기확보 상품 {len(unique_products):,}개 복원"
                    )
                    # 이전 실행의 빈 페이지 연속값을 이월하지 않는다 — 새 회선에서
                    # 0부터 다시 관찰한다(쿠팡 search_crawler reset_empty_streak 규율).
                    store.reset_empty_streak()

                # 재개 경계는 상품이 확인된 마지막 페이지(last_item_page)다. 0건으로
                # 기록된 페이지(소프트 차단·렌더 지연)는 미확인으로 간주해 다시 읽는다.
                boundary = store.last_item_page
                start_page = boundary + 1
                empty_streak = store.empty_streak

                if start_page <= self.config.max_pages:
                    if current_page is None:
                        await open_new_session()

                    sep = "&" if "?" in self.config.category_url else "?"
                    page_failures = 0

                    async def load_listing_page(p_num: int, label: str = "") -> tuple[list[dict], str]:
                        """목록 페이지 1장을 로드해 상품 카드를 추출한다.

                        로드 지연·차단 backoff 는 같은 페이지로 재시도하며, 재시도를
                        소진하면 요약에 종료 사유를 남기고 상태를 반환한다 —
                        "ok" / "line_stop" / "blocked_stop".
                        """
                        nonlocal page_failures
                        shown = label or f"{p_num}/{self.config.max_pages}"
                        p_url = f"{self.config.category_url}{sep}page={p_num}"
                        self.on_log(f"  [페이지 {shown}] 로드 중...")

                        while True:
                            try:
                                await current_page.goto(p_url, wait_until="domcontentloaded", timeout=35000)
                                await current_page.wait_for_timeout(1800)
                            except Exception as e:
                                page_failures += 1
                                if page_failures >= PHASE1_PAGE_RETRIES:
                                    summary.termination_reason = "line_error"
                                    summary.error = (
                                        f"목록 {p_num}페이지를 {page_failures}회 불러오지 못해 중단합니다: {e}"
                                    )
                                    return [], "line_stop"
                                self.on_log(f"    페이지 로드 지연 — 같은 페이지 재시도 ({page_failures}/{PHASE1_PAGE_RETRIES})")
                                await current_page.wait_for_timeout(3000)
                                continue

                            if _is_blocked_url(current_page.url):
                                page_failures += 1
                                if page_failures >= PHASE1_PAGE_RETRIES:
                                    summary.termination_reason = "blocked"
                                    summary.error = BLOCKED_ABORT_GUIDE
                                    self.on_log(
                                        f"    목록 {p_num}페이지 차단 {page_failures}회 지속 — 안전 중단. "
                                        "저장된 지점부터 재개됩니다."
                                    )
                                    return [], "blocked_stop"
                                await block_backoff()
                                continue
                            page_failures = 0

                            await current_page.evaluate("window.scrollBy(0, 1500)")
                            await current_page.wait_for_timeout(800)
                            await current_page.evaluate("window.scrollBy(0, 1500)")
                            await current_page.wait_for_timeout(800)

                            items = await current_page.evaluate("""() => {
                                const list = [];
                                const cards = Array.from(document.querySelectorAll('a[href*=\\"/item/\\"]'));
                                for (const a of cards) {
                                    const href = a.href || '';
                                    const m = href.match(/item\\/(\\d+)\\.html/);
                                    if (!m) continue;
                                    const id = m[1];
                                    if (list.some(x => x.id === id)) continue;

                                    const card = a.closest('[class*=\\"search-item\\"], [class*=\\"list--item\\"], div') || a;
                                    const text = card.innerText || '';
                                    const lines = text.split('\\n').map(l => l.trim()).filter(Boolean);

                                    let title = a.title || '';
                                    if (!title) {
                                        title = lines.find(l => !l.startsWith('-') && !l.startsWith('₩') && l.length > 5) || lines[0] || '';
                                    }

                                    let price = '';
                                    const pm = text.match(/₩[\\d,]+/);
                                    if (pm) price = pm[0];

                                    let orders = '';
                                    const om = text.match(/([\\d,]+(?:\\+|개)?\\s*판매)/);
                                    if (om) orders = om[1];

                                    const isTop = text.includes('TOP셀러') || text.includes('Top Seller');

                                    list.push({
                                        id: id,
                                        url: href,
                                        title: title.trim(),
                                        price: price,
                                        orders: orders,
                                        is_top_seller: isTop
                                    });
                                }
                                return list;
                            }""")
                            return items, "ok"

                    def take_new(items: list[dict]) -> int:
                        """신규 상품만 unique_products 에 합치고 신규 수를 반환."""
                        fresh = 0
                        for itm in items:
                            if itm["id"] not in unique_products:
                                unique_products[itm["id"]] = itm
                                fresh += 1
                        return fresh

                    if boundary > 0:
                        # 경계 페이지 재확인 — 마지막 저장 페이지를 새 회선에서 다시
                        # 읽어 누락·순서 변경을 되돌린 뒤 다음 페이지로 넘어간다
                        # (쿠팡 search_crawler 경계확인 규율 동일).
                        items_b, status_b = await load_listing_page(
                            boundary, f"{boundary}/{self.config.max_pages} 경계확인"
                        )
                        if status_b != "ok":
                            self.on_log(f"    {summary.error} — 다음 시도(회선)에서 이어서 수집합니다.")
                            await close_session()
                            return summary
                        fresh_b = take_new(items_b)
                        # 재동기화 단계 — 상품이 보이면 빈 페이지 판정을 리셋한다.
                        store.record_page(boundary, items_b, new_count=len(items_b))
                        empty_streak = store.empty_streak
                        if fresh_b > 0:
                            self.on_log(
                                f"  [재개] 경계 페이지 {boundary}에서 신규 상품 {fresh_b}개 — "
                                "목록 순서가 바뀌었습니다. 재확인분을 누적에 반영했습니다."
                            )

                    p_num = start_page
                    while p_num <= self.config.max_pages:
                        self._checkpoint()
                        self.on_progress("목록 탐색", p_num, self.config.max_pages)
                        items, status = await load_listing_page(p_num)
                        if status != "ok":
                            self.on_log(f"    {summary.error} — 다음 시도(회선)에서 이어서 수집합니다.")
                            await close_session()
                            return summary

                        new_count = take_new(items)
                        store.record_page(p_num, items, new_count=new_count)
                        empty_streak = store.empty_streak
                        self.on_log(f"    -> 발견 {len(items)}개 (신규 {new_count}개 / 누적 {len(unique_products)}개)")

                        if empty_streak >= 2:
                            self.on_log(f"    -> 연속 빈 페이지 감지: 목록 순회 종료 (총 {len(unique_products)}개 확보)")
                            break

                        await current_page.wait_for_timeout(1000)
                        p_num += 1

                    store.mark_listing_done(end_reason="empty" if empty_streak >= 2 else "max_pages")
                else:
                    store.mark_listing_done(end_reason="max_pages")

            total_items = len(unique_products)
            summary.total_items = total_items
            product_targets = list(unique_products.values())

            if total_items == 0:
                self.on_log("[오류] 상품 목록을 찾을 수 없습니다. 카테고리 URL을 확인하세요.")
                summary.termination_reason = "empty"
                await close_session()
                return summary

            self.on_log(f"[Phase 1 완료] 총 {total_items:,}개 상품 목록 확보 완료.")

            # ── Phase 2: 판매자 상세 사업자 정보 수집 ─────────────────────
            self.on_phase("Phase 2: 판매자 상세 수집", 2, 2)
            self.on_log(f"[Phase 2 시작] {total_items:,}개 상품 대상 사업자정보(이메일, 대표자, 사업자번호 등) 수집...")

            collected_records: list[dict] = store.load_item_results()
            processed_items: set[str] = store.processed_item_ids()
            vendor_cache: dict[str, dict] = store.confirmed_sellers()
            req_count_in_session = 0

            # 기존 저장소에서 고유 판매자 시드 자동 로드
            for f_path in list(run_dir.glob("*.json")) + list(self.config.output_dir.glob("*.json")):
                if f_path.name == json_file.name:
                    continue
                try:
                    with open(f_path, "r", encoding="utf-8") as f:
                        prev_records = json.load(f)
                        if isinstance(prev_records, list):
                            for pr in prev_records:
                                vid = pr.get("vendor_id")
                                cname = pr.get("company_name")
                                bnum = pr.get("business_number")
                                if vid and cname and bnum and cname != "(상세 미기재)" and vid not in vendor_cache:
                                    vendor_cache[vid] = {
                                        "store_name": pr.get("store_name", ""),
                                        "company_name": cname,
                                        "ceo_name": pr.get("ceo_name", ""),
                                        "business_number": bnum,
                                        "phone": pr.get("phone", ""),
                                        "email": pr.get("email", ""),
                                        "address": pr.get("address", ""),
                                        "ecommerce_report_number": pr.get("ecommerce_report_number", ""),
                                    }
                except Exception:
                    pass

            if collected_records:
                self.on_log(
                    f"  [진행 복원] 기확보 상품 {len(collected_records):,}건, 판매자 {len(vendor_cache):,}개사 결과 복원 완료."
                )
                for r in collected_records:
                    r_url = str(r.get("url") or "")
                    if r_url and r_url in self._emitted_urls:
                        continue
                    if r_url:
                        self._emitted_urls.add(r_url)
                    self.on_collected(r)
                summary.collected_items = len(collected_records)
                summary.unique_vendors = len(vendor_cache)
                summary.has_email = sum(1 for r in collected_records if r.get("email"))
                summary.has_business_number = sum(1 for r in collected_records if r.get("business_number"))
                summary.has_ceo_name = sum(1 for r in collected_records if r.get("ceo_name"))
                self.on_stats(summary)
            elif vendor_cache:
                self.on_log(f"  [시드 로드] 기존 검증된 판매자 {len(vendor_cache)}개사 정보 자동 탑재 완료.")

            # CSV 헤더 작성 및 복원 데이터 기록
            with open(csv_file, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=COUPANG_DATASET_FIELDS)
                writer.writeheader()
                for r in collected_records:
                    writer.writerow(r)

            def flush_json() -> None:
                """누적 결과 전체를 JSON 에 기록 — 주기 도달·중단·완료 시점 호출."""
                with open(json_file, "w", encoding="utf-8") as f:
                    json.dump(collected_records, f, ensure_ascii=False, indent=2)

            flush_json()
            records_since_flush = 0

            unprocessed = [itm for itm in product_targets if itm["id"] not in processed_items]
            if unprocessed and current_page is None:
                await open_new_session()

            consecutive_failures = 0
            failure_limit = PROXY_FAILURE_ABORT if self.config.use_proxy else CONSECUTIVE_BLOCK_ABORT
            dropped_count = 0
            active_pdp_attempt = None
            # 참고(2026-09-29 사용자 결정): 회선 소모 예산(교체 횟수 상한·대기)은
            # 폐지했다. 회선이 많이 나와도 세션 교체 스로틀(최소 간격)만 지키며
            # 수집을 계속 진행한다. 비용은 교체 건수가 아니라 트래픽(GB)으로
            # 과금되기 때문이다.

            for idx, itm in enumerate(product_targets, 1):
                self._checkpoint()
                self.on_progress("상세 수집", idx, total_items)

                p_id = itm["id"]
                p_url = itm["url"]
                p_title = itm["title"]

                # 이미 수집 완료된 상품은 고속 스킵
                if p_id in processed_items:
                    continue

                # rotation_batch_size 주기 회선 자동 순환 (프록시 사용 시에만)
                if self.config.use_proxy and req_count_in_session >= self.config.rotation_batch_size:
                    self.on_log(f"  [회선 자동 순환] {req_count_in_session}건 도달 -> 새 주거용 회선 세션 교체...")
                    await open_new_session()
                    req_count_in_session = 0

                record = None
                clean_url = f"https://ko.aliexpress.com/item/{p_id}.html"

                for attempt in range(1, PDP_ITEM_RETRIES + 1):
                    self._checkpoint()
                    pdp_attempt = object()
                    active_pdp_attempt = pdp_attempt
                    pdp_json_data = {}
                    pdp_received = False
                    api_response_seen = False
                    api_status: int | None = None
                    navigation_status: int | None = None
                    navigation_error: str | None = None
                    parse_issue: str | None = None
                    api_error_code: str | None = None

                    async def handle_response(response, _attempt_token=pdp_attempt):
                        nonlocal pdp_json_data, pdp_received, api_response_seen, api_status, parse_issue, api_error_code
                        if _attempt_token is not active_pdp_attempt:
                            return
                        if "mtop.aliexpress.pdp.pc.query" not in response.url:
                            return
                        api_response_seen = True
                        try:
                            api_status = int(response.status)
                        except (TypeError, ValueError):
                            api_status = None
                        if api_status != 200:
                            parse_issue = "http_status"
                            return
                        try:
                            b = await response.body()
                            if _attempt_token is not active_pdp_attempt:
                                return
                            txt = b.decode("utf-8", errors="ignore")
                            m = re.search(r'^[^{]*(\{.*\})[^}]*$', txt, re.DOTALL)
                            if not m:
                                parse_issue = "json_envelope_missing"
                                return
                            candidate_payload = json.loads(m.group(1))
                            if not isinstance(candidate_payload, dict):
                                parse_issue = "invalid_payload_shape"
                                return
                            api_error_code = _safe_mtop_error_code(candidate_payload)
                            if "PRODUCT_PROP_PC" not in txt and "SHOP_CARD_PC" not in txt:
                                parse_issue = "payload_fields_missing"
                                return
                            pdp_json_data = candidate_payload
                            pdp_received = True
                        except json.JSONDecodeError:
                            if _attempt_token is active_pdp_attempt:
                                parse_issue = "invalid_json"
                        except Exception as exc:  # noqa: BLE001 - response shape varies by edge
                            if _attempt_token is active_pdp_attempt:
                                parse_issue = type(exc).__name__

                    current_page.on("response", handle_response)
                    try:
                        navigation_response = await current_page.goto(
                            clean_url, wait_until="domcontentloaded", timeout=25000
                        )
                        try:
                            navigation_status = int(getattr(navigation_response, "status", None))
                        except (TypeError, ValueError):
                            navigation_status = None
                        # mtop is often sent shortly after DOMContentLoaded. Give it a
                        # little more time, but do not add a long fixed sleep to retries.
                        for _ in range(max(1, PDP_RESPONSE_WAIT_MS // 100)):
                            if pdp_received:
                                break
                            await current_page.wait_for_timeout(100)
                    except Exception as exc:  # noqa: BLE001 - classified below without body/URL
                        navigation_error = type(exc).__name__
                        await current_page.wait_for_timeout(500)

                    active_pdp_attempt = None
                    current_page.remove_listener("response", handle_response)
                    req_count_in_session += 1

                    if _is_blocked_url(current_page.url):
                        self.on_log(
                            f"  [{idx}/{total_items}] 보안 신호 감지 -> "
                            + ("새 회선 세션으로 자가 교체" if self.config.use_proxy else "안전 쿨다운 대기")
                            + f" (재시도 {attempt}/{PDP_ITEM_RETRIES})..."
                        )
                        if attempt < PDP_ITEM_RETRIES:
                            await block_backoff()
                            if self.config.use_proxy:
                                req_count_in_session = 0
                        else:
                            self.on_log("    마지막 시도라 추가 회선 교체 없이 이 상품을 보류합니다")
                        continue

                    if not pdp_received:
                        if navigation_error:
                            reason = f"페이지 요청 오류({navigation_error})"
                        elif navigation_status is not None and navigation_status >= 400:
                            reason = f"페이지 HTTP {navigation_status}"
                        elif api_response_seen and api_status != 200:
                            reason = f"mtop API HTTP {api_status or '알 수 없음'}"
                        elif parse_issue:
                            reason = f"mtop 응답 {parse_issue}"
                            if api_error_code:
                                reason += f" (ret {api_error_code})"
                        else:
                            reason = "mtop API 응답 없음"
                        self.on_log(
                            f"  [{idx}/{total_items}] 상품 데이터 미수신({reason}) — "
                            f"재시도 {attempt}/{PDP_ITEM_RETRIES}..."
                        )
                        if attempt < PDP_ITEM_RETRIES:
                            if self.config.use_proxy:
                                self.on_log("    수신 실패 — 같은 상품을 새 회선에서 즉시 재확인합니다")
                                await open_new_session()
                                req_count_in_session = 0
                            else:
                                await current_page.wait_for_timeout(
                                    int(self.config.block_cooldown_seconds * 1000)
                                )
                        continue

                    # ── 파싱 (상품 데이터를 받았을 때만 기록으로 이어진다) ──
                    result = pdp_json_data.get("data", {}).get("result", {})
                    shop_card = result.get("SHOP_CARD_PC", {})
                    product_props = result.get("PRODUCT_PROP_PC", {})
                    global_data = result.get("GLOBAL_DATA", {}).get("globalData", {})

                    store_name = shop_card.get("storeName") or ""
                    store_rating = ""
                    for b in shop_card.get("benefitInfoList", []):
                        if "좋아요" in b.get("title", "") or "positive" in b.get("title", "").lower():
                            store_rating = b.get("value", "")

                    vendor_id = str(global_data.get("sellerId") or "")
                    if not vendor_id and shop_card.get("licenseInfo"):
                        lm = re.search(r'storeNum=(\d+)', shop_card["licenseInfo"].get("actionTarget", ""))
                        if lm:
                            vendor_id = lm.group(1)
                    if not vendor_id:
                        vendor_id = p_id

                    # 캐시를 재사용하기 전에 현재 상품의 전화 속성을 먼저 본다.
                    # 이전 캐시에 빈칸/상세 참고가 있어도 뒤늦게 확인된 번호를 놓치지 않는다.
                    current_phone = _extract_phone_from_product_props(product_props)

                    # 캐시 적중 확인
                    if vendor_id in vendor_cache:
                        c_info = vendor_cache[vendor_id]
                        phone, merged_info = _merge_cached_phone(c_info, current_phone)
                        if merged_info is not None:
                            vendor_cache[vendor_id] = merged_info
                            store.record_seller(vendor_id, merged_info)
                            c_info = merged_info
                        record = {
                            "vendor_id": vendor_id,
                            "url": p_url,
                            "store_name": store_name or c_info.get("store_name", ""),
                            "company_name": c_info["company_name"],
                            "ceo_name": c_info["ceo_name"],
                            "business_number": c_info["business_number"],
                            "phone": phone,
                            "email": c_info["email"],
                            "address": c_info["address"],
                            "ecommerce_report_number": c_info["ecommerce_report_number"],
                            "power_seller": "TRUE" if itm.get("is_top_seller") else "FALSE",
                            "power_seller_title": "AliExpress TOP셀러" if itm.get("is_top_seller") else "",
                            "rating_count": itm.get("orders", ""),
                            "thumb_up_ratio": store_rating,
                            "product_title": p_title,
                            "price": itm.get("price", ""),
                            "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        }
                        break

                    # 7대 핵심 정보 파싱
                    company_name = ""
                    ceo_name = ""
                    business_number = ""
                    phone = current_phone
                    email = ""
                    address = ""
                    ecommerce_report_number = ""

                    showed_props = product_props.get("showedProps", [])
                    for prop in showed_props:
                        name = prop.get("attrName", "").strip()
                        val = prop.get("attrValue", "").strip()

                        if name in ("회사 이름", "회사명", "상호명", "상호", "제조업체", "제조자", "생산자"):
                            if val and not company_name and "참조" not in val:
                                company_name = val
                        elif name in ("대표자", "대표자명", "대표", "성명"):
                            ceo_name = val
                        elif name in ("사업자번호", "사업자등록번호"):
                            business_number = val
                        elif name in ("이메일 주소", "이메일", "E-mail", "Email"):
                            email = val
                        elif name in ("사업장소재지", "주소", "소재지"):
                            address = val
                        elif name in ("통신판매업신고번호", "통신판매업신고"):
                            ecommerce_report_number = val
                        elif name == "원산지" and not address:
                            address = val

                    props_map = product_props.get("showedPropsMap", {})
                    for k, v in props_map.items():
                        name = v.get("attrName", "").strip()
                        val = v.get("attrValue", "").strip()
                        if name == "이메일 주소" and not email: email = val
                        if name == "대표자" and not ceo_name: ceo_name = val
                        if name == "사업자번호" and not business_number: business_number = val
                        if name in ("회사 이름", "상호명") and not company_name and "참조" not in val: company_name = val
                        if name == "사업장소재지" and not address: address = val
                        if name == "통신판매업신고번호" and not ecommerce_report_number: ecommerce_report_number = val

                    if not company_name and not store_name:
                        company_name = "(상세 미기재)"

                    v_data = {
                        "store_name": store_name,
                        "company_name": company_name,
                        "ceo_name": ceo_name,
                        "business_number": business_number,
                        "phone": phone,
                        "email": email,
                        "address": address,
                        "ecommerce_report_number": ecommerce_report_number,
                    }
                    if company_name and company_name != "(상세 미기재)":
                        vendor_cache[vendor_id] = v_data
                    store.record_seller(vendor_id, v_data)

                    record = {
                        "vendor_id": vendor_id,
                        "url": p_url,
                        "store_name": store_name,
                        "company_name": company_name,
                        "ceo_name": ceo_name,
                        "business_number": business_number,
                        "phone": phone,
                        "email": email,
                        "address": address,
                        "ecommerce_report_number": ecommerce_report_number,
                        "power_seller": "TRUE" if itm.get("is_top_seller") else "FALSE",
                        "power_seller_title": "AliExpress TOP셀러" if itm.get("is_top_seller") else "",
                        "rating_count": itm.get("orders", ""),
                        "thumb_up_ratio": store_rating,
                        "product_title": p_title,
                        "price": itm.get("price", ""),
                        "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    }
                    break

                if record:
                    consecutive_failures = 0
                    collected_records.append(record)
                    processed_items.add(p_id)
                    store.record_item_result(p_id, record["vendor_id"], record)

                    # 실시간 CSV append + 주기적 JSON 전체 재작성
                    with open(csv_file, "a", encoding="utf-8-sig", newline="") as f:
                        writer = csv.DictWriter(f, fieldnames=COUPANG_DATASET_FIELDS)
                        writer.writerow(record)

                    records_since_flush += 1
                    if records_since_flush >= JSON_FLUSH_INTERVAL:
                        flush_json()
                        records_since_flush = 0

                    # 콜백 및 통계 — 증분 집계(매건 전체 재순회 금지)
                    if p_url not in self._emitted_urls:
                        self._emitted_urls.add(p_url)
                    self.on_collected(record)
                    if record.get("email"):
                        summary.has_email += 1
                    if record.get("business_number"):
                        summary.has_business_number += 1
                    if record.get("ceo_name"):
                        summary.has_ceo_name += 1
                    summary.collected_items = len(collected_records)
                    summary.unique_vendors = len(vendor_cache)
                    self.on_stats(summary)

                    biz_desc = f"[{record['company_name'] or record['store_name']}] 대표: {record['ceo_name'] or '-'} | 사업자: {record['business_number'] or '-'} | 이메일: {record['email'] or '-'}"
                    self.on_log(f"  [{idx}/{total_items}] 수집: {biz_desc} (누적 {len(collected_records)}건, 고유판매자 {len(vendor_cache)}개사)")
                else:
                    consecutive_failures += 1
                    # 회선·응답 문제는 상품 삭제로 확정할 수 없다. 실패 카운터를
                    # 누적하거나 포기 처리하지 않고, 진행 파일에는 상품을 남겨
                    # 다음 실행에서 다시 확인한다.
                    dropped_count += 1
                    self.on_log(
                        f"  [{idx}/{total_items}] 수집 실패(보류) — 다음 시작 때 "
                        f"이 상품부터 다시 시도됩니다. "
                        f"(연속 실패 {consecutive_failures}/{failure_limit})"
                    )
                    if consecutive_failures >= failure_limit:
                        summary.termination_reason = "blocked"
                        summary.error = RESPONSE_ABORT_GUIDE
                        self.on_log(
                            f"[응답 중단] 연속 {consecutive_failures}건 실패 — 안전 중단. "
                            f"{RESPONSE_ABORT_GUIDE}"
                        )
                        flush_json()
                        await close_session()
                        return summary

                await current_page.wait_for_timeout(int(self.config.delay * 1000))

            if dropped_count > 0:
                # 실패(데이터 미수신) 상품이 남으면 완료 봉인하지 않는다 — 다음
                # 시작 때 이 상품부터 다시 시도된다.
                summary.termination_reason = "blocked"
                summary.error = RESPONSE_ABORT_GUIDE
                self.on_log(
                    f"[응답 중단] 수집 못 건 상품 {dropped_count}건 — 완료 봉인하지 않고 종료. "
                    f"{RESPONSE_ABORT_GUIDE}"
                )
                flush_json()
                await close_session()
                return summary

            summary.termination_reason = "success"
            store.mark_finished()
            flush_json()
            await close_session()

        return summary
