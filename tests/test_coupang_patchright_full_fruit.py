"""Patchright 과일 전량 상품 목록 단계 — 실제 네트워크 없음."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from app.core.coupang.patchright_canary import EXTRACT_PRODUCTS_JS, HOME_URL
from app.core.coupang.patchright_fruit import FRUIT_CATEGORIES
from app.core.coupang.patchright_full_fruit import (
    MAX_CATEGORY_PAGES,
    PRODUCTS_FILENAME,
    STATE_FILENAME,
    FullFruitStore,
    _advance_listing_state,
    _new_state,
    run_listing_batch,
    run_listing_category,
    run_listing_pages,
)


class _Response:
    status = 200


class _Page:
    def __init__(
        self,
        *,
        product_count=60,
        product_counts_by_page=None,
        blocked_at="",
        blocked_page_number=0,
    ) -> None:
        self.url = "about:blank"
        self.urls: list[str] = []
        self.product_count = product_count
        self.product_counts_by_page = product_counts_by_page or {}
        self.blocked_at = blocked_at
        self.blocked_page_number = blocked_page_number
        self.extraction_limits: list[int] = []

    def goto(self, url, **_kwargs):
        self.url = str(url)
        self.urls.append(self.url)
        return _Response()

    def wait_for_timeout(self, _milliseconds):
        return None

    def content(self):
        if self.blocked_at == "category" and "/np/categories/" in self.url:
            return "Access Denied Reference #18.full"
        if (
            self.blocked_page_number
            and f"?page={self.blocked_page_number}" in self.url
        ):
            return "Access Denied Reference #18.full-page"
        return "<html><body>normal page content</body></html>"

    def evaluate(self, script, argument=None):
        if script != EXTRACT_PRODUCTS_JS:
            raise AssertionError("unexpected script")
        limit = int(argument)
        self.extraction_limits.append(limit)
        page_number = 1
        if "?page=" in self.url:
            page_number = int(self.url.rsplit("?page=", 1)[1])
        product_count = self.product_counts_by_page.get(
            page_number, self.product_count
        )
        page_base = (page_number - 1) * 100
        return [
            {
                "href": f"/vp/products/{1000 + page_base + index}"
                f"?itemId={2000 + page_base + index}"
                f"&vendorItemId={3000 + page_base + index}",
                "title": f"상품 {index}",
                "priceText": f"{index},000원",
            }
            for index in range(1, min(limit, product_count) + 1)
        ]


class _Context:
    def __init__(self, page: _Page) -> None:
        self.pages = [page]
        self.closed = False


def _factory(page: _Page, calls: list[tuple[Path, bool]], context: _Context):
    @contextmanager
    def open_browser(user_data_dir, *, headless=False):
        calls.append((Path(user_data_dir), headless))
        try:
            yield context
        finally:
            context.closed = True

    return open_browser


class PatchrightFullFruitTest(unittest.TestCase):
    def test_migrates_existing_twenty_and_collects_next_twenty_four(self):
        page = _Page()
        context = _Context(page)
        calls: list[tuple[Path, bool]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            output_dir.mkdir()
            (output_dir / "patchright_progress_194284.json").write_text(
                json.dumps({"category_id": "194284", "next_offset": 20}),
                encoding="utf-8",
            )
            result = run_listing_batch(
                output_dir=output_dir,
                limit=24,
                state_root=root / "state",
                browser_scope_factory=_factory(page, calls, context),
            )
            state = json.loads((output_dir / STATE_FILENAME).read_text("utf-8"))
            with (output_dir / PRODUCTS_FILENAME).open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                products = list(csv.DictReader(handle))

        self.assertEqual(result["event"], "listing_batch_completed")
        self.assertEqual(result["product_count"], 24)
        self.assertEqual(result["api_calls"], 0)
        self.assertEqual(result["document_navigations"], 2)
        self.assertEqual(page.extraction_limits, [44])
        self.assertEqual(len(products), 44)
        self.assertEqual(products[0]["price"], "1000")
        self.assertEqual(state["next_offset"], 44)
        self.assertEqual(state["raw_products_seen"], 44)
        self.assertEqual(len(page.urls), 2)
        self.assertEqual(page.urls[0], HOME_URL)
        self.assertTrue(context.closed)

    def test_product_merge_deduplicates_across_categories(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FullFruitStore(Path(tmp))
            store.ensure_files()
            first = {
                "category_id": "194284",
                "vendor_item_id": "V1",
                "product_id": "P1",
            }
            second = {
                "category_id": "194288",
                "vendor_item_id": "V1",
                "product_id": "P1",
            }
            self.assertEqual(store.merge_products([first]), (1, 1))
            self.assertEqual(store.merge_products([second]), (1, 0))

    def test_seller_and_product_mapping_are_globally_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FullFruitStore(Path(tmp))
            store.ensure_files()
            seller = {"status": "saved", "vendor_id": "S1"}
            failed_duplicate = {
                "status": "failed",
                "vendor_id": "S1",
                "error": "temporary",
            }
            link = {"vendor_item_id": "V1", "vendor_id": "S1"}
            self.assertEqual(store.merge_sellers([seller]), (1, 1))
            self.assertEqual(store.merge_sellers([failed_duplicate]), (1, 0))
            self.assertEqual(store.merge_product_sellers([link]), (1, 1))
            self.assertEqual(store.merge_product_sellers([link]), (1, 0))

    def test_two_empty_pages_move_to_next_category_in_tree_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = _new_state(Path(tmp))
        first_id = FRUIT_CATEGORIES[0][0]
        second_id = FRUIT_CATEGORIES[1][0]
        _advance_listing_state(state, selected_count=0)
        self.assertEqual(state["category_index"], 0)
        _advance_listing_state(state, selected_count=0)
        self.assertEqual(state["completed_categories"], [first_id])
        self.assertEqual(state["category_index"], 1)
        self.assertEqual(state["category_statuses"][second_id], "running")

    def test_all_twelve_categories_finish_in_tree_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = _new_state(Path(tmp))
        visited = []
        for category_id, _name in FRUIT_CATEGORIES:
            visited.append(FRUIT_CATEGORIES[state["category_index"]][0])
            _advance_listing_state(state, selected_count=0)
            _advance_listing_state(state, selected_count=0)
        self.assertEqual(visited, [item[0] for item in FRUIT_CATEGORIES])
        self.assertEqual(state["completed_categories"], visited)
        self.assertEqual(state["phase"], "sellers")

    def test_page_fifty_with_more_products_is_incomplete_not_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = _new_state(Path(tmp))
        state["page_number"] = MAX_CATEGORY_PAGES
        state["next_offset"] = 40
        _advance_listing_state(state, selected_count=20)
        self.assertEqual(state["status"], "incomplete_limit_reached")
        self.assertEqual(
            state["category_statuses"][FRUIT_CATEGORIES[0][0]],
            "incomplete_limit_reached",
        )
        self.assertEqual(state["completed_categories"], [])

    def test_three_pages_share_one_browser_and_checkpoint_each_page(self):
        page = _Page()
        context = _Context(page)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            output_dir.mkdir()
            state = _new_state(output_dir)
            state["page_number"] = 2
            (output_dir / STATE_FILENAME).write_text(
                json.dumps(state), encoding="utf-8"
            )
            result = run_listing_pages(
                output_dir=output_dir,
                page_count=3,
                state_root=root / "state",
                browser_scope_factory=_factory(page, [], context),
            )
            saved_state = json.loads(
                (output_dir / STATE_FILENAME).read_text("utf-8")
            )

        self.assertEqual(result["event"], "listing_pages_completed")
        self.assertEqual(result["completed_page_attempts"], 3)
        self.assertEqual(result["total_product_count"], 180)
        self.assertEqual(result["document_navigations"], 4)
        self.assertEqual([item["page_number"] for item in result["page_results"]], [2, 3, 4])
        self.assertEqual(saved_state["page_number"], 5)
        self.assertEqual(saved_state["next_offset"], 0)
        self.assertTrue(context.closed)

    def test_block_on_second_page_preserves_first_page_checkpoint(self):
        page = _Page(blocked_page_number=3)
        context = _Context(page)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            output_dir.mkdir()
            state = _new_state(output_dir)
            state["page_number"] = 2
            (output_dir / STATE_FILENAME).write_text(
                json.dumps(state), encoding="utf-8"
            )
            result = run_listing_pages(
                output_dir=output_dir,
                page_count=3,
                state_root=root / "state",
                browser_scope_factory=_factory(page, [], context),
            )
            saved_state = json.loads(
                (output_dir / STATE_FILENAME).read_text("utf-8")
            )
            with (output_dir / PRODUCTS_FILENAME).open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                products = list(csv.DictReader(handle))

        self.assertEqual(result["event"], "blocked")
        self.assertEqual(result["completed_page_attempts"], 1)
        self.assertEqual(saved_state["page_number"], 3)
        self.assertEqual(len(products), 60)
        self.assertTrue(context.closed)

    def test_category_run_stops_after_two_empty_pages(self):
        page = _Page(product_counts_by_page={2: 0, 3: 0})
        context = _Context(page)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            output_dir.mkdir()
            state = _new_state(output_dir)
            state["page_number"] = 2
            (output_dir / STATE_FILENAME).write_text(
                json.dumps(state), encoding="utf-8"
            )
            result = run_listing_category(
                output_dir=output_dir,
                state_root=root / "state",
                browser_scope_factory=_factory(page, [], context),
            )
            saved_state = json.loads(
                (output_dir / STATE_FILENAME).read_text("utf-8")
            )

        self.assertEqual(result["event"], "listing_category_completed")
        self.assertEqual(result["completed_page_attempts"], 2)
        self.assertEqual(saved_state["completed_categories"], ["194284"])
        self.assertEqual(saved_state["category_index"], 1)
        self.assertEqual(saved_state["page_number"], 1)
        self.assertEqual(len(page.urls), 3)
        self.assertTrue(context.closed)

    def test_category_run_reports_page_limit_as_incomplete(self):
        page = _Page()
        context = _Context(page)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            output_dir.mkdir()
            state = _new_state(output_dir)
            state["page_number"] = MAX_CATEGORY_PAGES
            (output_dir / STATE_FILENAME).write_text(
                json.dumps(state), encoding="utf-8"
            )
            result = run_listing_category(
                output_dir=output_dir,
                state_root=root / "state",
                browser_scope_factory=_factory(page, [], context),
            )
            saved_state = json.loads(
                (output_dir / STATE_FILENAME).read_text("utf-8")
            )

        self.assertEqual(result["event"], "incomplete_limit_reached")
        self.assertEqual(result["completed_page_attempts"], 1)
        self.assertEqual(saved_state["status"], "incomplete_limit_reached")
        self.assertEqual(saved_state["completed_categories"], [])
        self.assertTrue(context.closed)

    def test_category_block_latches_guard_without_advancing_state(self):
        page = _Page(blocked_at="category")
        context = _Context(page)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            store = FullFruitStore(output_dir)
            store.ensure_files()
            store.merge_products(
                [{"vendor_item_id": "kept", "product_id": "P-kept"}]
            )
            result = run_listing_batch(
                output_dir=output_dir,
                limit=24,
                state_root=root / "state",
                browser_scope_factory=_factory(page, [], context),
            )
            guard = json.loads(
                (root / "state" / "canary_guard.json").read_text("utf-8")
            )
            with (output_dir / PRODUCTS_FILENAME).open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                products = list(csv.DictReader(handle))

        self.assertEqual(result["event"], "blocked")
        self.assertFalse((output_dir / STATE_FILENAME).exists())
        self.assertTrue(guard["blocked"])
        self.assertEqual([row["vendor_item_id"] for row in products], ["kept"])
        self.assertTrue(context.closed)

    def test_corrupt_state_refuses_before_browser(self):
        calls: list[tuple[Path, bool]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            output_dir.mkdir()
            (output_dir / STATE_FILENAME).write_text("{broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                run_listing_batch(
                    output_dir=output_dir,
                    limit=24,
                    state_root=root / "state",
                    browser_scope_factory=_factory(
                        _Page(), calls, _Context(_Page())
                    ),
                )
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
