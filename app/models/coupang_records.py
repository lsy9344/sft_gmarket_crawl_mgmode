"""Coupang 데이터 계약: RunConfig, Record, RunSummary (WORK_ORDER §6)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CoupangRunConfig:
    output_dir: Path
    output_prefix: str | None = None
    max_scroll_pages: int = 10
    batch_size: int = 10
    warmup_time: float = 20.0
    delay_min: float = 1.0
    delay_max: float = 2.5

    def __post_init__(self) -> None:
        if self.max_scroll_pages < 1:
            raise ValueError("max_scroll_pages must be >= 1")
        if self.batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if self.warmup_time < 0:
            raise ValueError("warmup_time must be >= 0")
        if self.delay_min < 0:
            raise ValueError("delay_min must be >= 0")
        if self.delay_max < self.delay_min:
            raise ValueError("delay_max must be >= delay_min")
        if self.output_prefix is not None:
            if not self.output_prefix.strip():
                self.output_prefix = None
            elif re.search(r"[/\\]", self.output_prefix):
                raise ValueError("output_prefix must not contain path separators")


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
