"""판매자 전량(과일·냉장) 단계 — 상세 페이지 판매자정보 경로 회귀 시험.

실제 네트워크 없음. 2026-11 getStoreReview 사업자 키 누락 사고 뒤
patchright_full_sellers 가 상품 상세 페이지 결과를 저장하고, 한 건도
저장하지 못하면 완료 대신 중단하는지 확인한다.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from app.core.coupang.patchright_full_fruit import (
    PRODUCT_FIELDS,
    PRODUCT_SELLER_FIELDS,
    SELLER_FIELDS,
    _read_csv,
    _write_csv,
)
from app.core.coupang.patchright_full_sellers import run_seller_batch
from app.core.coupang.patchright_sample import (
    FETCH_STORE_REVIEW_JS,
    FETCH_VENDORS_JS,
)
from app.core.coupang.seller_detail import (
    EXPAND_SELLER_DETAIL_JS,
    EXTRACT_SELLER_DETAIL_JS,
)


class _Response:
    status = 200


class _Page:
    def __init__(self, *, rows=None, has_section=True, review_payload=None):
        self.url = "about:blank"
        self.rows = rows or {}
        self.has_section = has_section
        self.review_payload = review_payload or {}

    def goto(self, url, **_kwargs):
        self.url = str(url)
        return _Response()

    def wait_for_timeout(self, _milliseconds):
        return None

    def content(self):
        return "<html><body>normal page content</body></html>"

    def evaluate(self, script, argument=None):
        if script == EXTRACT_SELLER_DETAIL_JS:
            return {
                "rows": self.rows,
                "hasSection": self.has_section,
                "url": self.url,
                "title": "",
            }
        if script == EXPAND_SELLER_DETAIL_JS:
            return True
        if script == FETCH_STORE_REVIEW_JS:
            return {"status": 200, "body": json.dumps(self.review_payload)}
        if script == FETCH_VENDORS_JS:
            return {"status": 200, "body": json.dumps({"code": 200, "data": {"products": []}})}
        raise AssertionError("unexpected script")


class _Context:
    def __init__(self, page: _Page) -> None:
        self.pages = [page]


def _factory(page: _Page):
    @contextmanager
    def open_browser(_user_data_dir, *, headless=False, proxy=None):
        yield _Context(page)

    return open_browser


def _prepare(output_dir: Path) -> None:
    from app.core.coupang.patchright_full_fruit import FullFruitStore

    FullFruitStore(output_dir).ensure_files()
    _write_csv(
        output_dir / "fruit_products.csv",
        PRODUCT_FIELDS,
        [
            {
                "category_id": "1",
                "category_name": "과일",
                "page_number": "1",
                "product_id": "10",
                "item_id": "20",
                "vendor_item_id": "viid-1",
                "title": "상품",
                "price": "1000",
                "url": "https://www.coupang.com/vp/products/10?itemId=20&vendorItemId=viid-1",
                "collected_at": "2026-11-18 00:00:00",
            }
        ],
    )
    _write_csv(
        output_dir / "fruit_product_seller.csv",
        PRODUCT_SELLER_FIELDS,
        [
            {
                "product_id": "10",
                "item_id": "20",
                "vendor_item_id": "viid-1",
                "vendor_id": "A00001",
                "mapped_at": "2026-11-18 00:00:00",
            }
        ],
    )


class FullSellerDetailTest(unittest.TestCase):
    def test_saves_business_info_from_detail_page(self):
        page = _Page(
            rows={
                "company_ceo": "주식회사테스트 / 홍길동",
                "business_number": "123-45-67890",
                "phone": "02-1234-5678",
                "email": "test@example.com",
                "address": "서울특별시 강남구",
                "ecommerce_report_number": "2026-서울강남-0001",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            _prepare(output_dir)
            result = run_seller_batch(
                output_dir=output_dir,
                limit=10,
                state_root=root / "guard",
                browser_scope_factory=_factory(page),
            )
            self.assertEqual(result["event"], "seller_collection_complete")
            sellers = _read_csv(output_dir / "fruit_sellers.csv", SELLER_FIELDS)
            self.assertEqual(len(sellers), 1)
            self.assertEqual(sellers[0]["status"], "saved")
            self.assertEqual(sellers[0]["company_name"], "주식회사테스트")
            self.assertEqual(sellers[0]["ceo_name"], "홍길동")

    def test_missing_section_and_no_review_fields_halts(self):
        page = _Page(has_section=False, review_payload={})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            _prepare(output_dir)
            result = run_seller_batch(
                output_dir=output_dir,
                limit=10,
                state_root=root / "guard",
                browser_scope_factory=_factory(page),
            )
            self.assertEqual(result["event"], "failed")
            control = json.loads(
                (output_dir / "fruit_seller_control.json").read_text("utf-8")
            )
            self.assertEqual(control["status"], "halted")
            self.assertIn("한 건도 저장하지 못했습니다", str(control["reason"]))


if __name__ == "__main__":
    unittest.main()
