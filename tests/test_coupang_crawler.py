"""Coupang 코어 엔진 테스트 — 네트워크 없이 fake browser/page 사용 (AC-06~13, 18, 21, 22, 24)."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from app.core.base import CancelledError, Control
from app.core.coupang.crawler import CoupangCrawler
from app.core.coupang.exporter import CoupangExporter
from app.models.coupang_records import (
    RECORD_FIELDS,
    CoupangRecord,
    CoupangRunConfig,
    CoupangRunSummary,
)


def _make_config(tmp_dir: str, **kwargs) -> CoupangRunConfig:
    defaults = {
        "output_dir": Path(tmp_dir),
        "output_prefix": "test_output",
        "max_scroll_pages": 3,
        "batch_size": 5,
        "warmup_time": 0,
        "delay_min": 0,
        "delay_max": 0,
    }
    defaults.update(kwargs)
    return CoupangRunConfig(**defaults)


def _promotion_response(items, token=None):
    data = {"ret": "0", "data": {"promotionData": items, "token": token}}
    return {"status": 200, "body": json.dumps(data)}


def _individual_response(products):
    data = {"code": 200, "data": {"products": products}}
    return {"status": 200, "body": json.dumps(data)}


def _store_review_response(name="테스트상호", **kwargs):
    data = {
        "name": name,
        "repPersonName": kwargs.get("ceo", "테스트대표"),
        "businessNumber": kwargs.get("bn", "123-45-67890"),
        "repPhoneNum": kwargs.get("phone", "02-000-0000"),
        "repEmail": kwargs.get("email", "test@example.com"),
        "repAddr1": kwargs.get("addr1", "서울시"),
        "repAddr2": kwargs.get("addr2", "테스트동"),
        "eCommerceReportNumber": kwargs.get("ecomm", "2024-서울테스트-0001"),
        "qualitySellerBadgeDto": kwargs.get("badge"),
        "ratingCount": kwargs.get("rating", 100),
        "thumbUpRatio": kwargs.get("thumbup", 95.5),
    }
    return {"status": 200, "body": json.dumps(data)}


class FakeMouse:
    def move(self, x, y):
        pass

    def wheel(self, x, y):
        pass


class FakePage:
    """Fake page that returns scripted responses based on URL patterns."""

    def __init__(self, promotion_pages=None, vendors_response=None, review_responses=None):
        self.mouse = FakeMouse()
        self._request_handlers = []
        self._promotion_pages = promotion_pages or []
        self._promotion_call = 0
        self._vendors_response = vendors_response
        self._review_responses = review_responses or {}
        self._evaluate_calls = []

    def on(self, event, handler):
        self._request_handlers.append(handler)

    def goto(self, url, **kwargs):
        if "getPromotion" in str(url) or "/np/omp" in str(url):
            req = MagicMock()
            req.url = "https://www.coupang.com/np/omp/api/getPromotion"
            req.post_data = json.dumps({"query": {"feedId": "test_feed", "continuationToken": "seemore=CGs="}})
            for h in self._request_handlers:
                h(req)

    def evaluate(self, script, *args):
        self._evaluate_calls.append((script, args))
        if "getPromotion" in script:
            if self._promotion_call < len(self._promotion_pages):
                resp = self._promotion_pages[self._promotion_call]
                self._promotion_call += 1
                return resp
            return _promotion_response([], None)
        if "individualInfo" in script:
            return self._vendors_response or _individual_response([])
        if "getStoreReview" in script:
            vid = args[0] if args else ""
            return self._review_responses.get(vid, {"status": 500, "body": ""})
        return {}

    def close(self):
        pass


class FakeBrowser:
    def __init__(self, page):
        self._page = page
        self.closed = False

    def new_page(self):
        return self._page

    def close(self):
        self.closed = True


def _make_items(n, start=1):
    return [{"vendorItemId": f"VI{start + i}", "itemId": f"I{start + i}", "title": f"상품{start + i}", "categoryId": "1"} for i in range(n)]


def _make_products(viids, vendor_prefix="V"):
    products = []
    for i, viid in enumerate(viids):
        products.append({
            "productId": f"P{i}",
            "itemId": f"I{i}",
            "vendorItemId": viid,
            "storeInfoArea": {
                "vendorId": f"{vendor_prefix}{i}",
                "storeId": 109671,
                "displayName": f"스토어{i}",
            },
        })
    return products


class CrawlerPipelineTest(unittest.TestCase):
    """AC-07, AC-08, AC-09, AC-10: 기본 파이프라인 동작."""

    def _run_crawler(self, page, tmp_dir, **config_kwargs):
        config = _make_config(tmp_dir, **config_kwargs)
        control = Control()
        browser = FakeBrowser(page)
        crawler = CoupangCrawler(
            config=config,
            control=control,
            browser_factory=lambda: browser,
        )
        return crawler.run(), browser

    def test_successful_pipeline_produces_records(self):
        items = _make_items(3)
        products = _make_products(["VI1", "VI2", "VI3"])
        reviews = {
            "V0": _store_review_response("상호0", badge={"qualityTitle": "파워셀러"}),
            "V1": _store_review_response("상호1"),
            "V2": _store_review_response("상호2"),
        }
        page = FakePage(
            promotion_pages=[_promotion_response(items, None)],
            vendors_response=_individual_response(products),
            review_responses=reviews,
        )
        with tempfile.TemporaryDirectory() as tmp:
            summary, browser = self._run_crawler(page, tmp)

        self.assertEqual(summary.business_info_success, 3)
        self.assertEqual(summary.products_seen, 3)
        self.assertEqual(summary.unique_vendors, 3)
        self.assertEqual(summary.power_sellers, 1)
        self.assertFalse(summary.cancelled)
        self.assertIsNone(summary.error)
        self.assertTrue(browser.closed)
        self.assertEqual(len(summary.records), 3)

    def test_record_fields_match_schema(self):
        """AC-08: 14 fields exactly."""
        items = _make_items(1)
        products = _make_products(["VI1"])
        reviews = {"V0": _store_review_response()}
        page = FakePage(
            promotion_pages=[_promotion_response(items, None)],
            vendors_response=_individual_response(products),
            review_responses=reviews,
        )
        with tempfile.TemporaryDirectory() as tmp:
            summary, _ = self._run_crawler(page, tmp)

        rec = summary.records[0]
        self.assertEqual(list(rec.keys()), list(RECORD_FIELDS))

    def test_ret_string_zero_is_success(self):
        """AC-07: str(ret) == '0' check."""
        items = _make_items(1)
        resp = {"status": 200, "body": json.dumps({"ret": 0, "data": {"promotionData": items, "token": None}})}
        products = _make_products(["VI1"])
        page = FakePage(
            promotion_pages=[resp],
            vendors_response=_individual_response(products),
            review_responses={"V0": _store_review_response()},
        )
        with tempfile.TemporaryDirectory() as tmp:
            summary, _ = self._run_crawler(page, tmp)
        self.assertEqual(summary.business_info_success, 1)

    def test_cursor_pagination_and_termination(self):
        """AC-07: cursor update and termination conditions."""
        page1_items = _make_items(2, start=1)
        page2_items = _make_items(2, start=3)
        pages = [
            _promotion_response(page1_items, "token2"),
            _promotion_response(page2_items, None),
        ]
        products = _make_products(["VI1", "VI2", "VI3", "VI4"])
        reviews = {f"V{i}": _store_review_response(f"상호{i}") for i in range(4)}
        page = FakePage(
            promotion_pages=pages,
            vendors_response=_individual_response(products),
            review_responses=reviews,
        )
        with tempfile.TemporaryDirectory() as tmp:
            summary, _ = self._run_crawler(page, tmp)
        self.assertEqual(summary.products_seen, 4)
        self.assertIn(summary.termination_reason, ("token_exhausted", "no_new_items"))

    def test_vendorItemId_dedup(self):
        """AC-09: duplicate vendorItemIds within a run are removed."""
        items = _make_items(3) + [_make_items(3)[0]]  # duplicate VI1
        products = _make_products(["VI1", "VI2", "VI3"])
        reviews = {f"V{i}": _store_review_response() for i in range(3)}
        page = FakePage(
            promotion_pages=[_promotion_response(items, None)],
            vendors_response=_individual_response(products),
            review_responses=reviews,
        )
        with tempfile.TemporaryDirectory() as tmp:
            summary, _ = self._run_crawler(page, tmp)
        self.assertEqual(summary.products_seen, 3)

    def test_vendorId_dedup(self):
        """AC-09: duplicate vendorIds are merged."""
        items = _make_items(4)
        products = [
            {"productId": "P0", "itemId": "I0", "vendorItemId": "VI1",
             "storeInfoArea": {"vendorId": "V0", "storeId": 1, "displayName": "S0"}},
            {"productId": "P1", "itemId": "I1", "vendorItemId": "VI2",
             "storeInfoArea": {"vendorId": "V0", "storeId": 1, "displayName": "S0"}},
            {"productId": "P2", "itemId": "I2", "vendorItemId": "VI3",
             "storeInfoArea": {"vendorId": "V1", "storeId": 1, "displayName": "S1"}},
            {"productId": "P3", "itemId": "I3", "vendorItemId": "VI4",
             "storeInfoArea": {"vendorId": "V1", "storeId": 1, "displayName": "S1"}},
        ]
        reviews = {"V0": _store_review_response(), "V1": _store_review_response("상호1")}
        page = FakePage(
            promotion_pages=[_promotion_response(items, None)],
            vendors_response=_individual_response(products),
            review_responses=reviews,
        )
        with tempfile.TemporaryDirectory() as tmp:
            summary, _ = self._run_crawler(page, tmp)
        self.assertEqual(summary.unique_vendors, 2)
        self.assertEqual(summary.business_info_success, 2)

    def test_brand_seller_skipped_not_error(self):
        """AC-10: BrandSeller null → skip count, not error."""
        items = _make_items(2)
        products = _make_products(["VI1", "VI2"])
        reviews = {
            "V0": _store_review_response(),
            "V1": {"status": 200, "body": json.dumps({"name": None})},
        }
        page = FakePage(
            promotion_pages=[_promotion_response(items, None)],
            vendors_response=_individual_response(products),
            review_responses=reviews,
        )
        with tempfile.TemporaryDirectory() as tmp:
            summary, _ = self._run_crawler(page, tmp)
        self.assertEqual(summary.business_info_success, 1)
        self.assertEqual(summary.brand_seller_skipped, 1)
        self.assertEqual(summary.request_errors, 0)

    def test_request_error_counted(self):
        """AC-10: request errors counted separately."""
        items = _make_items(2)
        products = _make_products(["VI1", "VI2"])
        reviews = {
            "V0": _store_review_response(),
            "V1": {"status": 500, "body": ""},
        }
        page = FakePage(
            promotion_pages=[_promotion_response(items, None)],
            vendors_response=_individual_response(products),
            review_responses=reviews,
        )
        with tempfile.TemporaryDirectory() as tmp:
            summary, _ = self._run_crawler(page, tmp)
        self.assertEqual(summary.business_info_success, 1)
        self.assertEqual(summary.request_errors, 1)

    def test_url_construction(self):
        """AC-08: representative product URL."""
        items = _make_items(1)
        products = [{
            "productId": "12345", "itemId": "678", "vendorItemId": "VI1",
            "storeInfoArea": {"vendorId": "V0", "storeId": 1, "displayName": "S"},
        }]
        reviews = {"V0": _store_review_response()}
        page = FakePage(
            promotion_pages=[_promotion_response(items, None)],
            vendors_response=_individual_response(products),
            review_responses=reviews,
        )
        with tempfile.TemporaryDirectory() as tmp:
            summary, _ = self._run_crawler(page, tmp)
        self.assertEqual(
            summary.records[0]["url"],
            "https://www.coupang.com/vp/products/12345?itemId=678&vendorItemId=VI1",
        )


class CrawlerTerminationTest(unittest.TestCase):
    """AC-22: termination reasons and exit behavior."""

    def _run(self, page, tmp_dir, **kw):
        config = _make_config(tmp_dir, **kw)
        control = Control()
        browser = FakeBrowser(page)
        crawler = CoupangCrawler(config=config, control=control, browser_factory=lambda: browser)
        return crawler.run()

    def test_template_not_captured(self):
        page = FakePage(promotion_pages=[])
        page._request_handlers = []  # no handler will fire
        # Override goto to not trigger request capture
        page.goto = lambda url, **kw: None
        with tempfile.TemporaryDirectory() as tmp:
            summary = self._run(page, tmp)
        self.assertEqual(summary.termination_reason, "template_not_captured")
        self.assertIsNotNone(summary.error)

    def test_no_items(self):
        page = FakePage(promotion_pages=[_promotion_response([], None)])
        with tempfile.TemporaryDirectory() as tmp:
            summary = self._run(page, tmp)
        self.assertEqual(summary.termination_reason, "no_items")

    def test_page_limit(self):
        pages = [_promotion_response(_make_items(3, start=i * 3 + 1), f"tok{i}") for i in range(5)]
        page = FakePage(promotion_pages=pages)
        all_viids = [f"VI{i}" for i in range(1, 7)]
        products = _make_products(all_viids)
        page._vendors_response = _individual_response(products)
        page._review_responses = {f"V{i}": _store_review_response() for i in range(6)}
        with tempfile.TemporaryDirectory() as tmp:
            summary = self._run(page, tmp, max_scroll_pages=2)
        self.assertEqual(summary.termination_reason, "page_limit")


class CrawlerCancelTest(unittest.TestCase):
    """AC-12, AC-13: cancel behavior and partial save."""

    def test_cancel_during_business_info_saves_partial(self):
        items = _make_items(3)
        products = _make_products(["VI1", "VI2", "VI3"])
        reviews = {f"V{i}": _store_review_response(f"상호{i}") for i in range(3)}
        page = FakePage(
            promotion_pages=[_promotion_response(items, None)],
            vendors_response=_individual_response(products),
            review_responses=reviews,
        )
        control = Control()
        config = _make_config("/unused", delay_min=0, delay_max=0)

        call_count = [0]
        orig_evaluate = page.evaluate

        def cancel_after_first_review(script, *args):
            result = orig_evaluate(script, *args)
            if "getStoreReview" in script:
                call_count[0] += 1
                if call_count[0] == 1:
                    control.request_cancel()
            return result

        page.evaluate = cancel_after_first_review

        with tempfile.TemporaryDirectory() as tmp:
            config.output_dir = Path(tmp)
            browser = FakeBrowser(page)
            crawler = CoupangCrawler(config=config, control=control, browser_factory=lambda: browser)
            summary = crawler.run()

        self.assertTrue(summary.cancelled)
        self.assertEqual(summary.termination_reason, "cancelled")
        self.assertTrue(browser.closed)
        self.assertGreaterEqual(len(summary.records), 1)
        self.assertIsNotNone(summary.json_path)
        self.assertIn("_partial", summary.json_path)

    def test_browser_closed_on_all_paths(self):
        """AC-12: browser closed on success, cancel, and error."""
        items = _make_items(1)
        products = _make_products(["VI1"])
        reviews = {"V0": _store_review_response()}
        page = FakePage(
            promotion_pages=[_promotion_response(items, None)],
            vendors_response=_individual_response(products),
            review_responses=reviews,
        )
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            browser = FakeBrowser(page)
            crawler = CoupangCrawler(config=config, control=Control(), browser_factory=lambda: browser)
            crawler.run()
        self.assertTrue(browser.closed)


class CrawlerThreadTest(unittest.TestCase):
    """AC-06: engine runs in worker thread, not UI thread."""

    def test_browser_factory_called_in_worker_thread(self):
        items = _make_items(1)
        products = _make_products(["VI1"])
        reviews = {"V0": _store_review_response()}
        page = FakePage(
            promotion_pages=[_promotion_response(items, None)],
            vendors_response=_individual_response(products),
            review_responses=reviews,
        )

        factory_thread_ids = []
        main_thread_id = threading.current_thread().ident

        def factory():
            factory_thread_ids.append(threading.current_thread().ident)
            return FakeBrowser(page)

        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            control = Control()
            result_holder = [None]

            def run_in_thread():
                crawler = CoupangCrawler(config=config, control=control, browser_factory=factory)
                result_holder[0] = crawler.run()

            t = threading.Thread(target=run_in_thread)
            t.start()
            t.join(timeout=10)

        self.assertEqual(len(factory_thread_ids), 1)
        self.assertNotEqual(factory_thread_ids[0], main_thread_id)


class StoreNameCoverageTest(unittest.TestCase):
    """AC-21: store_name coverage calculation."""

    def test_coverage_with_mixed_store_names(self):
        summary = CoupangRunSummary(
            business_info_success=4,
            store_name_present=3,
            store_name_missing=1,
        )
        self.assertAlmostEqual(summary.store_name_coverage, 0.75)

    def test_coverage_zero_success(self):
        summary = CoupangRunSummary(business_info_success=0)
        self.assertEqual(summary.store_name_coverage, 0.0)


class DuplicateBusinessNumberTest(unittest.TestCase):
    """AC-24: duplicate_business_numbers_observed calculation."""

    def test_hyphen_variants_same_number(self):
        records = [
            {"vendor_id": "V1", "business_number": "123-45-67890"},
            {"vendor_id": "V2", "business_number": "1234567890"},
        ]
        count = CoupangCrawler._count_duplicate_business_numbers(records)
        self.assertEqual(count, 1)

    def test_three_vendors_same_number(self):
        records = [
            {"vendor_id": "V1", "business_number": "123-45-67890"},
            {"vendor_id": "V2", "business_number": "1234567890"},
            {"vendor_id": "V3", "business_number": "123-45-67890"},
        ]
        count = CoupangCrawler._count_duplicate_business_numbers(records)
        self.assertEqual(count, 1)

    def test_two_different_duplicates(self):
        records = [
            {"vendor_id": "V1", "business_number": "111-11-11111"},
            {"vendor_id": "V2", "business_number": "1111111111"},
            {"vendor_id": "V3", "business_number": "222-22-22222"},
            {"vendor_id": "V4", "business_number": "2222222222"},
        ]
        count = CoupangCrawler._count_duplicate_business_numbers(records)
        self.assertEqual(count, 2)

    def test_invalid_length_excluded(self):
        records = [
            {"vendor_id": "V1", "business_number": "12345"},
            {"vendor_id": "V2", "business_number": "12345"},
            {"vendor_id": "V3", "business_number": ""},
        ]
        count = CoupangCrawler._count_duplicate_business_numbers(records)
        self.assertEqual(count, 0)

    def test_no_duplicates(self):
        records = [
            {"vendor_id": "V1", "business_number": "111-11-11111"},
            {"vendor_id": "V2", "business_number": "222-22-22222"},
        ]
        count = CoupangCrawler._count_duplicate_business_numbers(records)
        self.assertEqual(count, 0)


class SecretSentinelTest(unittest.TestCase):
    """AC-18: secrets must not appear in logs or exports."""

    def test_no_secrets_in_logs(self):
        logs = []
        items = _make_items(1)
        products = _make_products(["VI1"])
        reviews = {"V0": _store_review_response()}
        page = FakePage(
            promotion_pages=[_promotion_response(items, None)],
            vendors_response=_individual_response(products),
            review_responses=reviews,
        )
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            crawler = CoupangCrawler(
                config=config,
                control=Control(),
                browser_factory=lambda: FakeBrowser(page),
                on_log=lambda msg: logs.append(msg),
            )
            crawler.run()

        all_logs = "\n".join(logs)
        self.assertNotIn("SECRET_COOKIE_SENTINEL", all_logs)
        self.assertNotIn("SECRET_TEMPLATE_SENTINEL", all_logs)


class ExporterTest(unittest.TestCase):
    """AC-11: JSON/CSV encoding, header, atomic write."""

    def test_json_utf8_csv_utf8sig(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            exporter = CoupangExporter(config)
            records = [CoupangRecord(vendor_id="V1", company_name="테스트").to_dict()]
            json_path, csv_path = exporter.save(records)

            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data[0]["company_name"], "테스트")

            with open(csv_path, "rb") as f:
                raw = f.read()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))  # BOM

    def test_csv_header_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            exporter = CoupangExporter(config)
            records = [CoupangRecord(vendor_id="V1").to_dict()]
            _, csv_path = exporter.save(records)

            with open(csv_path, "r", encoding="utf-8-sig") as f:
                header = f.readline().strip()
            self.assertEqual(header, ",".join(RECORD_FIELDS))

    def test_partial_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            exporter = CoupangExporter(config)
            records = [CoupangRecord(vendor_id="V1").to_dict()]
            json_path, csv_path = exporter.save(records, partial=True)
            self.assertIn("_partial", json_path)
            self.assertIn("_partial", csv_path)

    def test_empty_records_final_creates_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            exporter = CoupangExporter(config)
            json_path, csv_path = exporter.save([], partial=False)
            self.assertTrue(os.path.exists(json_path))
            self.assertTrue(os.path.exists(csv_path))
            with open(json_path, "r", encoding="utf-8") as f:
                self.assertEqual(json.load(f), [])

    def test_empty_partial_no_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            exporter = CoupangExporter(config)
            json_path, csv_path = exporter.save([], partial=True)
            self.assertIsNone(json_path)
            self.assertIsNone(csv_path)

    def test_atomic_no_tmp_leftover(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            exporter = CoupangExporter(config)
            records = [CoupangRecord(vendor_id="V1").to_dict()]
            exporter.save(records)
            files = os.listdir(tmp)
            self.assertFalse(any(f.endswith(".tmp") for f in files))


class ConfigValidationTest(unittest.TestCase):
    """AC-05: config validation."""

    def test_invalid_max_pages(self):
        with self.assertRaises(ValueError):
            CoupangRunConfig(output_dir=Path("/tmp"), max_scroll_pages=0)

    def test_invalid_delay_range(self):
        with self.assertRaises(ValueError):
            CoupangRunConfig(output_dir=Path("/tmp"), delay_min=3.0, delay_max=1.0)

    def test_path_separator_in_prefix(self):
        with self.assertRaises(ValueError):
            CoupangRunConfig(output_dir=Path("/tmp"), output_prefix="foo/bar")

    def test_empty_prefix_becomes_none(self):
        c = CoupangRunConfig(output_dir=Path("/tmp"), output_prefix="  ")
        self.assertIsNone(c.output_prefix)


class ControlSleepTest(unittest.TestCase):
    """Control.sleep interruptible behavior."""

    def test_sleep_completes(self):
        ctrl = Control()
        ctrl.sleep(0.05)

    def test_sleep_cancel_raises(self):
        ctrl = Control()
        ctrl.request_cancel()
        with self.assertRaises(CancelledError):
            ctrl.sleep(5.0)

    def test_sleep_pause_then_resume(self):
        ctrl = Control()
        import time

        ctrl.pause()

        def resume_later():
            time.sleep(0.1)
            ctrl.resume()

        t = threading.Thread(target=resume_later)
        t.start()
        ctrl.sleep(0.05)
        t.join()


if __name__ == "__main__":
    unittest.main()
