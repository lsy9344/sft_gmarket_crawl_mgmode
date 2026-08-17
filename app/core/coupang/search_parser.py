"""검색 결과 페이지 파서 — 차단 감지 + DOM 추출 스크립트 + href 파싱.

쿠팡 신형 SRP(React) 구조 실측 결과(docs/coupang/SEARCH_POC_FINDINGS.md rev.3):
- 메인 결과 목록(~60개)은 DOM 카드로만 렌더링됨 (내장 JSON 아님)
- 카드 href 에 legacyProductId/itemId/vendorItemId 포함
- 로켓 배지(`rds/logo*` img src)는 SSR 에 항상 포함 → 로켓 판별 신뢰 가능
- 페이지네이션 없음 (`disableFixedPagination:true`), 정렬(sorter)만 유효

브라우저 측은 DOM_EXTRACTION_JS 를 page.evaluate 로 실행하고,
결과 dict 목록을 parse_extracted() 로 정규화한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

_SOFT_BLOCK_MAX_BYTES = 1500
BLOCK_KEYWORDS = (
    "자동화된 테스트 소프트웨어",
    "접근이 제한",
    "비정상적인 접근",
    "보안 절차",
    "확인 절차",
    "Access Denied",
    "captcha",
    "사용권한",
)

_HREF_RE = re.compile(r"/vp/products/(\d+)")
_PRICE_RE = re.compile(r"([\d,]+)\s*원")
_PRICE_NUM_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d{4,})")

# 브라우저에서 실행되는 추출 스크립트 — 왕복 최소화 위해 1회 evaluate 로 전량 수집.
# 클래스명이 해시되어 있으므로 부분 매칭(*=) 사용.
# 로켓 판별: rds 로고(로켓배송) + falcon rocket 배지(로켓그로스/판매자로켓) — PLP 실측 근거
DOM_EXTRACTION_JS = """
() => {
  const out = [];
  const cards = document.querySelectorAll('li[class*="ProductUnit_productUnit"]');
  for (const li of cards) {
    const a = li.querySelector('a[href*="/vp/products/"]');
    if (!a) continue;
    const titleEl = li.querySelector('[class*="productName"]');
    const priceArea = li.querySelector('[class*="PriceArea"]');
    const rocket = !!li.querySelector(
      'img[src*="rds/logo"], img[src*="rds/delivery_badge"], '
      + 'img[src*="badges/falcon"][src*="rocket"]'
    );
    const sponsored = !!li.querySelector('[aria-label="Ad information"]');
    out.push({
      href: a.getAttribute('href') || '',
      title: titleEl ? titleEl.innerText.trim() : '',
      priceText: priceArea ? priceArea.innerText.trim().slice(0, 80) : '',
      rocket,
      sponsored,
    });
  }
  return out;
}
"""

# 쿠팡이 검색 페이지 RSC 페이로드에 내장하는 공식 가격 밴드 정의
_PRICE_BAND_RE = re.compile(
    r'\{"id":"[\d-]+","text":"[^"]+","minPrice":(\d+),"maxPrice":(\d+)\}')


@dataclass
class SearchProduct:
    """검색 결과에서 추출한 상품 1건."""

    item_id: str
    legacy_product_id: str = ""
    vendor_item_id: str = ""
    vendor_id: str = ""
    title: str = ""
    price: int = 0
    rocket: bool = False
    sponsored: bool = False
    url: str = ""

    @property
    def dedup_key(self) -> str:
        return self.vendor_item_id or self.item_id


def is_blocked(html: str) -> tuple[bool, str]:
    """차단 페이지 감지 — 키워드 매칭 + 소프트 블록(비정상 작은 응답)."""
    if len(html.encode("utf-8", errors="ignore")) < _SOFT_BLOCK_MAX_BYTES:
        return True, f"소프트 블록 ({len(html)}자)"
    low = html.lower()
    for kw in BLOCK_KEYWORDS:
        if kw.lower() in low:
            return True, f"차단 키워드: {kw}"
    return False, ""


def parse_href(href: str) -> tuple[str, str, str]:
    """상품 href → (legacy_product_id, item_id, vendor_item_id)."""
    m = _HREF_RE.search(href)
    legacy = m.group(1) if m else ""
    try:
        parsed = urlparse(href if href.startswith("http") else f"https://www.coupang.com{href}")
        qs = parse_qs(parsed.query)
        item_id = qs.get("itemId", [""])[0]
        vendor_item_id = qs.get("vendorItemId", [""])[0]
    except (ValueError, IndexError):
        item_id = vendor_item_id = ""
    return legacy, item_id, vendor_item_id


def parse_price(text: str) -> int:
    """'9,920원' 류 텍스트 → 원 단위 정수."""
    m = _PRICE_RE.search(text or "") or _PRICE_NUM_RE.search(text or "")
    if not m:
        return 0
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return 0


def parse_price_bands(html: str) -> list[tuple[int, int]]:
    """검색 페이지 HTML 에서 공식 가격 밴드 (min, max) 목록 추출.

    실측 근거(rev.11): 쿠팡은 키워드마다 가격 밴드 정의를 RSC 페이로드에 내장하며
    밴드 목록은 키워드/가격 분포에 따라 달라진다. 중복 제거 후 순서 보존.
    """
    bands: list[tuple[int, int]] = []
    for m in _PRICE_BAND_RE.finditer(html):
        try:
            lo, hi = int(m.group(1)), int(m.group(2))
        except ValueError:
            continue
        if (lo, hi) not in bands:
            bands.append((lo, hi))
    return bands


def parse_extracted(rows: list[dict]) -> list[SearchProduct]:
    """DOM_EXTRACTION_JS 결과 → SearchProduct 목록 (viid 기준 중복 제거, 순서 보존)."""
    items: dict[str, SearchProduct] = {}
    for row in rows:
        try:
            href = row.get("href", "")
            legacy, item_id, viid = parse_href(href)
            if not item_id and not legacy:
                continue
            product = SearchProduct(
                item_id=item_id or legacy,
                legacy_product_id=legacy,
                vendor_item_id=viid,
                title=(row.get("title") or "").strip()[:200],
                price=parse_price(row.get("priceText", "")),
                rocket=bool(row.get("rocket")),
                sponsored=bool(row.get("sponsored")),
                url=(f"https://www.coupang.com/vp/products/{legacy}"
                     f"?itemId={item_id}&vendorItemId={viid}" if legacy and viid else ""),
            )
            items.setdefault(product.dedup_key, product)
        except Exception:  # noqa: BLE001 - 개별 카드 파싱 실패는 건너뜀
            continue
    return list(items.values())
