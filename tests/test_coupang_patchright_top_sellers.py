"""Patchright 상위 데이터셋 판매자 단계 — 실제 네트워크 없음.

병렬 수집 코어 포팅(M1): 파이프라인 작업 선택은 프로토타입 스크립트가
아니라 app.core.coupang.parallel_pipeline 모듈을 직접 검증한다. 프로토타입
CLI(scripts/prototypes/coupang_patchright_top_sellers.py) 테스트는 이
포팅에서 제외했다 — CLI 인자 해석은 향후 매니저(M2) 책임이다.
"""

from __future__ import annotations

import csv
import json
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar

from app.core.base import Control
from app.core.coupang.parallel_pipeline import choose_action
from app.core.coupang.patchright_sample import (
    FETCH_STORE_REVIEW_JS,
    FETCH_VENDORS_JS,
)
from app.core.coupang.patchright_top_sellers import (
    TopSellerStore,
    _seller_row,
    _work,
    authorize_http_503_retry,
    build_seller_final,
    mark_legacy_sellers_for_recheck,
    run_top_seller_batch,
)
from app.core.coupang.patchright_top_thousand import (
    PRODUCT_FIELDS,
    TopThousandStore,
    read_state,
)
from app.core.coupang.seller_detail import (
    EXPAND_SELLER_DETAIL_JS,
    EXTRACT_SELLER_DETAIL_JS,
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
                 seller_error_at="", detail_rows_by_id=None,
                 detail_section=True) -> None:
        self.url = "about:blank"
        self.vendors_by_item = vendors_by_item or {}
        self.sellers_by_id = sellers_by_id or {}
        self.seller_error_at = seller_error_at
        self.detail_rows_by_id = detail_rows_by_id or {}
        self.detail_section = detail_section
        self.current_vendor = ""
        self.review_calls = 0

    def goto(self, url, **_kwargs):
        self.url = str(url)
        vendor_item_id = ""
        if "vendorItemId=" in self.url:
            vendor_item_id = self.url.split("vendorItemId=", 1)[1].split("&", 1)[0]
        self.current_vendor = self.vendors_by_item.get(vendor_item_id, "")
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
        if script == EXTRACT_SELLER_DETAIL_JS:
            if self.current_vendor == self.seller_error_at:
                return {"rows": {}, "hasSection": False, "url": self.url, "title": ""}
            rows = self.detail_rows_by_id.get(self.current_vendor)
            if rows is None:
                payload = self.sellers_by_id.get(self.current_vendor)
                rows = {}
                if payload:
                    if payload.get("name"):
                        rows["company_ceo"] = (
                            f"{payload.get('name', '')} / "
                            f"{payload.get('repPersonName', '')}".strip()
                        )
                    if payload.get("businessNumber"):
                        rows["business_number"] = payload["businessNumber"]
                    if payload.get("repPhoneNum"):
                        rows["phone"] = payload["repPhoneNum"]
                    if payload.get("repEmail"):
                        rows["email"] = payload["repEmail"]
                    if payload.get("repAddr1"):
                        rows["address"] = payload["repAddr1"]
                    if payload.get("eCommerceReportNumber"):
                        rows["ecommerce_report_number"] = payload[
                            "eCommerceReportNumber"
                        ]
            return {
                "rows": rows,
                "hasSection": self.detail_section,
                "url": self.url,
                "title": "",
            }
        if script == EXPAND_SELLER_DETAIL_JS:
            return True
        if script == FETCH_STORE_REVIEW_JS:
            self.review_calls += 1
            if argument == self.seller_error_at:
                return {"status": 200, "body": json.dumps({})}
            payload = self.sellers_by_id.get(argument)
            if payload is None:
                return {"status": 200, "body": json.dumps({})}
            return {"status": 200, "body": json.dumps(payload)}
        raise AssertionError("unexpected script")


class _Context:
    def __init__(self, page: _Page) -> None:
        self.pages = [page]


class _TwoPageContext(_Context):
    def __init__(self, page: _Page, review_page: _Page) -> None:
        super().__init__(page)
        self.review_page = review_page

    def new_page(self):
        return self.review_page


def _two_page_factory(page: _Page, review_page: _Page):
    @contextmanager
    def open_browser(user_data_dir, *, headless=False, proxy=None):
        yield _TwoPageContext(page, review_page)

    return open_browser


def _factory(page: _Page):
    @contextmanager
    def open_browser(user_data_dir, *, headless=False, proxy=None):
        yield _Context(page)

    return open_browser


def _proxy_factory(page: _Page, received: list):
    """팩토리가 받은 proxy 인자를 그대로 기록하는 가짜 브라우저 팩토리."""

    @contextmanager
    def open_browser(user_data_dir, *, headless=False, proxy=None):
        received.append(proxy)
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
            sellers_by_id={
                "A00001": _seller_payload(
                    qualitySellerBadgeDto={"qualityTitle": "파워판매자"},
                )
            },
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
            self.assertEqual(final_rows[0]["전화"], "02-1234-5678")
            self.assertEqual(final_rows[0]["이메일"], "test@example.com")
            self.assertEqual(final_rows[0]["파워셀러"], "True")
            self.assertEqual(final_rows[0]["파워셀러등급"], "파워판매자")
            self.assertNotIn("우수판매자여부", final_rows[0])
            self.assertEqual(final_rows[0]["소속카테고리목록"], "식품/견과/건과")
            self.assertEqual(final_rows[0]["상품수"], "1")
            control = json.loads(
                (output_dir / "top_seller_control.json").read_text("utf-8")
            )
            self.assertEqual(control["status"], "completed")

    def test_review_api_uses_separate_shop_tab(self):
        page = _Page(
            vendors_by_item={"viid-1": "A00001"},
            sellers_by_id={"A00001": _seller_payload()},
        )
        review_page = _Page(
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
                browser_scope_factory=_two_page_factory(page, review_page),
            )
            self.assertEqual(result["event"], "seller_collection_complete")
            self.assertEqual(review_page.review_calls, 1)
            self.assertEqual(page.review_calls, 0)
            sellers = _read_rows(output_dir / "top_sellers.csv")
            self.assertEqual(sellers[0]["status"], "saved")

    def test_missing_detail_section_halts_instead_of_zero_saved(self):
        page = _Page(
            vendors_by_item={"viid-1": "A00001"},
            sellers_by_id={"A00001": _seller_payload()},
            seller_error_at="A00001",
            detail_section=False,
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
            self.assertIn("한 건도 저장하지 못했습니다", str(control["reason"]))
            self.assertFalse((output_dir / "coupang_판매자_0명.csv").exists())

    def test_http_503_retry_requires_three_hours(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            output_dir.mkdir()
            control = {
                "version": 1,
                "status": "halted",
                "reason": "판매자 처리 실패: HTTP 503",
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            (output_dir / "top_seller_control.json").write_text(
                json.dumps(control, ensure_ascii=False), encoding="utf-8"
            )
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
            _prepare(output_dir, [_product_row("viid-1")])
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
            (output_dir / "coupang_판매자_0명.csv").write_text(
                "stale", encoding="utf-8"
            )
            path = build_seller_final(output_dir)
            rows = _read_rows(path)
            self.assertFalse((output_dir / "coupang_판매자_0명.csv").exists())
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["판매자ID"], "A00001")
            self.assertEqual(rows[0]["파워셀러"], "False")
            self.assertEqual(rows[0]["파워셀러등급"], "")
            self.assertEqual(rows[0]["전화"], "p")
            self.assertEqual(rows[0]["이메일"], "e")
            self.assertNotIn("우수판매자여부", rows[0])
            self.assertEqual(path.name, "coupang_판매자_1명.csv")


class CancelResumeTest(unittest.TestCase):
    """사용자 정지·크래시 이후 이어하기 — 2026-10-06 검토 반영.

    정지는 control 을 cancelled 로 남긴다(halted 아님) — 같은 출력 폴더의
    다음 실행이 멈추지 않고 이어서 수집한다.
    """

    def _age_guard(self, state_root: Path) -> None:
        """취소 직전 세션이 남긴 가드 예약을 2시간 전으로 되돌린다."""
        path = state_root / "canary_guard.json"
        guard = json.loads(path.read_text(encoding="utf-8"))
        guard["last_attempt_ts"] = time.time() - 2 * 60 * 60
        path.write_text(json.dumps(guard), encoding="utf-8")

    def test_cancel_leaves_resumable_control_and_next_run_completes(self):
        page = _Page(
            vendors_by_item={"viid-1": "A00001"},
            sellers_by_id={"A00001": _seller_payload()},
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            control = Control()
            control.request_cancel()
            result = run_top_seller_batch(
                output_dir=output_dir,
                limit=10,
                control=control,
                state_root=root / "guard",
                browser_scope_factory=_factory(page),
            )
            self.assertEqual(result["event"], "cancelled")
            control_data = json.loads(
                (output_dir / "top_seller_control.json").read_text("utf-8")
            )
            self.assertEqual(control_data["status"], "cancelled")
            # 다음 실행은 halted 가 아니므로 멈추지 않고 완주한다.
            self.assertEqual(choose_action(output_dir)["action"], "sellers")
            self._age_guard(root / "guard")
            resumed = run_top_seller_batch(
                output_dir=output_dir,
                limit=10,
                state_root=root / "guard",
                browser_scope_factory=_factory(page),
            )
            self.assertEqual(resumed["event"], "seller_collection_complete")
            sellers = _read_rows(output_dir / "top_sellers.csv")
            self.assertEqual(
                [(row["vendor_id"], row["status"]) for row in sellers],
                [("A00001", "saved")],
            )

    def test_stale_in_progress_control_does_not_block_next_run(self):
        """크래시가 남긴 in_progress 도 다음 실행을 막지 않는다."""
        page = _Page(
            vendors_by_item={"viid-1": "A00001"},
            sellers_by_id={"A00001": _seller_payload()},
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            (output_dir / "top_seller_control.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "status": "in_progress",
                        "updated_at": "2026-10-06 09:00:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = run_top_seller_batch(
                output_dir=output_dir,
                limit=10,
                state_root=root / "guard",
                browser_scope_factory=_factory(page),
            )
            self.assertEqual(result["event"], "seller_collection_complete")


class PipelineChooseTest(unittest.TestCase):
    """판매자 우선 선택 — parallel_pipeline.choose_action 이 dict로 돌려준다."""

    def test_chooses_sellers_before_listing_and_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            decision = choose_action(output_dir)
            self.assertEqual(decision["action"], "sellers")
            self.assertIn("판매자", decision["reason"])

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
            decision = choose_action(output_dir)
            self.assertEqual(decision["action"], "complete")
            self.assertIn("끝났습니다", decision["reason"])


class RunTopSellersProxyTest(unittest.TestCase):
    def test_forwards_proxy_dict_to_browser_factory(self):
        page = _Page(
            vendors_by_item={"viid-1": "A00001"},
            sellers_by_id={"A00001": _seller_payload()},
        )
        proxy = {
            "server": "http://gate.decodo.com:7000",
            "username": (
                "user-sp3lqmo64w-session-b01"
                "-sessionduration-1440-country-kr"
            ),
            "password": "secret",
        }
        received: list = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            result = run_top_seller_batch(
                output_dir=output_dir,
                limit=10,
                state_root=root / "guard",
                browser_scope_factory=_proxy_factory(page, received),
                proxy=proxy,
            )
        self.assertEqual(result["event"], "seller_collection_complete")
        self.assertEqual(received, [proxy])

    def test_default_run_passes_proxy_none_to_factory(self):
        page = _Page(
            vendors_by_item={"viid-1": "A00001"},
            sellers_by_id={"A00001": _seller_payload()},
        )
        received: list = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            result = run_top_seller_batch(
                output_dir=output_dir,
                limit=10,
                state_root=root / "guard",
                browser_scope_factory=_proxy_factory(page, received),
            )
        self.assertEqual(result["event"], "seller_collection_complete")
        self.assertEqual(received, [None])


class LegacySellerRecoveryTest(unittest.TestCase):
    """2026-11 이전 no_public_info 행을 한 번만 재조회 대기열로 되돌린다."""

    _VENDOR: ClassVar[dict] = {
        "vendorId": "A00001",
        "displayName": "테스트스토어",
        "productId": 10,
        "itemId": 20,
        "vendorItemId": "viid-1",
    }

    def _write_legacy(self, output_dir: Path) -> None:
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
                    "mapped_at": "2026-11-16 13:12:09",
                }
            ],
        )
        _write_csv(
            output_dir / "top_sellers.csv",
            SELLER_FIELDS,
            [_seller_row("A00001", self._VENDOR, None)],
        )

    def test_legacy_no_public_info_is_recovered_from_detail_page(self):
        page = _Page(sellers_by_id={"A00001": _seller_payload()})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            self._write_legacy(output_dir)
            result = run_top_seller_batch(
                output_dir=output_dir,
                limit=10,
                state_root=root / "guard",
                browser_scope_factory=_factory(page),
            )
            self.assertEqual(result["event"], "seller_collection_complete")
            sellers = _read_rows(output_dir / "top_sellers.csv")
            self.assertEqual(
                [(row["vendor_id"], row["status"]) for row in sellers],
                [("A00001", "saved")],
            )
            self.assertEqual(sellers[0]["company_name"], "주식회사테스트")
            final_rows = _read_rows(output_dir / "coupang_판매자_1명.csv")
            self.assertEqual(final_rows[0]["판매자ID"], "A00001")
            marker = json.loads(
                (output_dir / "seller_detail_recovery.json").read_text("utf-8")
            )
            self.assertEqual(marker["requeued"], 1)
            self.assertEqual(marker["vendor_ids"], ["A00001"])

    def test_recovery_marker_prevents_second_requeue(self):
        from app.core.coupang.parallel_pipeline import seller_recovery_pending

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            self._write_legacy(output_dir)
            store = TopSellerStore(output_dir)
            self.assertTrue(seller_recovery_pending(output_dir))
            first = mark_legacy_sellers_for_recheck(store)
            self.assertEqual(first["requeued"], 1)
            self.assertFalse(seller_recovery_pending(output_dir))
            second = mark_legacy_sellers_for_recheck(store)
            self.assertEqual(second["requeued"], 0)
            rows = _read_rows(output_dir / "top_sellers.csv")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "failed")
            self.assertEqual(
                rows[0]["error"], "상세 페이지 재확인 대기(2026-11 이전 판정)"
            )

    def test_zero_saved_existing_rows_do_not_complete(self):
        page = _Page()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            _prepare(output_dir, [])
            from app.core.coupang.patchright_full_fruit import (
                SELLER_FIELDS,
                _write_csv,
            )

            _write_csv(
                output_dir / "top_sellers.csv",
                SELLER_FIELDS,
                [_seller_row("A00001", self._VENDOR, None)],
            )
            (output_dir / "seller_detail_recovery.json").write_text(
                json.dumps({"version": 1, "requeued": 0}), encoding="utf-8"
            )
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
            self.assertFalse((output_dir / "coupang_판매자_0명.csv").exists())

    def test_blocked_halt_is_not_resumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            (output_dir / "top_seller_control.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "status": "halted",
                        "reason": "상품-판매자 연결 요청에서 차단됐습니다.",
                        "updated_at": "2026-11-17 16:21:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = mark_legacy_sellers_for_recheck(TopSellerStore(output_dir))
            self.assertFalse(result["resumed_halted"])
            from app.core.coupang.parallel_pipeline import seller_recovery_pending

            self.assertFalse(seller_recovery_pending(output_dir))
            control = json.loads(
                (output_dir / "top_seller_control.json").read_text("utf-8")
            )
            self.assertEqual(control["status"], "halted")

    def test_failed_to_fetch_halt_is_resumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            (output_dir / "top_seller_control.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "status": "halted",
                        "reason": "판매자 A00001 처리 실패: Failed to fetch",
                        "updated_at": "2026-11-17 16:21:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = mark_legacy_sellers_for_recheck(TopSellerStore(output_dir))
            self.assertTrue(result["resumed_halted"])
            control = json.loads(
                (output_dir / "top_seller_control.json").read_text("utf-8")
            )
            self.assertEqual(control["status"], "cancelled")


class SellerDetailPayloadTest(unittest.TestCase):
    def test_combined_company_and_representative_are_split(self):
        from app.core.coupang.seller_detail import seller_detail_payload

        data = seller_detail_payload(
            {
                "rows": {
                    "company_ceo": "주식회사테스트 / 홍길동",
                    "business_number": "123-45-67890",
                    "phone": "02-1234-5678",
                    "email": "test@example.com",
                    "address": "서울특별시 강남구 테헤란로 1",
                    "ecommerce_report_number": "2026-서울강남-0001",
                }
            }
        )
        self.assertEqual(data["name"], "주식회사테스트")
        self.assertEqual(data["repPersonName"], "홍길동")
        self.assertEqual(data["businessNumber"], "123-45-67890")
        self.assertEqual(data["repPhoneNum"], "02-1234-5678")
        self.assertEqual(data["repEmail"], "test@example.com")
        self.assertEqual(data["repAddr1"], "서울특별시 강남구 테헤란로 1")
        self.assertEqual(data["eCommerceReportNumber"], "2026-서울강남-0001")


if __name__ == "__main__":
    unittest.main()
