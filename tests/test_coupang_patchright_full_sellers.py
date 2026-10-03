"""Patchright 과일 판매자 단계 — 실제 브라우저·네트워크 없음."""

from __future__ import annotations

import csv
import json
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

from app.core.coupang.patchright_canary import HOME_URL
from app.core.coupang.patchright_full_fruit import (
    PRODUCT_SELLER_FILENAME,
    SELLERS_FILENAME,
    FullFruitStore,
)
from app.core.coupang.patchright_full_sellers import (
    MIN_HTTP_503_RETRY_SECONDS,
    SELLER_CONTROL_FILENAME,
    authorize_http_503_retry,
    run_seller_batch,
)
from app.core.coupang.patchright_sample import (
    FETCH_STORE_REVIEW_JS,
    FETCH_VENDORS_JS,
    SHOP_SESSION_URL,
)
from app.models.coupang_records import RECORD_FIELDS


class _Response:
    status = 200


class _Page:
    def __init__(
        self,
        *,
        duplicate_vendors: bool = False,
        review_block_at: int = 0,
        review_crash_at: int = 0,
    ) -> None:
        self.url = "about:blank"
        self.urls: list[str] = []
        self.duplicate_vendors = duplicate_vendors
        self.review_block_at = review_block_at
        self.review_crash_at = review_crash_at
        self.mapping_batches: list[list[str]] = []
        self.review_ids: list[str] = []

    def goto(self, url, **_kwargs):
        self.url = str(url)
        self.urls.append(self.url)
        return _Response()

    def wait_for_timeout(self, _milliseconds):
        return None

    def content(self):
        return "<html><body>normal page content</body></html>"

    def evaluate(self, script, argument=None):
        if script == FETCH_VENDORS_JS:
            vendor_item_ids = list(argument)
            self.mapping_batches.append(vendor_item_ids)
            products = []
            for vendor_item_id in vendor_item_ids:
                index = int(vendor_item_id.removeprefix("VI"))
                vendor_index = (index + 1) // 2 if self.duplicate_vendors else index
                products.append(
                    {
                        "productId": f"P{index}",
                        "itemId": f"I{index}",
                        "vendorItemId": vendor_item_id,
                        "storeInfoArea": {
                            "vendorId": f"V{vendor_index}",
                            "storeId": vendor_index,
                            "displayName": f"스토어 {vendor_index}",
                        },
                    }
                )
            return {
                "status": 200,
                "body": json.dumps({"code": 200, "data": {"products": products}}),
            }
        if script == FETCH_STORE_REVIEW_JS:
            vendor_id = str(argument)
            self.review_ids.append(vendor_id)
            if self.review_crash_at == len(self.review_ids):
                raise RuntimeError("browser process stopped")
            if self.review_block_at == len(self.review_ids):
                return {
                    "status": 403,
                    "body": "Access Denied Reference #18.seller-full",
                }
            return {
                "status": 200,
                "body": json.dumps(
                    {
                        "name": f"회사 {vendor_id}",
                        "repPersonName": "대표",
                        "businessNumber": "123-45-67890",
                        "repPhoneNum": "02-0000-0000",
                        "repEmail": "seller@example.com",
                        "repAddr1": "서울",
                        "repAddr2": "테스트로 1",
                    }
                ),
            }
        raise AssertionError("unexpected script")


class _Context:
    def __init__(self, page: _Page) -> None:
        self.pages = [page]
        self.closed = False


def _factory(page: _Page, calls: list[Path]):
    @contextmanager
    def open_browser(user_data_dir, *, headless=False):
        if headless:
            raise AssertionError("seller browser must remain visible")
        calls.append(Path(user_data_dir))
        context = _Context(page)
        try:
            yield context
        finally:
            context.closed = True

    return open_browser


def _add_products(store: FullFruitStore, count: int) -> None:
    store.merge_products(
        [
            {
                "category_id": "194284",
                "category_name": "사과/배",
                "page_number": "1",
                "product_id": f"P{index}",
                "item_id": f"I{index}",
                "vendor_item_id": f"VI{index}",
                "title": f"상품 {index}",
                "price": "1000",
                "url": "",
                "collected_at": "2026-09-02 00:00:00",
            }
            for index in range(1, count + 1)
        ]
    )


class PatchrightFullSellersTest(unittest.TestCase):
    def test_maps_in_tens_and_saves_each_unique_seller_once(self):
        page = _Page(duplicate_vendors=True)
        calls: list[Path] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = FullFruitStore(root / "out")
            store.ensure_files()
            _add_products(store, 12)
            result = run_seller_batch(
                output_dir=store.output_dir,
                limit=12,
                state_root=root / "guard",
                browser_scope_factory=_factory(page, calls),
            )
            with (store.output_dir / PRODUCT_SELLER_FILENAME).open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                mappings = list(csv.DictReader(handle))
            with (store.output_dir / SELLERS_FILENAME).open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                sellers = list(csv.DictReader(handle))

        self.assertEqual(result["event"], "seller_collection_complete")
        self.assertEqual([len(batch) for batch in page.mapping_batches], [10, 2])
        self.assertEqual(len(mappings), 12)
        self.assertEqual(len(sellers), 6)
        self.assertEqual(page.review_ids, [f"V{index}" for index in range(1, 7)])
        self.assertEqual(result["api_calls"], 8)
        self.assertEqual(page.urls, [HOME_URL, SHOP_SESSION_URL])
        self.assertEqual(len(calls), 1)

    def test_blocked_review_is_checkpointed_and_later_run_does_not_open_browser(self):
        page = _Page(review_block_at=2)
        first_calls: list[Path] = []
        second_calls: list[Path] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = FullFruitStore(root / "out")
            store.ensure_files()
            _add_products(store, 4)
            first = run_seller_batch(
                output_dir=store.output_dir,
                limit=4,
                state_root=root / "guard",
                browser_scope_factory=_factory(page, first_calls),
            )
            second = run_seller_batch(
                output_dir=store.output_dir,
                limit=4,
                state_root=root / "another-guard",
                browser_scope_factory=_factory(_Page(), second_calls),
            )
            with (store.output_dir / SELLERS_FILENAME).open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                sellers = list(csv.DictReader(handle))
            with (store.output_dir / PRODUCT_SELLER_FILENAME).open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                mappings = list(csv.DictReader(handle))
            control = json.loads(
                (store.output_dir / SELLER_CONTROL_FILENAME).read_text("utf-8")
            )

        self.assertEqual(first["event"], "blocked")
        self.assertEqual([row["status"] for row in sellers], ["saved", "failed"])
        self.assertEqual(len(mappings), 4)
        self.assertEqual(control["status"], "halted")
        self.assertEqual(second["event"], "seller_halted")
        self.assertEqual(len(first_calls), 1)
        self.assertEqual(second_calls, [])

    def test_imports_old_samples_and_seen_vendor_without_network(self):
        calls: list[Path] = []
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            store = FullFruitStore(output_dir)
            store.ensure_files()
            with (output_dir / "patchright_sample_old.csv").open(
                "w", encoding="utf-8-sig", newline=""
            ) as handle:
                writer = csv.DictWriter(handle, fieldnames=RECORD_FIELDS)
                writer.writeheader()
                writer.writerow({"vendor_id": "V1", "email": "seller@example.com"})
            (output_dir / "patchright_progress_194284.json").write_text(
                json.dumps(
                    {
                        "category_id": "194284",
                        "next_offset": 2,
                        "seen_vendor_item_ids": ["VI1", "VI2"],
                        "seen_vendor_ids": ["V1", "V2"],
                    }
                ),
                encoding="utf-8",
            )
            result = run_seller_batch(
                output_dir=output_dir,
                state_root=output_dir / "guard",
                browser_scope_factory=_factory(_Page(), calls),
            )
            with (output_dir / SELLERS_FILENAME).open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                sellers = list(csv.DictReader(handle))

        self.assertEqual(result["event"], "seller_collection_complete")
        self.assertEqual(result["imported_sellers"], 2)
        self.assertEqual(
            {row["vendor_id"]: row["status"] for row in sellers},
            {"V1": "saved", "V2": "no_public_info"},
        )
        self.assertEqual(calls, [])

    def test_crash_after_request_start_never_requests_same_seller_again(self):
        page = _Page(review_crash_at=1)
        retry_calls: list[Path] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = FullFruitStore(root / "out")
            store.ensure_files()
            _add_products(store, 1)
            first = run_seller_batch(
                output_dir=store.output_dir,
                limit=1,
                state_root=root / "guard",
                browser_scope_factory=_factory(page, []),
            )
            control_path = store.output_dir / SELLER_CONTROL_FILENAME
            control_path.write_text(
                json.dumps({"version": 1, "status": "ready"}),
                encoding="utf-8",
            )
            second = run_seller_batch(
                output_dir=store.output_dir,
                limit=1,
                state_root=root / "another-guard",
                browser_scope_factory=_factory(_Page(), retry_calls),
            )
            with (store.output_dir / SELLERS_FILENAME).open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                sellers = list(csv.DictReader(handle))

        self.assertEqual(first["event"], "failed")
        self.assertEqual(page.review_ids, ["V1"])
        self.assertEqual(sellers[0]["status"], "failed")
        self.assertEqual(second["event"], "seller_collection_complete")
        self.assertEqual(retry_calls, [])

    def test_http_503_retry_requires_three_hours_and_opens_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            halted_at = time.mktime(
                time.strptime("2026-09-02 21:21:39", "%Y-%m-%d %H:%M:%S")
            )
            (output_dir / SELLER_CONTROL_FILENAME).write_text(
                json.dumps(
                    {
                        "version": 1,
                        "status": "halted",
                        "updated_at": "2026-09-02 21:21:39",
                        "reason": "상품-판매자 연결 HTTP 503",
                    }
                ),
                encoding="utf-8",
            )
            early, reason = authorize_http_503_retry(
                output_dir, now=halted_at + MIN_HTTP_503_RETRY_SECONDS - 1
            )
            allowed, _reason = authorize_http_503_retry(
                output_dir, now=halted_at + MIN_HTTP_503_RETRY_SECONDS
            )
            second, _reason = authorize_http_503_retry(
                output_dir, now=halted_at + MIN_HTTP_503_RETRY_SECONDS + 1
            )
            control = json.loads(
                (output_dir / SELLER_CONTROL_FILENAME).read_text("utf-8")
            )

        self.assertFalse(early)
        self.assertIn("1분", reason)
        self.assertTrue(allowed)
        self.assertFalse(second)
        self.assertEqual(control["status"], "ready")


if __name__ == "__main__":
    unittest.main()
