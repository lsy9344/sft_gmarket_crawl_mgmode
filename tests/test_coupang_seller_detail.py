"""상세 페이지 판매자정보 추출 상태 판정 — 실제 네트워크 없음."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from app.core.coupang.patchright_sample import FETCH_STORE_REVIEW_JS
from app.core.coupang.seller_detail import (
    EXPAND_SELLER_DETAIL_JS,
    EXTRACT_SELLER_DETAIL_JS,
    fetch_seller_detail,
)

_VENDOR = {
    "vendorId": "A00001",
    "displayName": "테스트스토어",
    "productId": 10,
    "itemId": 20,
    "vendorItemId": 30,
}


class _Page:
    def __init__(self, *, rows=None, has_section=True, review=None,
                 status=200, content="<html><body>normal</body></html>"):
        self.url = "about:blank"
        self.rows = rows or {}
        self.has_section = has_section
        self.review = review or {}
        self.status = status
        self._content = content

    def goto(self, url, **_kwargs):
        self.url = str(url)
        return SimpleNamespace(status=self.status)

    def wait_for_timeout(self, _milliseconds):
        return None

    def content(self):
        return self._content

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
            return {"status": 200, "body": json.dumps(self.review)}
        raise AssertionError("unexpected script")


def _fetch(page: _Page) -> dict:
    return fetch_seller_detail(
        page, "A00001", _VENDOR, None, {"document_navigations": 0, "api_calls": 0}
    )


class SellerDetailStatusTest(unittest.TestCase):
    def test_detail_rows_are_saved(self):
        result = _fetch(
            _Page(
                rows={
                    "company_ceo": "주식회사테스트 / 홍길동",
                    "business_number": "123-45-67890",
                }
            )
        )
        self.assertEqual(result["status"], "saved")
        self.assertEqual(result["data"]["name"], "주식회사테스트")

    def test_empty_section_is_no_public_info(self):
        result = _fetch(_Page(has_section=True, review={}))
        self.assertEqual(result["status"], "no_public_info")

    def test_missing_section_is_failed(self):
        result = _fetch(_Page(has_section=False, review={}))
        self.assertEqual(result["status"], "failed")
        self.assertIn("영역", result["error"])

    def test_review_fields_are_a_fallback(self):
        result = _fetch(
            _Page(
                has_section=False,
                review={
                    "name": "주식회사테스트",
                    "businessNumber": "123-45-67890",
                },
            )
        )
        self.assertEqual(result["status"], "saved")
        self.assertEqual(result["data"]["name"], "주식회사테스트")

    def test_detail_vendor_name_is_not_used_as_store_name(self):
        """``sellerDetailInfo.vendorName`` 은 상호이지 스토어명이 아니다."""
        result = _fetch(
            _Page(
                rows={"company_ceo": "주식회사테스트 / 홍길동"},
                review={
                    "vendorName": "주식회사테스트",
                    "storeName": "테스트스토어",
                },
            )
        )
        self.assertEqual(result["status"], "saved")
        self.assertNotIn("storeName", result["data"])
        self.assertNotIn("store_name", result["data"])

    def test_blocked_page_is_reported_to_caller(self):
        result = _fetch(
            _Page(status=403, content="<html><body>Access Denied</body></html>")
        )
        self.assertTrue(result["blocked"])
        self.assertEqual(result["status"], "failed")


if __name__ == "__main__":
    unittest.main()
