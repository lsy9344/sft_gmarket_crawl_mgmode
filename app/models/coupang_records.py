"""Coupang 데이터 계약: RunConfig, Record, RunSummary (WORK_ORDER §6)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_WINDOWS_FORBIDDEN_CHARS = set('<>:"/\\|?*')


@dataclass
class CoupangRunConfig:
    output_dir: Path
    output_prefix: str | None = None
    max_scroll_pages: int = 10
    batch_size: int = 10
    warmup_time: float = 20.0
    # 판매자정보(getStoreReview) 요청 간격 — IP 평판 게이트 엔드포인트(BRIGHTDATA_
    # AKAMAI_REVIEW §9). 1,794건 무차단 실측값 3초 중심으로 정렬했다(2026-09-09
    # 검토: 기존 1.0~2.5초는 검증치의 절반 수준으로 403 노출이 컸다).
    delay_min: float = 2.5
    delay_max: float = 3.5
    # 영속 브라우저 프로필 — 쿠키·방문 이력을 실행 간 누적해 세션 신뢰를 축적한다
    # (SEARCH_POC_FINDINGS 가설 A). False 면 기존의 매 실행 신규 세션 방식.
    use_persistent_profile: bool = True
    profile_dir: Path | None = None  # None → <프로젝트 루트>/runtime_profile
    # 프록시 (playwright/Camoufox proxy dict) — None 이면 직접 접속.
    # Coupang 카테고리 탭은 Decodo 스티키 세션을 주입한다. 1차 목록은 프록시 IP,
    # 2차 판매자정보는 회선 IP 세션으로 엔진이 전환한다.
    proxy: dict | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.use_persistent_profile, bool):
            # TRY004 무시 — 설정 검증은 이 파일 전체가 ValueError 관례
            raise ValueError("use_persistent_profile must be a bool")  # noqa: TRY004
        if self.proxy is not None:
            if not isinstance(self.proxy, dict):
                raise ValueError("proxy must be a dict or None")
            for key in ("server", "username", "password"):
                value = self.proxy.get(key)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"proxy must include a non-empty '{key}'")
        if self.profile_dir is not None:
            self.profile_dir = Path(self.profile_dir)
        if self.max_scroll_pages < 1:
            raise ValueError("max_scroll_pages must be >= 1")
        if self.batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if not math.isfinite(self.warmup_time) or self.warmup_time < 0:
            raise ValueError("warmup_time must be >= 0")
        if not math.isfinite(self.delay_min) or self.delay_min < 0:
            raise ValueError("delay_min must be >= 0")
        if not math.isfinite(self.delay_max) or self.delay_max < self.delay_min:
            raise ValueError("delay_max must be >= delay_min")
        if self.output_prefix is not None:
            if not self.output_prefix.strip():
                self.output_prefix = None
            else:
                prefix = self.output_prefix
                if any(ch in _WINDOWS_FORBIDDEN_CHARS or ord(ch) < 32 for ch in prefix):
                    raise ValueError("output_prefix contains Windows-forbidden characters")
                if prefix.endswith((".", " ")):
                    raise ValueError("output_prefix must not end with a dot or space")
                stem = prefix.split(".", 1)[0].upper()
                if stem in _WINDOWS_RESERVED_NAMES:
                    raise ValueError("output_prefix uses a reserved Windows device name")


RECORD_FIELDS = (
    "vendor_id",
    "url",
    "store_name",
    "company_name",
    "ceo_name",
    "business_number",
    "phone",
    "email",
    "address",
    "ecommerce_report_number",
    "power_seller",
    "power_seller_title",
    "rating_count",
    "thumb_up_ratio",
)


@dataclass
class CoupangRecord:
    vendor_id: str
    url: str = ""
    store_name: str = ""
    company_name: str = ""
    ceo_name: str = ""
    business_number: str = ""
    phone: str = ""
    email: str = ""
    address: str = ""
    ecommerce_report_number: str = ""
    power_seller: bool = False
    power_seller_title: str = ""
    rating_count: int = 0
    thumb_up_ratio: float = 0.0

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in RECORD_FIELDS}


@dataclass
class CoupangRunSummary:
    products_seen: int = 0
    unique_vendors: int = 0
    business_info_success: int = 0
    brand_seller_skipped: int = 0
    request_errors: int = 0
    power_sellers: int = 0
    store_name_present: int = 0
    store_name_missing: int = 0
    duplicate_business_numbers_observed: int = 0
    records: list = field(default_factory=list)
    json_path: str | None = None
    csv_path: str | None = None
    cancelled: bool = False
    error: str | None = None
    save_error: str | None = None
    cleanup_error: str | None = None
    termination_reason: str = ""
    # 회선 IP(판매자 단계) 차단 — Decodo 회선 교체 재시도 대상이 아님(§3.3)
    blocked_direct: bool = False

    @property
    def store_name_coverage(self) -> float:
        if self.business_info_success == 0:
            return 0.0
        return self.store_name_present / self.business_info_success


@dataclass
class CoupangProbeSummary:
    """count-only probe 결과 — 사업자 API 미호출, 저장 없음 (CRAWL_RESULTS §4.3)."""

    products_seen: int = 0
    pages_fetched: int = 0
    termination_reason: str = ""
    cancelled: bool = False
    error: str | None = None
    cleanup_error: str | None = None


@dataclass(frozen=True)
class CoupangStatsSnapshot:
    """불변 stats DTO — UI 신호용. records/path/error 제외."""

    products_seen: int = 0
    unique_vendors: int = 0
    business_info_success: int = 0
    brand_seller_skipped: int = 0
    request_errors: int = 0
    power_sellers: int = 0
    store_name_present: int = 0
    store_name_missing: int = 0
    duplicate_business_numbers_observed: int = 0

    @classmethod
    def from_summary(cls, s: CoupangRunSummary) -> CoupangStatsSnapshot:
        return cls(
            products_seen=s.products_seen,
            unique_vendors=s.unique_vendors,
            business_info_success=s.business_info_success,
            brand_seller_skipped=s.brand_seller_skipped,
            request_errors=s.request_errors,
            power_sellers=s.power_sellers,
            store_name_present=s.store_name_present,
            store_name_missing=s.store_name_missing,
            duplicate_business_numbers_observed=s.duplicate_business_numbers_observed,
        )
