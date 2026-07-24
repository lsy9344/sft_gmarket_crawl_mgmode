"""데이터 모델 정의 (Phase 0 조사 결과, 판매자 레코드).

WORK_ORDER.md §3.4 / §5.4 스키마 기준.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

# Pre-scan 상태 상수
STATUS_COLLECTABLE = "collectable"
STATUS_COMPLETED = "completed"
STATUS_EMPTY = "empty"
# 리스팅 접근이 네트워크 오류/403(Cloudflare)/봇 감지로 끝내 실패한 경우.
# 상품이 실제로 0개인 STATUS_EMPTY 와 구분해야 사용자가 "차단되어 조사 실패"를
# "이 카테고리는 원래 상품이 없음"으로 오인하지 않는다.
STATUS_BLOCKED = "blocked"

# 카테고리 출처 상수
SOURCE_BEST = "best"
SOURCE_SUPERDEAL = "superdeal"


@dataclass
class PrescanResult:
    """Phase 0 사전 조사 1개 카테고리 결과 (WORK_ORDER §3.4)."""

    category_name: str            # 카테고리명
    source: str                   # "best" | "superdeal"
    total_codes: int              # 리스팅에서 추출된 전체 goodscode 수
    new_codes: int                # collected_ids 제외 후 신규 건수
    already_collected: int        # 이미 수집한 건수
    codes: list[str] = field(default_factory=list)  # 추출된 goodscode 목록 (Phase 1 재사용)
    status: str = STATUS_COLLECTABLE                # "collectable" | "completed" | "empty"

    @property
    def status_label(self) -> str:
        """UI 표기용 한글 상태 라벨."""
        return {
            STATUS_COLLECTABLE: "수집 가능",
            STATUS_COMPLETED: "완료됨",
            STATUS_EMPTY: "상품 없음",
            STATUS_BLOCKED: "차단/오류",
        }.get(self.status, self.status)

    def to_dict(self) -> dict:
        return asdict(self)


# 성공 판정 시 '내용 필드'로 간주하지 않는 키 (메타/보조 필드)
_META_FIELDS = frozenset({"goodscode", "url", "collected_at", "source"})
# 성공 판정 대상 5개 필드 (WORK_ORDER §5.5: 아래 중 1개 이상 비어있지 않으면 성공)
SUCCESS_FIELDS = ("store_name", "company_name", "email", "phone", "business_number")
# 로그 (n/6) 표기용 핵심 내용 필드 6개
CORE_CONTENT_FIELDS = ("store_name", "company_name", "email", "phone", "business_number", "address")


@dataclass
class SellerRecord:
    """판매자 정보 1건 (WORK_ORDER §5.4).

    ceo_name 은 mg.gmarket.co.kr 에 없음 → 항상 빈 문자열 유지(§15).
    """

    goodscode: str
    url: str
    store_name: str = ""
    company_name: str = ""
    ceo_name: str = ""
    email: str = ""
    phone: str = ""
    business_number: str = ""
    address: str = ""
    collected_at: str = ""
    source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def is_valid(self) -> bool:
        """store_name/company_name/email/phone/business_number 중 1개 이상 채워지면 성공 (§5.5)."""
        return any(getattr(self, f) for f in SUCCESS_FIELDS)

    def filled_count(self) -> int:
        """채워진 핵심 내용 필드 수 (로그의 (n/6) 표기용, 최대 6)."""
        return sum(1 for f in CORE_CONTENT_FIELDS if getattr(self, f))


# CSV/JSON 출력 시 필드 순서 (WORK_ORDER §5.4 스키마 순서 고정)
RECORD_FIELDS = [
    "goodscode",
    "url",
    "store_name",
    "company_name",
    "ceo_name",
    "email",
    "phone",
    "business_number",
    "address",
    "collected_at",
    "source",
]
