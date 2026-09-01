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
)


class _Response:
    status = 200


class _Page:
    def __init__(self, *, product_count=60, blocked_at="") -> None:
        self.url = "about:blank"
        self.urls: list[str] = []
        self.product_count = product_count
        self.blocked_at = blocked_at
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
        return "<html><body>normal page content</body></html>"

    def evaluate(self, script, argument=None):
        if script != EXTRACT_PRODUCTS_JS:
            raise AssertionError("unexpected script")
        limit = int(argument)
        self.extraction_limits.append(limit)
        return [
            {
                "href": f"/vp/products/{1000 + index}?itemId={2000 + index}"
                f"&vendorItemId={3000 + index}",
                "title": f"상품 {index}",
                "priceText": f"{index},000원",
            }
            for index in range(1, min(limit, self.product_count) + 1)
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
