"""Patchright 카테고리 상위 데이터셋 수집 — 실제 네트워크 없음."""

from __future__ import annotations

import csv
import json
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from app.core.coupang.patchright_canary import HOME_URL
from app.core.coupang.patchright_top_thousand import (
    EXTRACT_CARDS_JS,
    PRODUCTS_FILENAME,
    TARGET_CATEGORY_ITEMS,
    TOP_CATEGORIES,
    TopThousandStore,
    _new_state,
    build_combined_final_dataset,
    build_final_dataset,
    classify_card,
    read_state,
    run_top_pages,
)


class _Response:
    status = 200


class _Page:
    """페이지별 카드 목록을 제공하는 가짜 브라우저 페이지."""

    def __init__(
        self,
        *,
        cards_by_page=None,
        default_cards=None,
        blocked_page="",
    ) -> None:
        self.url = "about:blank"
        self.urls: list[str] = []
        self.cards_by_page = cards_by_page or {}
        self.default_cards = default_cards or []
        self.blocked_page = blocked_page

    def goto(self, url, **_kwargs):
        self.url = str(url)
        self.urls.append(self.url)
        return _Response()

    def wait_for_timeout(self, _milliseconds):
        return None

    def content(self):
        if self.blocked_page and self.url == self.blocked_page:
            return "Access Denied Reference #18.full"
        return "<html><body>normal page content</body></html>"

    def evaluate(self, script, argument=None):
        if script != EXTRACT_CARDS_JS:
            raise AssertionError("unexpected script")
        assert int(argument) == 60
        page_number = 1
        if "?page=" in self.url:
            page_number = int(self.url.rsplit("?page=", 1)[1])
        category_id = ""
        if "/np/categories/" in self.url:
            category_id = (
                self.url.split("/np/categories/", 1)[1]
                .split("?")[0]
                .split("/")[0]
            )
        if not category_id:
            return self.default_cards
        key = (category_id, page_number)
        if key in self.cards_by_page:
            return self.cards_by_page[key]
        return self.default_cards


def _card(
    index: int,
    vendor_item_id: str,
    *,
    review_text: str = "",
    extra_text: str = "",
    image_srcs: str = "",
) -> dict:
    full_text = f"상품 {index} {index},000원 {review_text} {extra_text}".strip()
    return {
        "href": (
            f"/vp/products/{9000 + index}"
            f"?itemId={9100 + index}&vendorItemId={vendor_item_id}"
        ),
        "title": f"상품 {index}",
        "fullText": full_text,
        "imageSrcs": image_srcs,
    }


class _Context:
    def __init__(self, page: _Page) -> None:
        self.pages = [page]


def _factory(page: _Page, calls: list[str]):
    @contextmanager
    def open_browser(user_data_dir, *, headless=False):
        calls.append(str(user_data_dir))
        yield _Context(page)

    return open_browser


def _read_rows(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _age_last_attempt(guard_dir: Path) -> None:
    """연속 실행 사이의 최소 접속 간격을 이미 지난 것으로 기록한다."""
    path = guard_dir / "canary_guard.json"
    guard = json.loads(path.read_text(encoding="utf-8"))
    guard["last_attempt_ts"] = time.time() - 2 * 60 * 60
    path.write_text(json.dumps(guard), encoding="utf-8")


def _row(
    category_id: str,
    vendor_item_id: str,
    *,
    review: str,
    name: str,
) -> dict:
    """완성형 빌더 시험용 수집 행."""
    return {
        "category_id": category_id,
        "category_name": name,
        "page_number": "1",
        "product_id": "1",
        "item_id": "2",
        "vendor_item_id": vendor_item_id,
        "title": f"상품 {vendor_item_id}",
        "price": "1000",
        "review_count": review,
        "delivery_markers": "",
        "url": f"https://www.coupang.com/vp/products/1?vendorItemId={vendor_item_id}",
        "collected_at": "2026-10-01 00:00:00",
    }


class ClassifyCardTest(unittest.TestCase):
    def test_rocket_text_and_images_are_detected(self):
        text_card = classify_card(_card(1, "v1", extra_text="로켓배송 내일 도착"))
        image_card = classify_card(
            _card(2, "v2", image_srcs="https://aimg.coupangcdn.com/rds/logo.png")
        )
        fresh_card = classify_card(_card(3, "v3", extra_text="로켓프레시 새벽 도착"))
        guarantee = classify_card(_card(4, "v4", extra_text="내일(수) 도착 보증"))
        self.assertTrue(text_card["rocket"])
        self.assertTrue(image_card["rocket"])
        self.assertTrue(fresh_card["rocket"])
        self.assertTrue(guarantee["rocket"])

    def test_jet_delivery_is_detected(self):
        text_card = classify_card(_card(1, "v1", extra_text="제트배송"))
        image_card = classify_card(
            _card(2, "v2", image_srcs="https://aimg.coupongcdn.com/rds/jet_badge.png")
        )
        self.assertTrue(text_card["jet"])
        self.assertFalse(text_card["rocket"])
        self.assertTrue(image_card["jet"])

    def test_seller_shipping_is_neither(self):
        card = classify_card(
            _card(1, "v1", extra_text="판매자배송 도착 보증 불가 무료배송")
        )
        self.assertFalse(card["rocket"])
        self.assertFalse(card["jet"])
        self.assertEqual(card["delivery_markers"], "")

    def test_review_count_formats(self):
        plain = classify_card(_card(1, "v1", review_text="리뷰 1,234"))
        plus = classify_card(_card(2, "v2", review_text="리뷰 10,000+"))
        parens = classify_card(_card(3, "v3", review_text="4.8 (2,913)"))
        empty = classify_card(_card(4, "v4"))
        self.assertEqual(plain["review_count"], 1234)
        self.assertEqual(plus["review_count"], 10000)
        self.assertEqual(parens["review_count"], 2913)
        self.assertEqual(empty["review_count"], 0)


class RunTopPagesTest(unittest.TestCase):
    def test_saves_only_seller_shipping_products_in_order(self):
        cards = [
            _card(1, "v1", review_text="리뷰 100"),
            _card(2, "v2", extra_text="로켓배송"),
            _card(3, "v3", review_text="리뷰 200"),
            _card(4, "v4", extra_text="제트배송"),
            _card(5, "v5", review_text="리뷰 300"),
        ]
        page = _Page(cards_by_page={("194373", 1): cards})
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            result = run_top_pages(
                output_dir=output_dir,
                page_count=1,
                state_root=root / "guard",
                browser_scope_factory=_factory(page, calls),
            )
            self.assertEqual(result["event"], "top_pages_completed")
            rows = _read_rows(output_dir / PRODUCTS_FILENAME)
            self.assertEqual(
                [row["vendor_item_id"] for row in rows], ["v1", "v3", "v5"]
            )
            self.assertEqual(
                [row["review_count"] for row in rows], ["100", "200", "300"]
            )
            self.assertEqual(result["tp_collected"], 3)
            self.assertEqual(
                result["page_results"][0]["rocket_count"], 1
            )
            self.assertEqual(result["page_results"][0]["jet_count"], 1)

    def test_deduplicates_repeated_products_across_pages(self):
        page = _Page(
            cards_by_page={
                ("194373", 1): [_card(1, "v1", review_text="리뷰 10")],
                ("194373", 2): [
                    _card(1, "v1", review_text="리뷰 10"),
                    _card(2, "v2", review_text="리뷰 20"),
                ],
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            result = run_top_pages(
                output_dir=output_dir,
                page_count=2,
                state_root=root / "guard",
                browser_scope_factory=_factory(page, []),
            )
            self.assertEqual(result["event"], "top_pages_completed")
            rows = _read_rows(output_dir / PRODUCTS_FILENAME)
            self.assertEqual(len(rows), 2)
            self.assertEqual(result["tp_collected"], 2)

    def test_target_reached_writes_sorted_csv_and_stops_category(self):
        full_pages = {
            ("194373", page_number): [
                _card(
                    page_number * 100 + index,
                    f"v{page_number}-{index}",
                    review_text=f"리뷰 {page_number * 100 + index}",
                )
                for index in range(1, 61)
            ]
            for page_number in range(1, 19)
        }
        page = _Page(cards_by_page=full_pages)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            first = run_top_pages(
                output_dir=output_dir,
                page_count=10,
                state_root=root / "guard",
                browser_scope_factory=_factory(page, []),
            )
            self.assertEqual(first["event"], "top_pages_completed")
            _age_last_attempt(root / "guard")
            second = run_top_pages(
                output_dir=output_dir,
                page_count=8,
                state_root=root / "guard",
                browser_scope_factory=_factory(page, []),
            )
            self.assertEqual(second["event"], "top_category_completed")
            rows = _read_rows(output_dir / PRODUCTS_FILENAME)
            self.assertEqual(len(rows), TARGET_CATEGORY_ITEMS)
            top_rows = _read_rows(output_dir / "top_1000_194373.csv")
            self.assertEqual(len(top_rows), TARGET_CATEGORY_ITEMS)
            review_counts = [int(row["review_count"]) for row in top_rows]
            self.assertEqual(review_counts, sorted(review_counts, reverse=True))
            state = json.loads(
                (output_dir / "top_state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                state["categories"]["194373"]["status"], "target_reached"
            )
            self.assertEqual(state["category_index"], 1)
            self.assertEqual(state["page_number"], 1)

    def test_page_cap_marks_category_incomplete_and_continues(self):
        page = _Page(
            cards_by_page={
                ("194688", 50): [_card(1, "w1", review_text="리뷰 5")],
            },
            default_cards=[_card(99, "w-default", review_text="리뷰 1")],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            store = TopThousandStore(output_dir)
            store.ensure_files()
            state = json.loads(
                (output_dir / "top_state.json").read_text(encoding="utf-8")
            )
            state["category_index"] = 1
            state["page_number"] = 50
            state["categories"]["194373"]["status"] = "target_reached"
            state["categories"]["194688"]["status"] = "running"
            (output_dir / "top_state.json").write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8"
            )
            result = run_top_pages(
                output_dir=output_dir,
                page_count=1,
                state_root=root / "guard",
                browser_scope_factory=_factory(page, []),
            )
            self.assertEqual(result["event"], "top_category_completed")
            state = json.loads(
                (output_dir / "top_state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                state["categories"]["194688"]["status"],
                "incomplete_limit_reached",
            )
            self.assertEqual(state["status"], "running")
            self.assertEqual(state["category_index"], 2)
            self.assertEqual(
                state["categories"]["194376"]["status"], "running"
            )

    def test_empty_pages_finish_category_and_continue_to_next(self):
        page = _Page(cards_by_page={})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            store = TopThousandStore(output_dir)
            store.ensure_files()
            state = json.loads(
                (output_dir / "top_state.json").read_text(encoding="utf-8")
            )
            state["category_index"] = 1
            state["categories"]["194373"]["status"] = "target_reached"
            state["categories"]["194688"]["status"] = "running"
            (output_dir / "top_state.json").write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8"
            )
            result = run_top_pages(
                output_dir=output_dir,
                page_count=2,
                state_root=root / "guard",
                browser_scope_factory=_factory(page, []),
            )
            self.assertEqual(result["event"], "top_category_completed")
            state = json.loads(
                (output_dir / "top_state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["status"], "running")
            self.assertEqual(
                state["categories"]["194688"]["status"], "exhausted"
            )
            self.assertEqual(state["category_index"], 2)
            self.assertEqual(
                state["categories"]["194376"]["status"], "running"
            )
            top_rows = _read_rows(output_dir / "top_1000_194688.csv")
            self.assertEqual(top_rows, [])

    def test_blocked_page_records_block_and_keeps_position(self):
        page = _Page(
            cards_by_page={("194373", 1): [_card(1, "v1")]},
            blocked_page="https://www.coupang.com/np/categories/194373?page=1",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            result = run_top_pages(
                output_dir=output_dir,
                page_count=1,
                state_root=root / "guard",
                browser_scope_factory=_factory(page, []),
            )
            self.assertEqual(result["event"], "blocked")
            self.assertEqual(result["blocked_at"], "category")
            guard = json.loads(
                (root / "guard" / "canary_guard.json").read_text(encoding="utf-8")
            )
            self.assertTrue(guard["blocked"])
            rows = _read_rows(output_dir / PRODUCTS_FILENAME)
            self.assertEqual(rows, [])
            state = json.loads(
                (output_dir / "top_state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["page_number"], 1)

    def test_guard_refusal_returns_without_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guard_dir = root / "guard"
            guard_dir.mkdir()
            (guard_dir / "canary_guard.json").write_text(
                json.dumps(
                    {
                        "last_attempt_at": "now",
                        "last_attempt_ts": time.time(),
                        "blocked": False,
                    }
                ),
                encoding="utf-8",
            )
            calls: list[str] = []
            result = run_top_pages(
                output_dir=root / "out",
                page_count=1,
                state_root=guard_dir,
                browser_scope_factory=_factory(_Page(), calls),
            )
            self.assertEqual(result["event"], "guard_refused")
            self.assertEqual(calls, [])

    def test_completed_state_short_circuits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            store = TopThousandStore(output_dir)
            store.ensure_files()
            state = _new_state()
            state["status"] = "completed"
            state["category_index"] = len(TOP_CATEGORIES)
            for record in state["categories"].values():
                record["status"] = "target_reached"
            (output_dir / "top_state.json").write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8"
            )
            calls: list[str] = []
            result = run_top_pages(
                output_dir=output_dir,
                page_count=1,
                state_root=root / "guard",
                browser_scope_factory=_factory(_Page(), calls),
            )
            self.assertEqual(result["event"], "top_collection_complete")
            self.assertEqual(calls, [])

    def test_total_stop_completes_collection_at_final_target(self):
        page = _Page(
            cards_by_page={
                ("194373", 1): [_card(1, "v1", review_text="리뷰 100")],
                ("194373", 2): [_card(2, "v2", review_text="리뷰 200")],
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            store = TopThousandStore(output_dir)
            store.ensure_files()
            state = read_state(output_dir)
            state["stop_when_final"] = {"category_id": "194373", "target": 2}
            (output_dir / "top_state.json").write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8"
            )
            result = run_top_pages(
                output_dir=output_dir,
                page_count=3,
                state_root=root / "guard",
                browser_scope_factory=_factory(page, []),
            )
            self.assertEqual(result["event"], "top_collection_complete")
            self.assertIn("final_datasets", result)
            state = json.loads(
                (output_dir / "top_state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["status"], "completed")
            self.assertTrue(
                all(
                    record["status"] == "skipped"
                    for record in state["categories"].values()
                )
            )
            rows = _read_rows(output_dir / "final_dataset_194373.csv")
            self.assertEqual(
                [row["review_count"] for row in rows], ["200", "100"]
            )

    def test_build_final_dataset_merges_dedupes_and_caps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            store = TopThousandStore(output_dir)
            store.ensure_files()
            # 같은 상품(shared)이 부모와 하위에 반복되고 리뷰 수는 하위가 더 크다.
            rows = [
                _row("194373", "shared", review="100", name="식품/견과/건과"),
                _row(
                    "194376",
                    "shared",
                    review="500",
                    name="식품/견과/건과/땅콩/호두",
                ),
                _row(
                    "194376",
                    "child-only",
                    review="300",
                    name="식품/견과/건과/땅콩/호두",
                ),
                _row(
                    "194688",
                    "other-family",
                    review="999",
                    name="식품/축산/계란/식용곤충",
                ),
            ]
            with mock.patch(
                "app.core.coupang.patchright_top_thousand._read_csv",
                return_value=rows,
            ):
                path, count = build_final_dataset(store, "194373")
            self.assertEqual(count, 2)
            final_rows = _read_rows(path)
            self.assertEqual(len(final_rows), 2)
            self.assertEqual(
                [row["vendor_item_id"] for row in final_rows],
                ["shared", "child-only"],
            )
            self.assertEqual(final_rows[0]["review_count"], "500")
            self.assertIn("땅콩/호두", final_rows[0]["matched_categories"])
            self.assertIn("견과/건과", final_rows[0]["matched_categories"])
            self.assertNotIn(
                "other-family", [row["vendor_item_id"] for row in final_rows]
            )

    def test_combined_final_dataset_has_both_families_uncapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            store = TopThousandStore(output_dir)
            store.ensure_files()
            rows = [
                _row("194373", "a", review="100", name="식품/견과/건과"),
                _row("194688", "b", review="900", name="식품/축산/계란/식용곤충"),
                _row("194690", "b", review="900", name="식품/축산/계란/식용곤충/소고기"),
                _row("194376", "c", review="500", name="식품/견과/건과/땅콩/호두"),
            ]
            with mock.patch(
                "app.core.coupang.patchright_top_thousand._read_csv",
                return_value=rows,
            ):
                path, count = build_combined_final_dataset(store)
            self.assertEqual(count, 3)
            final_rows = _read_rows(path)
            self.assertEqual(len(final_rows), 3)
            self.assertEqual(
                [row["vendor_item_id"] for row in final_rows],
                ["b", "c", "a"],
            )
            self.assertIn(
                "소고기", final_rows[0]["matched_categories"]
            )
            self.assertIn(
                "축산/계란/식용곤충", final_rows[0]["matched_categories"]
            )

    def test_skipped_categories_are_passed_over_on_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            store = TopThousandStore(output_dir)
            store.ensure_files()
            state = _new_state()
            for record in state["categories"].values():
                record["status"] = "skipped"
            state["categories"]["194690"]["status"] = "pending"
            state["category_index"] = 2
            state["status"] = "completed"
            (output_dir / "top_state.json").write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8"
            )
            migrated = read_state(output_dir)
            self.assertEqual(migrated["status"], "running")
            self.assertEqual(migrated["category_index"], 11)
            self.assertEqual(
                migrated["categories"]["194690"]["status"], "running"
            )

    def test_old_two_category_state_extends_to_subcategories(self):
        """상위 카테고리만 있던 시절의 완료 상태를 하위 카테고리로 이어준다."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            store = TopThousandStore(output_dir)
            store.ensure_files()
            legacy = {
                "version": 1,
                "status": "completed",
                "category_index": 2,
                "page_number": 1,
                "consecutive_empty_pages": 0,
                "categories": {
                    "194373": {
                        "category_name": "식품/견과/건과",
                        "status": "exhausted",
                        "raw_seen": 1020,
                        "rocket_seen": 809,
                        "jet_seen": 0,
                        "tp_collected": 201,
                        "pages_scanned": 19,
                    },
                    "194688": {
                        "category_name": "식품/축산/계란/식용곤충",
                        "status": "exhausted",
                        "raw_seen": 1020,
                        "rocket_seen": 963,
                        "jet_seen": 0,
                        "tp_collected": 53,
                        "pages_scanned": 19,
                    },
                },
            }
            (output_dir / "top_state.json").write_text(
                json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
            )
            state = read_state(output_dir)
            self.assertEqual(state["status"], "running")
            self.assertEqual(state["category_index"], 2)
            self.assertEqual(len(state["categories"]), len(TOP_CATEGORIES))
            self.assertEqual(
                state["categories"]["194376"]["status"], "running"
            )
            self.assertEqual(
                state["categories"]["194373"]["tp_collected"], 201
            )
            calls: list[str] = []
            result = run_top_pages(
                output_dir=output_dir,
                page_count=1,
                state_root=root / "guard",
                browser_scope_factory=_factory(_Page(), calls),
            )
            self.assertEqual(result["event"], "top_pages_completed")
            self.assertEqual(result["category_id"], "194376")

    def test_same_product_counts_in_both_parent_and_child_datasets(self):
        shared = _card(1, "shared-1", review_text="리뷰 500")
        page = _Page(
            cards_by_page={
                ("194373", 1): [shared],
                ("194376", 1): [shared, _card(2, "child-2", review_text="리뷰 10")],
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            first = run_top_pages(
                output_dir=output_dir,
                page_count=3,
                state_root=root / "guard",
                browser_scope_factory=_factory(page, []),
            )
            self.assertEqual(first["event"], "top_category_completed")
            _age_last_attempt(root / "guard")
            # 축산 카테고리는 이미 확인했다 치고 견과 하위부터 이어서 확인한다.
            state = read_state(output_dir)
            state["categories"]["194688"]["status"] = "exhausted"
            state["category_index"] = 2
            state["page_number"] = 1
            (output_dir / "top_state.json").write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8"
            )
            second = run_top_pages(
                output_dir=output_dir,
                page_count=3,
                state_root=root / "guard",
                browser_scope_factory=_factory(page, []),
            )
            self.assertEqual(second["event"], "top_category_completed")
            rows = _read_rows(output_dir / PRODUCTS_FILENAME)
            self.assertEqual(len(rows), 3)
            parent_rows = _read_rows(output_dir / "top_1000_194373.csv")
            child_rows = _read_rows(output_dir / "top_1000_194376.csv")
            self.assertEqual(len(parent_rows), 1)
            self.assertEqual(len(child_rows), 2)
            self.assertEqual(
                [row["vendor_item_id"] for row in child_rows],
                ["shared-1", "child-2"],
            )


if __name__ == "__main__":
    unittest.main()
