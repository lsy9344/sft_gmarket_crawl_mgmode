"""Patchright 소량 순차 수집 시험 — 실제 브라우저·네트워크 없음."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from app.core.base import CancelledError, Control
from app.core.coupang.patchright_canary import CATEGORY_URL, EXTRACT_PRODUCTS_JS, HOME_URL
from app.core.coupang.patchright_sample import (
    FETCH_STORE_REVIEW_JS,
    FETCH_VENDORS_JS,
    SHOP_SESSION_URL,
    run_sample,
)
from scripts.prototypes.coupang_patchright_sample import _print_state


class _Response:
    status = 200


class _Page:
    def __init__(
        self,
        *,
        blocked_at="",
        mapping_blocked=False,
        review_block_at=0,
        fail=False,
    ) -> None:
        self.url = "about:blank"
        self.urls: list[str] = []
        self.blocked_at = blocked_at
        self.mapping_blocked = mapping_blocked
        self.review_block_at = review_block_at
        self.fail = fail
        self.vendor_batches: list[list[str]] = []
        self.review_ids: list[str] = []
        self.extraction_limits: list[int] = []

    def goto(self, url, **_kwargs):
        self.url = str(url)
        if self.fail:
            raise RuntimeError("navigation failed")
        self.urls.append(self.url)
        return _Response()

    def wait_for_timeout(self, _milliseconds):
        return None

    def content(self):
        if self.blocked_at == "home" and self.url == HOME_URL:
            return "Access Denied Reference #18.home"
        if self.blocked_at == "category" and "/np/categories/" in self.url:
            return "Access Denied Reference #18.category"
        if self.blocked_at == "shop" and self.url == SHOP_SESSION_URL:
            return "Access Denied Reference #18.shop"
        return "<html><body>normal page content</body></html>"

    def evaluate(self, script, argument=None):
        if script == EXTRACT_PRODUCTS_JS:
            self.extraction_limits.append(int(argument))
            return [
                {
                    "href": f"/vp/products/{1000 + index}?itemId={2000 + index}"
                    f"&vendorItemId={3000 + index}",
                    "title": f"상품 {index}",
                    "priceText": f"{index},000원",
                    "rocket": False,
                    "sponsored": False,
                }
                for index in range(1, 7)
            ]
        if script == FETCH_VENDORS_JS:
            self.vendor_batches.append(list(argument))
            if self.mapping_blocked:
                return {
                    "status": 403,
                    "body": "Access Denied Reference #18.mapping",
                }
            products = []
            for vendor_item_id in argument:
                index = int(vendor_item_id) - 3000
                products.append(
                    {
                        "productId": f"P{index}",
                        "itemId": f"I{index}",
                        "vendorItemId": vendor_item_id,
                        "storeInfoArea": {
                            "vendorId": f"V{index}",
                            "storeId": index,
                            "displayName": f"스토어 {index}",
                        },
                    }
                )
            products.append(
                {
                    "productId": "P-extra",
                    "itemId": "I-extra",
                    "vendorItemId": "unexpected-extra",
                    "storeInfoArea": {
                        "vendorId": "V-extra",
                        "storeId": 999,
                        "displayName": "예상 밖 판매자",
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
            if self.review_block_at == len(self.review_ids):
                return {
                    "status": 403,
                    "body": "Access Denied Reference #18.review",
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
                        "eCommerceReportNumber": "2026-서울-1",
                        "ratingCount": 3,
                        "thumbUpRatio": 99.0,
                    }
                ),
            }
        raise AssertionError("unexpected script")


class _Context:
    def __init__(self, page: _Page) -> None:
        self.pages = [page]
        self.closed = False

    def new_page(self):
        return self.pages[0]


def _factory(page: _Page, calls: list[tuple[Path, bool]], context: _Context):
    @contextmanager
    def open_browser(user_data_dir, *, headless=False):
        calls.append((Path(user_data_dir), headless))
        try:
            yield context
        finally:
            context.closed = True

    return open_browser


class _CancelOnFirstSellerWait(Control):
    def __init__(self) -> None:
        super().__init__()
        self.sleep_calls = 0

    def sleep(self, _seconds: float, poll_interval: float = 0.1) -> None:
        self.sleep_calls += 1
        if self.sleep_calls == 4:
            raise CancelledError()


class PatchrightSampleTest(unittest.TestCase):
    def test_cli_log_hides_collected_contact_fields(self):
        output = StringIO()
        with redirect_stdout(output):
            _print_state(
                {
                    "event": "sample_completed",
                    "records": [{"phone": "02-secret", "email": "secret@example.com"}],
                }
            )
        visible = json.loads(output.getvalue())
        self.assertEqual(visible["records"], "1 record(s) saved")
        self.assertNotIn("02-secret", output.getvalue())
        self.assertNotIn("secret@example.com", output.getvalue())
        self.assertTrue(output.getvalue().isascii())

    def test_collects_and_saves_at_most_three_records(self):
        page = _Page()
        context = _Context(page)
        calls: list[tuple[Path, bool]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_sample(
                category_id="176573",
                output_dir=root / "out",
                limit=3,
                state_root=root / "state",
                browser_scope_factory=_factory(page, calls, context),
            )
            saved = json.loads(Path(result["json_path"]).read_text("utf-8"))

        self.assertEqual(result["event"], "sample_completed")
        self.assertEqual(result["record_count"], 3)
        self.assertEqual(result["document_navigations"], 3)
        self.assertEqual(result["api_calls"], 4)
        self.assertEqual(len(page.vendor_batches), 1)
        self.assertEqual(len(page.vendor_batches[0]), 3)
        self.assertEqual(page.extraction_limits, [3])
        self.assertEqual(page.review_ids, ["V1", "V2", "V3"])
        self.assertEqual(len(saved), 3)
        self.assertEqual(
            page.urls,
            [HOME_URL, CATEGORY_URL.format(category_id="176573"), SHOP_SESSION_URL],
        )
        self.assertFalse(calls[0][1])
        self.assertTrue(context.closed)

    def test_rejects_limit_above_twelve_before_browser(self):
        calls: list[tuple[Path, bool]] = []
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                run_sample(
                    category_id="176573",
                    output_dir=Path(tmp),
                    limit=13,
                    state_root=Path(tmp),
                    browser_scope_factory=_factory(_Page(), calls, _Context(_Page())),
                )
        self.assertEqual(calls, [])

    def test_rejects_scan_beyond_sixty_before_browser(self):
        calls: list[tuple[Path, bool]] = []
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                run_sample(
                    category_id="176573",
                    output_dir=Path(tmp),
                    limit=12,
                    offset=49,
                    state_root=Path(tmp),
                    browser_scope_factory=_factory(
                        _Page(), calls, _Context(_Page())
                    ),
                )
        self.assertEqual(calls, [])

    def test_page_number_changes_category_url_and_progress_file(self):
        page = _Page()
        context = _Context(page)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_sample(
                category_id="176573",
                page_number=2,
                output_dir=root / "out",
                limit=2,
                state_root=root / "state",
                browser_scope_factory=_factory(page, [], context),
            )

        self.assertEqual(result["event"], "sample_completed")
        self.assertIn(
            "https://www.coupang.com/np/categories/176573?page=2", page.urls
        )
        self.assertTrue(result["progress_path"].endswith("_page_2.json"))

    def test_seen_vendor_in_other_category_is_skipped_globally(self):
        page = _Page()
        context = _Context(page)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            output_dir.mkdir()
            (output_dir / "patchright_progress_194282.json").write_text(
                json.dumps(
                    {
                        "category_id": "194282",
                        "next_offset": 1,
                        "seen_vendor_item_ids": ["3001"],
                        "seen_vendor_ids": ["V1"],
                    }
                ),
                encoding="utf-8",
            )
            result = run_sample(
                category_id="176573",
                output_dir=output_dir,
                limit=2,
                state_root=root / "state",
                browser_scope_factory=_factory(page, [], context),
            )

        self.assertEqual(result["event"], "sample_completed")
        self.assertEqual(result["skipped_seen_vendor_count"], 1)
        self.assertEqual(page.review_ids, ["V2"])

    def test_returns_page_exhausted_without_shop_or_api(self):
        page = _Page()
        context = _Context(page)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            output_dir.mkdir()
            (output_dir / "patchright_progress_176573.json").write_text(
                json.dumps(
                    {
                        "category_id": "176573",
                        "next_offset": 6,
                        "seen_vendor_item_ids": [f"{3000 + i}" for i in range(1, 7)],
                        "seen_vendor_ids": [],
                    }
                ),
                encoding="utf-8",
            )
            result = run_sample(
                category_id="176573",
                output_dir=output_dir,
                limit=1,
                state_root=root / "state",
                browser_scope_factory=_factory(page, [], context),
            )

        self.assertEqual(result["event"], "page_exhausted")
        self.assertEqual(result["document_navigations"], 2)
        self.assertEqual(result["api_calls"], 0)
        self.assertNotIn(SHOP_SESSION_URL, page.urls)

    def test_offset_skips_previous_items_and_next_run_resumes(self):
        first_page = _Page()
        first_context = _Context(first_page)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = run_sample(
                category_id="176573",
                output_dir=root / "out",
                limit=2,
                offset=3,
                state_root=root / "state-1",
                browser_scope_factory=_factory(
                    first_page, [], first_context
                ),
            )
            progress = json.loads(
                Path(first["progress_path"]).read_text("utf-8")
            )

            second_page = _Page()
            second_context = _Context(second_page)
            second = run_sample(
                category_id="176573",
                output_dir=root / "out",
                limit=1,
                state_root=root / "state-2",
                browser_scope_factory=_factory(
                    second_page, [], second_context
                ),
            )

        self.assertEqual(first["event"], "sample_completed")
        self.assertEqual(first_page.extraction_limits, [5])
        self.assertEqual(first_page.vendor_batches, [["3004", "3005"]])
        self.assertEqual(first["next_offset"], 5)
        self.assertEqual(progress["next_offset"], 5)
        self.assertEqual(
            progress["seen_vendor_item_ids"],
            ["3001", "3002", "3003", "3004", "3005"],
        )
        self.assertEqual(progress["seen_vendor_ids"], ["V4", "V5"])
        self.assertEqual(second["event"], "sample_completed")
        self.assertEqual(second_page.extraction_limits, [6])
        self.assertEqual(second_page.vendor_batches, [["3006"]])
        self.assertEqual(second["next_offset"], 6)

    def test_seen_vendor_skips_business_request_and_advances_progress(self):
        page = _Page()
        context = _Context(page)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            output_dir.mkdir()
            progress_path = output_dir / "patchright_progress_176573.json"
            progress_path.write_text(
                json.dumps(
                    {
                        "category_id": "176573",
                        "next_offset": 1,
                        "seen_vendor_item_ids": ["3001"],
                        "seen_vendor_ids": ["V2"],
                    }
                ),
                encoding="utf-8",
            )
            result = run_sample(
                category_id="176573",
                output_dir=output_dir,
                limit=1,
                state_root=root / "state",
                browser_scope_factory=_factory(page, [], context),
            )
            progress = json.loads(progress_path.read_text("utf-8"))

        self.assertEqual(result["event"], "sample_completed")
        self.assertEqual(result["record_count"], 0)
        self.assertEqual(result["api_calls"], 1)
        self.assertEqual(result["skipped_seen_vendor_count"], 1)
        self.assertEqual(page.review_ids, [])
        self.assertEqual(result["next_offset"], 2)
        self.assertEqual(progress["seen_vendor_item_ids"], ["3001", "3002"])
        self.assertEqual(progress["seen_vendor_ids"], ["V2"])

    def test_rejects_offset_behind_saved_progress_before_browser(self):
        calls: list[tuple[Path, bool]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            output_dir.mkdir()
            (output_dir / "patchright_progress_176573.json").write_text(
                json.dumps(
                    {
                        "category_id": "176573",
                        "next_offset": 5,
                        "seen_vendor_item_ids": ["3001"],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                run_sample(
                    category_id="176573",
                    output_dir=output_dir,
                    limit=1,
                    offset=4,
                    state_root=root / "state",
                    browser_scope_factory=_factory(
                        _Page(), calls, _Context(_Page())
                    ),
                )
        self.assertEqual(calls, [])

    def test_category_block_stops_before_api_and_latches_guard(self):
        page = _Page(blocked_at="category")
        context = _Context(page)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_sample(
                category_id="176573",
                output_dir=root / "out",
                state_root=root,
                browser_scope_factory=_factory(page, [], context),
            )
            guard = json.loads((root / "canary_guard.json").read_text("utf-8"))
        self.assertEqual(result["event"], "blocked")
        self.assertEqual(result["blocked_at"], "category")
        self.assertEqual(result["reference"], "Reference #18.category")
        self.assertEqual(page.vendor_batches, [])
        self.assertTrue(guard["blocked"])
        self.assertTrue(context.closed)

    def test_second_review_block_saves_one_partial_and_stops(self):
        page = _Page(review_block_at=2)
        context = _Context(page)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_sample(
                category_id="176573",
                output_dir=root / "out",
                state_root=root / "state",
                browser_scope_factory=_factory(page, [], context),
            )
            saved = json.loads(Path(result["json_path"]).read_text("utf-8"))
            guard = json.loads(
                (root / "state" / "canary_guard.json").read_text("utf-8")
            )
        self.assertEqual(result["event"], "blocked")
        self.assertEqual(result["blocked_at"], "business_info")
        self.assertEqual(page.review_ids, ["V1", "V2"])
        self.assertEqual(len(saved), 1)
        self.assertIn("_partial.json", result["json_path"])
        self.assertTrue(guard["blocked"])
        self.assertTrue(context.closed)

    def test_cancel_after_first_record_saves_partial_and_stops(self):
        page = _Page()
        context = _Context(page)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_sample(
                category_id="176573",
                output_dir=root / "out",
                control=_CancelOnFirstSellerWait(),
                state_root=root / "state",
                browser_scope_factory=_factory(page, [], context),
            )
            saved = json.loads(Path(result["json_path"]).read_text("utf-8"))
        self.assertEqual(result["event"], "cancelled")
        self.assertEqual(page.review_ids, ["V1"])
        self.assertEqual(len(saved), 1)
        self.assertIn("_partial.json", result["json_path"])
        self.assertTrue(context.closed)

    def test_csv_failure_preserves_json_path(self):
        page = _Page()
        context = _Context(page)
        with tempfile.TemporaryDirectory() as tmp, patch(
            "app.core.coupang.exporter.CoupangExporter._atomic_write_csv",
            side_effect=OSError("csv failed"),
        ):
            root = Path(tmp)
            result = run_sample(
                category_id="176573",
                output_dir=root / "out",
                limit=3,
                state_root=root / "state",
                browser_scope_factory=_factory(page, [], context),
            )
            saved = json.loads(Path(result["json_path"]).read_text("utf-8"))
        self.assertEqual(result["event"], "failed")
        self.assertIn("CSV", result["error"])
        self.assertEqual(len(saved), 3)
        self.assertEqual(result["csv_path"], "")
        self.assertTrue(context.closed)

    def test_api_block_stops_before_reviews_and_latches_guard(self):
        page = _Page(mapping_blocked=True)
        context = _Context(page)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_sample(
                category_id="176573",
                output_dir=root / "out",
                state_root=root,
                browser_scope_factory=_factory(page, [], context),
            )
            guard = json.loads((root / "canary_guard.json").read_text("utf-8"))
        self.assertEqual(result["event"], "blocked")
        self.assertEqual(result["blocked_at"], "vendor_mapping")
        self.assertEqual(result["api_calls"], 1)
        self.assertEqual(page.review_ids, [])
        self.assertTrue(guard["blocked"])
        self.assertTrue(context.closed)

    def test_guard_refuses_second_attempt_before_browser(self):
        calls: list[tuple[Path, bool]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = run_sample(
                category_id="176573",
                output_dir=root / "out",
                state_root=root,
                browser_scope_factory=_factory(_Page(), calls, _Context(_Page())),
            )
            second = run_sample(
                category_id="176573",
                output_dir=root / "out",
                state_root=root,
                browser_scope_factory=_factory(_Page(), calls, _Context(_Page())),
            )
        self.assertEqual(first["event"], "sample_completed")
        self.assertEqual(second["event"], "guard_refused")
        self.assertEqual(len(calls), 1)

    def test_navigation_error_still_closes_browser(self):
        page = _Page(fail=True)
        context = _Context(page)
        with tempfile.TemporaryDirectory() as tmp:
            result = run_sample(
                category_id="176573",
                output_dir=Path(tmp) / "out",
                state_root=Path(tmp) / "state",
                browser_scope_factory=_factory(page, [], context),
            )
        self.assertEqual(result["event"], "failed")
        self.assertEqual(result["document_navigations"], 1)
        self.assertTrue(context.closed)

    def test_navigation_error_with_block_page_latches_guard(self):
        page = _Page(blocked_at="home", fail=True)
        context = _Context(page)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_sample(
                category_id="176573",
                output_dir=root / "out",
                state_root=root,
                browser_scope_factory=_factory(page, [], context),
            )
            guard = json.loads((root / "canary_guard.json").read_text("utf-8"))
        self.assertEqual(result["event"], "blocked")
        self.assertEqual(result["blocked_at"], "home")
        self.assertEqual(result["reference"], "Reference #18.home")
        self.assertTrue(guard["blocked"])
        self.assertTrue(context.closed)


if __name__ == "__main__":
    unittest.main()
