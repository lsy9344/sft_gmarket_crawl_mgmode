"""Foodspring 데이터 계약: RunConfig, Record, RunSummary."""

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
class FoodSpringRunConfig:
    output_dir: Path
    output_prefix: str | None = None
    max_pages: int = 200          # 상품 목록 GraphQL 페이지 수 (80개/페이지)
    workers: int = 3              # 셀러 상세 페이지 동시 fetch 수
    delay_min: float = 0.8
    delay_max: float = 2.0
    limit_products: int | None = None  # 테스트용 상품 상한 (None=전체)

    def __post_init__(self) -> None:
        if self.max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        if self.workers < 1:
            raise ValueError("workers must be >= 1")
        if not math.isfinite(self.delay_min) or self.delay_min < 0:
            raise ValueError("delay_min must be >= 0")
        if not math.isfinite(self.delay_max) or self.delay_max < self.delay_min:
            raise ValueError("delay_max must be >= delay_min")
        if self.limit_products is not None and self.limit_products < 1:
            raise ValueError("limit_products must be >= 1")
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


SELLER_RECORD_FIELDS = (
    "seller_id",
    "url",
    "store_name",
    "owner_name",
    "business_number",
    "phone",
    "email",
    "address",
    "ecommerce_report_number",
    "customer_service_number",
    "product_count",
)


@dataclass
class FoodSpringSellerRecord:
    seller_id: str
    url: str = ""
    store_name: str = ""
    owner_name: str = ""
    business_number: str = ""
    phone: str = ""
    email: str = ""
    address: str = ""
    ecommerce_report_number: str = ""
    customer_service_number: str = ""
    product_count: int = 0

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in SELLER_RECORD_FIELDS}


@dataclass
class FoodSpringRunSummary:
    products_seen: int = 0
    unique_products: int = 0
    unique_vendors: int = 0
    business_info_success: int = 0
    email_success: int = 0
    request_errors: int = 0
    missing_info_sellers: int = 0
    records: list = field(default_factory=list)
    xlsx_path: str | None = None
    cancelled: bool = False
    error: str | None = None
    save_error: str | None = None
    termination_reason: str = ""

    @property
    def business_info_coverage(self) -> float:
        if self.unique_vendors == 0:
            return 0.0
        return self.business_info_success / self.unique_vendors