"""Patchright 상위 데이터셋 판매자 단계·파이프라인 — 실제 네트워크 없음."""

from __future__ import annotations

import csv
import json
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

from app.core.coupang.patchright_sample import (
    FETCH_STORE_REVIEW_JS,
    FETCH_VENDORS_JS,
)
from app.core.coupang.patchright_top_sellers import (
    TopSellerStore,
    _work,
    authorize_http_503_retry,
    build_seller_final,
    run_top_seller_batch,
)
from app.core.coupang.patchright_top_thousand import (
    PRODUCT_FIELDS,
    TopThousandStore,
    read_state,
)


class _Response:
    status = 200


def _product_row(vendor_item_id: str, *, category_id="194373",
                 category_name="식품/견과/건과") -> dict:
    return {
        "category_id": category_id,
        "category_name": category_name,
        "page_number": "1",
        "product_id": "10",
        "item_id": "20",
        "vendor_item_id": vendor_item_id,
        "title": f"상품 {vendor_item_id}",
        "price": "1000",
        "review_count": "100",
        "delivery_markers": "",
        "url": f"https://www.coupang.com/vp/products/10?vendorItemId={vendor_item_id}",
        "collected_at": "2026-10-01 00:00:00",
    }


def _vendors_payload(vendor_item_id: str, vendor_id: str = "A00001") -> dict:
    return {
        "code": 200,
        "data": {
            "products": [
                {
                    "productId": 10,
                    "itemId": 20,
                    "vendorItemId": vendor_item_id,
                    "storeInfoArea": {
                        "vendorId": vendor_id,
                        "storeId": 99,
                        "displayName": "테스트스토어",
                    },
                }
            ]
        },
    }


def _seller_payload(**changes) -> dict:
    payload = {
        "name": "주식회사테스트",
        "repPersonName": "홍길동",
        "businessNumber": "123-45-67890",
        "repPhoneNum": "02-1234-5678",
        "repEmail": "test@example.com",
        "repAddr1": "서울특별시 강남구",
        "repAddr2": "테헤란로 1",
        "eCommerceReportNumber": "",
        "ratingCount": 4321,
        "thumbUpRatio": 95,
    }
    payload.update(changes)
    return payload


class _Page:
    def __init__(self, *, vendors_by_item=None, sellers_by_id=None,
                 seller_error_at="") -> None:
        self.url = "about:blank"
        self.vendors_by_item = vendors_by_item or {}
        self.sellers_by_id = sellers_by_id or {}
        self.seller_error_at = seller_error_at

    def goto(self, url, **_kwargs):
        self.url = str(url)
        return _Response()

    def wait_for_timeout(self, _milliseconds):
        return None

    def content(self):
        return "<html><body>normal page content</body></html>"

    def evaluate(self, script, argument=None):
        if script == FETCH_VENDORS_JS:
            products = []
            for vendor_item_id in argument:
                vendor_id = self.vendors_by_item.get(vendor_item_id)
                if vendor_id is None:
                    continue
                products.append(
                    {
                        "productId": 10,
                        "itemId": 20,
                        "vendorItemId": vendor_item_id,
                        "storeInfoArea": {
                            "vendorId": vendor_id,
                            "storeId": 99,
                            "displayName": "테스트스토어",
                        },
                    }
                )
            return {"status": 200, "body": json.dumps({"code": 200, "data": {"products": products}})}
        if script == FETCH_STORE_REVIEW_JS:
            if argument == self.seller_error_at:
                return {"status": 503, "body": "Service Unavailable"}
            payload = self.sellers_by_id.get(argument)
            if payload is None:
                return {"status": 200, "body": json.dumps({})}
            return {"status": 200, "body": json.dumps(payload)}
        raise AssertionError("unexpected script")


class _Context:
    def __init__(self, page: _Page) -> None:
        self.pages = [page]


def _factory(page: _Page):
    @contextmanager
    def open_browser(user_data_dir, *, headless=False):
        yield _Context(page)

    return open_browser


def _prepare(output_dir: Path, product_rows: list[dict]) -> None:
    store = TopThousandStore(output_dir)
    store.ensure_files()
    TopSellerStore(output_dir).ensure_files()
    from app.core.coupang.patchright_full_fruit import _write_csv

    _write_csv(output_dir / "top_products.csv", PRODUCT_FIELDS, product_rows)


def _read_rows(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


class TopSellersTest(unittest.TestCase):
    def test_maps_products_and_saves_business_info(self):
        page = _Page(
            vendors_by_item={"viid-1": "A00001"},
            sellers_by_id={"A00001": _seller_payload()},
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            result = run_top_seller_batch(
                output_dir=output_dir,
                limit=10,
                state_root=root / "guard",
                browser_scope_factory=_factory(page),
            )
            self.assertEqual(result["event"], "seller_collection_complete")
            sellers = _read_rows(output_dir / "top_sellers.csv")
            self.assertEqual(len(sellers), 1)
            self.assertEqual(sellers[0]["status"], "saved")
            self.assertEqual(sellers[0]["company_name"], "주식회사테스트")
            self.assertEqual(sellers[0]["ceo_name"], "홍길동")
            self.assertEqual(sellers[0]["business_number"], "123-45-67890")
            self.assertEqual(sellers[0]["email"], "test@example.com")
            mappings = _read_rows(output_dir / "top_product_seller.csv")
            self.assertEqual(
                [row["vendor_id"] for row in mappings], ["A00001"]
            )
            final_rows = _read_rows(output_dir / "coupang_판매자_1명.csv")
            self.assertEqual(len(final_rows), 1)
            self.assertEqual(final_rows[0]["상호"], "주식회사테스트")
            self.assertEqual(final_rows[0]["대표자"], "홍길동")
            self.assertEqual(final_rows[0]["소속카테고리목록"], "식품/견과/건과")
            self.assertEqual(final_rows[0]["상품수"], "1")
            control = json.loads(
                (output_dir / "top_seller_control.json").read_text("utf-8")
            )
            self.assertEqual(control["status"], "completed")

    def test_http_503_halts_and_retry_requires_three_hours(self):
        page = _Page(
            vendors_by_item={"viid-1": "A00001"},
            sellers_by_id={"A00001": _seller_payload()},
            seller_error_at="A00001",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            result = run_top_seller_batch(
                output_dir=output_dir,
                limit=10,
                state_root=root / "guard",
                browser_scope_factory=_factory(page),
            )
            self.assertEqual(result["event"], "failed")
            control = json.loads(
                (output_dir / "top_seller_control.json").read_text("utf-8")
            )
            self.assertEqual(control["status"], "halted")
            self.assertIn("HTTP 503", str(control["reason"]))
            allowed_now, reason = authorize_http_503_retry(output_dir)
            self.assertFalse(allowed_now)
            self.assertIn("남았습니다", reason)
            control["updated_at"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 4 * 60 * 60)
            )
            (output_dir / "top_seller_control.json").write_text(
                json.dumps(control, ensure_ascii=False), encoding="utf-8"
            )
            allowed_later, _reason = authorize_http_503_retry(output_dir)
            self.assertTrue(allowed_later)

    def test_work_reads_twelve_column_products(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            _prepare(
                output_dir,
                [_product_row("viid-1"), _product_row("viid-2")],
            )
            pending, unmapped = _work(TopSellerStore(output_dir), 10)
            self.assertEqual(pending, [])
            self.assertEqual(len(unmapped), 2)

    def test_build_seller_final_skips_unsaved_and_sorts_by_rating(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            store = TopSellerStore(output_dir)
            _prepare(output_dir, [_product_row("viid-1")])
            from app.core.coupang.patchright_full_fruit import _write_csv
            from app.core.coupang.patchright_full_fruit import (
                PRODUCT_SELLER_FIELDS,
                SELLER_FIELDS,
            )

            _write_csv(
                output_dir / "top_product_seller.csv",
                PRODUCT_SELLER_FIELDS,
                [
                    {
                        "product_id": "10",
                        "item_id": "20",
                        "vendor_item_id": "viid-1",
                        "vendor_id": "A00001",
                        "mapped_at": "2026-10-01 00:00:00",
                    }
                ],
            )
            saved = {
                "status": "saved",
                "vendor_id": "A00001",
                "url": "u",
                "store_name": "스토어",
                "company_name": "상호1",
                "ceo_name": "대표1",
                "business_number": "1",
                "phone": "p",
                "email": "e",
                "address": "a",
                "ecommerce_report_number": "",
                "power_seller": "False",
                "power_seller_title": "",
                "rating_count": "100",
                "thumb_up_ratio": "90",
                "error": "",
                "updated_at": "2026-10-01 00:00:00",
            }
            no_info = {**saved, "vendor_id": "A00002", "status": "no_public_info"}
            _write_csv(
                output_dir / "top_sellers.csv",
                SELLER_FIELDS,
                [no_info, saved],
            )
            path = build_seller_final(output_dir)
            rows = _read_rows(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["판매자ID"], "A00001")
            self.assertEqual(path.name, "coupang_판매자_1명.csv")


class PipelineChooseTest(unittest.TestCase):
    def test_chooses_sellers_before_listing(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "top_pipeline",
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "prototypes"
            / "coupang_patchright_top_pipeline.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            action, reason = module.choose_action(output_dir)
            self.assertEqual(action, "sellers")

            # 판매자가 끝나고 목록 상태도 끝났으면 complete.
            from app.core.coupang.patchright_full_fruit import (
                PRODUCT_SELLER_FIELDS,
                SELLER_FIELDS,
                _write_csv,
            )

            _write_csv(
                output_dir / "top_product_seller.csv",
                PRODUCT_SELLER_FIELDS,
                [
                    {
                        "product_id": "10",
                        "item_id": "20",
                        "vendor_item_id": "viid-1",
                        "vendor_id": "A00001",
                        "mapped_at": "2026-10-01 00:00:00",
                    }
                ],
            )
            _write_csv(
                output_dir / "top_sellers.csv",
                SELLER_FIELDS,
                [
                    {
                        "status": "saved",
                        "vendor_id": "A00001",
                        "url": "u",
                        "store_name": "",
                        "company_name": "c",
                        "ceo_name": "n",
                        "business_number": "1",
                        "phone": "",
                        "email": "",
                        "address": "",
                        "ecommerce_report_number": "",
                        "power_seller": "False",
                        "power_seller_title": "",
                        "rating_count": "1",
                        "thumb_up_ratio": "1",
                        "error": "",
                        "updated_at": "2026-10-01 00:00:00",
                    }
                ],
            )
            state = read_state(output_dir)
            state["status"] = "completed"
            state["category_index"] = 19
            for record in state["categories"].values():
                record["status"] = "exhausted"
            (output_dir / "top_state.json").write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8"
            )
            action, reason = module.choose_action(output_dir)
            self.assertEqual(action, "complete")


if __name__ == "__main__":
    unittest.main()
