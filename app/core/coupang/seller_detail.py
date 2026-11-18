"""상품 상세 페이지의 판매자 사업자정보 추출 (2026-11 응답 변경 대응).

2026-11부터 쿠팡 ``getStoreReview`` 응답에서 ``name``·``businessNumber`` 등
사업자정보 8개 키가 빠지고, 같은 정보가 상품 상세 페이지
(``/vp/products/...``)의 화면 "판매자 정보" 표와 Next.js RSC 데이터
(``sellerDetailInfo``)에 남았다. 이 모듈은 저장된 상품-판매자 연결의 대표
상품 상세 페이지를 한 번 열어 그 표를 읽고, 기존 ``getStoreReview`` 응답과
같은 모양의 dict로 돌려준다.

브라우저 탐색·차단 판정은 ``patchright_sample`` 의 공용 헬퍼를 그대로 쓴다.
값을 찾지 못한 경우를 두 가지로 구분하는 것이 핵심이다:

- ``no_public_info``: 페이지에 "판매자 정보" 영역이 있는데 값이 비어 있음.
- ``failed``: 페이지를 열지 못했거나 영역 자체가 없음(구조 변경·차단 의심).

기존 코드는 두 경우를 모두 "정보 없음"으로 확정해 재조회 대상에서 제외했다.
그 판정을 다시 만들지 않기 위해 상태를 호출자에게 그대로 넘긴다.
"""

from __future__ import annotations

import json
from contextlib import suppress
from typing import Any

from app.core.coupang.patchright_sample import (
    FETCH_STORE_REVIEW_JS,
    _api_block,
    _build_record,
    _decode_api,
    _navigate,
    _wait,
)

__all__ = [
    "DETAIL_RETRY_LIMIT",
    "DETAIL_RETRY_WAIT_MS",
    "DETAIL_SETTLE_MS",
    "EXPAND_SELLER_DETAIL_JS",
    "EXTRACT_SELLER_DETAIL_JS",
    "fetch_seller_detail",
    "has_seller_detail",
    "seller_detail_payload",
]

DETAIL_SETTLE_MS = 2_000
DETAIL_RETRY_WAIT_MS = 700
DETAIL_RETRY_LIMIT = 3

# 화면 라벨 → 내부 키. 순서가 중요하다 — "상호/대표자"를 "상호"보다 먼저
# 매칭해야 결합 라벨이 한 칸으로 들어온다.
_LABELS = (
    ("상호/대표자", "company_ceo"),
    ("상호 / 대표자", "company_ceo"),
    ("대표자", "ceo"),
    ("상호", "company"),
    ("사업장 소재지", "address"),
    ("사업장소재지", "address"),
    ("e-mail", "email"),
    ("이메일", "email"),
    ("연락처", "phone"),
    ("통신판매업 신고번호", "ecommerce_report_number"),
    ("통신판매업신고번호", "ecommerce_report_number"),
    ("사업자번호", "business_number"),
    ("사업자등록번호", "business_number"),
)

_LABELS_JSON = json.dumps(_LABELS, ensure_ascii=False)

# 상세 페이지 DOM에서 라벨(th/dt)과 값(다음 형제)을 읽는다. 화면 표가
# 서버 렌더링되므로 스크롤·탭 클릭 없이도 값이 잡히지만, 호출자가 필요하면
# EXPAND_SELLER_DETAIL_JS로 한 번 더 시도한다.
EXTRACT_SELLER_DETAIL_JS = r"""
() => {
  const labels = __LABELS__;
  const placeholders = new Set(["-", "없음", "미제공", "N/A", "n/a"]);
  const rows = {};
  let matchedLabels = 0;
  const put = (label, value) => {
    label = (label || "").replace(/\s+/g, " ").trim();
    value = (value || "").replace(/\s+/g, " ").trim();
    if (!label) return;
    for (const [needle, key] of labels) {
      if (!label.includes(needle)) continue;
      matchedLabels += 1;
      if (value && !placeholders.has(value) && !rows[key]) {
        rows[key] = value;
      }
      return;
    }
  };
  for (const node of document.querySelectorAll("th, dt")) {
    if (node.nextElementSibling) {
      put(node.innerText, node.nextElementSibling.innerText);
    }
  }
  return {
    rows,
    hasSection: matchedLabels > 0,
    url: location.href,
    title: document.title,
  };
}
""".replace("__LABELS__", _LABELS_JSON)

EXPAND_SELLER_DETAIL_JS = r"""
() => {
  for (let index = 0; index < 12; index += 1) {
    window.scrollBy(0, 1200);
  }
  const candidates = Array.from(
    document.querySelectorAll("button, a, div, span")
  );
  const target = candidates.find(
    (element) => (element.innerText || "").trim() === "판매자 정보"
  );
  if (target) {
    target.click();
    return true;
  }
  return false;
}
"""


def _text(value: Any) -> str:
    return str(value or "").strip()


def seller_detail_payload(payload: object) -> dict:
    """추출 JS 결과를 ``getStoreReview`` 응답과 같은 필드명으로 바꾼다."""
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, dict):
        rows = {}
    combined = _text(rows.get("company_ceo"))
    company = _text(rows.get("company"))
    ceo = _text(rows.get("ceo"))
    if combined:
        parts = [part.strip() for part in combined.split("/", 1)]
        company = parts[0]
        if len(parts) > 1:
            ceo = parts[1]
    return {
        "name": company,
        "repPersonName": ceo,
        "businessNumber": _text(rows.get("business_number")),
        "repPhoneNum": _text(rows.get("phone")),
        "repEmail": _text(rows.get("email")),
        "repAddr1": _text(rows.get("address")),
        "repAddr2": "",
        "eCommerceReportNumber": _text(rows.get("ecommerce_report_number")),
    }


# 상세 페이지에서 읽는 사업자정보 필드 — getStoreReview 보조 응답으로도
# 빈 칸만 채운다(상세 페이지 값이 우선). `repAddr2` 는 주소 보조 줄이라
# 단독으로는 "사업자정보 있음"으로 보지 않는다.
_DETAIL_FIELDS = (
    "name",
    "repPersonName",
    "businessNumber",
    "repPhoneNum",
    "repEmail",
    "repAddr1",
    "repAddr2",
    "eCommerceReportNumber",
)
_REQUIRED_DETAIL_FIELDS = tuple(
    field for field in _DETAIL_FIELDS if field != "repAddr2"
)


def has_seller_detail(data: dict | None) -> bool:
    """사업자정보로 인정할 값이 하나라도 있는지."""
    if not isinstance(data, dict):
        return False
    return any(
        _text(data.get(field)) for field in _REQUIRED_DETAIL_FIELDS
    )


def _merge_review(data: dict | None, review: dict) -> dict:
    """상세 페이지 결과에 스토어 API의 배지·평점과 빈 사업자 필드를 보탠다."""
    merged = dict(data or {})
    for field in _DETAIL_FIELDS:
        if not _text(merged.get(field)) and _text(review.get(field)):
            merged[field] = review[field]
    for field in ("qualitySellerBadgeDto", "ratingCount", "thumbUpRatio"):
        if review.get(field) is not None:
            merged[field] = review[field]
    return merged


def _result(
    *,
    data: dict | None,
    status: str,
    error: str = "",
    blocked: bool = False,
    reference: str = "",
) -> dict:
    return {
        "data": data,
        "status": status,
        "error": error,
        "blocked": blocked,
        "reference": reference,
    }


def fetch_seller_detail(
    page,
    vendor_id: str,
    vendor: dict,
    control,
    state: dict,
    review_page=None,
) -> dict:
    """판매자 대표 상품 상세 페이지를 열어 사업자정보를 읽는다.

    반환 ``status`` 는 ``saved``/``no_public_info``/``failed`` 중 하나다.
    ``blocked`` 가 True 면 호출자는 기존 차단 경로(기록 후 중단)를 따라야
    한다. 페이지 탐색 자체가 실패한 경우는 예외를 올리지 않고 ``failed`` 로
    돌려 한 판매자 때문에 세션 전체를 잃지 않게 한다.

    ``review_page`` 는 보조 getStoreReview 요청을 보낼 페이지다. 상세 페이지는
    www.coupang.com 으로 이동하므로, 호출자가 shop.coupang.com 에 머무는
    별도 탭을 주면 보조 요청이 같은 출처에서 나간다(없으면 상세 페이지에서
    시도하고 실패해도 무시한다).
    """
    review_target = review_page if review_page is not None else page
    url = _text(_build_record(vendor_id, {}, vendor).get("url"))
    if not url:
        return _result(
            data=None,
            status="failed",
            error="상품 URL이 없어 판매자 상세 페이지를 열지 못했습니다.",
        )
    try:
        blocked, http_status, reference = _navigate(
            page, url, DETAIL_SETTLE_MS, control, state
        )
    except Exception as error:  # noqa: BLE001 - 개별 상품 실패는 다음 판매자로
        return _result(
            data=None,
            status="failed",
            error=(
                f"상품 상세 페이지 접속 실패: "
                f"{type(error).__name__}: {error}"
            ),
        )
    if blocked:
        return _result(
            data=None,
            status="failed",
            blocked=True,
            reference=reference,
        )

    payload: object = {}
    data: dict | None = None
    last_error = ""
    last_attempt_ok = False
    for attempt in range(DETAIL_RETRY_LIMIT + 1):
        try:
            payload = page.evaluate(EXTRACT_SELLER_DETAIL_JS)
            data = seller_detail_payload(payload)
            last_attempt_ok = True
            if has_seller_detail(data):
                break
            if attempt == 0:
                with suppress(Exception):
                    # 펼치기 실패는 다음 추출 재시도에서 회복한다.
                    page.evaluate(EXPAND_SELLER_DETAIL_JS)
        except Exception as error:  # noqa: BLE001 - 문서 재로딩 경합은 재시도
            last_attempt_ok = False
            last_error = (
                f"상품 상세 페이지 판매자 정보 추출 실패: "
                f"{type(error).__name__}: {error}"
            )
            payload = {}
            data = None
        if attempt < DETAIL_RETRY_LIMIT:
            _wait(page, DETAIL_RETRY_WAIT_MS, control)

    # 상세 페이지가 새 주 출처이고, getStoreReview는 배지·평점과 (혹시 남아
    # 있는) 빈 사업자 필드만 보탠다. 보조 요청이 차단·오류여도 상세 페이지
    # 결과를 버리지 않는다. 한 세션에서 보조 API가 차단되면 이후 호출은
    # 건너뛰어 같은 차단을 반복하지 않는다.
    review_data: dict | None = None
    if not state.get("review_api_blocked"):
        state["api_calls"] = state.get("api_calls", 0) + 1
        try:
            review_result = review_target.evaluate(FETCH_STORE_REVIEW_JS, vendor_id)
            review_blocked, _review_reference = _api_block(review_result)
            if review_blocked:
                state["review_api_blocked"] = True
            else:
                review_data, _review_error = _decode_api(
                    review_result, f"판매자 {vendor_id} 보조 정보"
                )
        except Exception:  # noqa: BLE001 - 보조 요청 실패는 무시
            review_data = None
    if review_data is not None:
        data = _merge_review(data, review_data)

    if has_seller_detail(data):
        return _result(data=data, status="saved")
    if not last_attempt_ok and last_error:
        return _result(data=None, status="failed", error=last_error)
    has_section = isinstance(payload, dict) and bool(payload.get("hasSection"))
    if has_section:
        return _result(data=data, status="no_public_info")
    return _result(
        data=None,
        status="failed",
        error=(
            "상품 상세 페이지에서 판매자 정보 영역을 찾지 못했습니다"
            f" (HTTP {http_status})."
        ),
    )
