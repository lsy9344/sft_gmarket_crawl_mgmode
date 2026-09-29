"""Adversarial stress harness for Challenger 2: Fallback & Boundary Challenger.

This test suite aggressively probes:
1. Resource loading under corrupted, unreadable, permission-denied, or schema-invalid conditions.
2. Missing proxy credentials dialog cancellations, escape/close aborts, settings redirects, and local safe mode execution.
3. 0-item listings, early streak termination, disk pollution prevention (ensuring no empty CSV/JSON files are created).
4. 17-column CSV/JSON dataset schema integrity and 7 mandatory business fields compliance across edge-case mtop payloads.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtWidgets import QApplication, QMessageBox, QWidget
    _QT_OK = True
except Exception:
    _QT_OK = False

if _QT_OK:
    from app.core import config, decodo
    from app.core.aliexpress_category_crawler import (
        COUPANG_DATASET_FIELDS,
        AliexpressCategoryCrawler,
        AliexpressCategoryRunConfig,
        AliexpressCrawlSummary,
    )
    from app.core.aliexpress_resume_store import (
        AliexpressResumeStore,
        ResumeStoreError,
        ali_category_run_dir,
        peek_resume,
    )
    from app.core.base import CancelledError, Control
    from app.ui.aliexpress_category_panel import AliexpressCategoryPanel
    from app.ui.main_window import MainWindow
    from app.workers.aliexpress_category_crawl_worker import AliexpressCategoryCrawlWorker

_app = None


def setUpModule():
    global _app
    if _QT_OK:
        _app = QApplication.instance() or QApplication(sys.argv)


class FakePageForAdversarial:
    def __init__(self, items_by_page: dict[int, list[dict]] | None = None, mtop_by_item: dict[str, dict] | None = None):
        self.items_by_page = items_by_page or {}
        self.mtop_by_item = mtop_by_item or {}
        self.visited_urls: list[str] = []
        self._listeners: dict[str, list] = {}
        self.url = ""
        self.closed = False

    async def goto(self, url: str, **kwargs):
        self.visited_urls.append(url)
        self.url = url
        for item_id, payload in self.mtop_by_item.items():
            if f"/item/{item_id}.html" in url:
                class FakeMtopResponse:
                    def __init__(self, p):
                        self.url = "https://acs.aliexpress.com/h5/mtop.aliexpress.pdp.pc.query/1.0/"
                        self.status = 200
                        self._payload = p

                    async def body(self):
                        return json.dumps(self._payload).encode("utf-8")

                for handler in list(self._listeners.get("response", [])):
                    await handler(FakeMtopResponse(payload))
                break

    async def wait_for_timeout(self, ms: int):
        pass

    async def evaluate(self, script: str, *args):
        if "document.querySelectorAll" in script:
            curr = self.visited_urls[-1] if self.visited_urls else ""
            p_num = 1
            if "page=" in curr:
                try:
                    p_num = int(curr.split("page=")[-1].split("&")[0])
                except Exception:
                    p_num = 1
            return self.items_by_page.get(p_num, [])
        return None

    def on(self, event: str, handler):
        self._listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler):
        if event in self._listeners and handler in self._listeners[event]:
            self._listeners[event].remove(handler)

    async def close(self):
        self.closed = True


class FakeBrowserContext:
    def __init__(self, page: FakePageForAdversarial):
        self._page = page
        self._cdp = FakeCdpSession()

    async def add_cookies(self, cookies):
        pass

    async def new_page(self):
        return self._page

    async def new_cdp_session(self, page):
        return self._cdp

    async def close(self):
        pass


class FakeCdpSession:
    async def send(self, method: str, params=None):
        pass

    def on(self, event: str, handler):
        pass


class FakePlaywrightBrowser:
    def __init__(self, context: FakeBrowserContext):
        self._context = context

    async def new_context(self, **kwargs):
        return self._context

    async def close(self):
        pass


class FakePlaywrightLauncher:
    def __init__(self, page: FakePageForAdversarial):
        self._page = page
        self.chromium = MagicMock()
        self.launch_kwargs = None

        async def launch(**kwargs):
            self.launch_kwargs = kwargs
            return FakePlaywrightBrowser(FakeBrowserContext(self._page))

        self.chromium.launch = launch

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


# =====================================================================
# Challenge Area 1: Resource Loading Under Hostile Conditions
# =====================================================================
@unittest.skipUnless(_QT_OK, "PyQt6 required")
class TestAdversarialResourceLoading(unittest.TestCase):
    """Aggressively stress-test category tree resource resolution and fallbacks."""

    def setUp(self):
        self.panel = AliexpressCategoryPanel()

    def tearDown(self):
        self.panel.deleteLater()

    def test_tree_file_permission_denied_triggers_graceful_fallback(self):
        """Simulate unreadable tree file (PermissionError)."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "app" / "resources" / "aliexpress_category_tree.json"
            f.parent.mkdir(parents=True)
            f.write_text(json.dumps([{"name": "비공개"}]), encoding="utf-8")
            
            # Windows chmod does not deny reads; inject the same OS error on
            # every platform so this checks the fallback, not permission bits.
            with patch("sys._MEIPASS", tmp, create=True), patch(
                "builtins.open", side_effect=PermissionError("test read denied")
            ):
                self.panel.category_tree.clear()
                self.panel._load_category_tree()

                self.assertEqual(self.panel.input_tabs.currentIndex(), 1)
                self.assertIn("기본 카테고리 트리를 불러올 수 없습니다", self.panel.log_text.toPlainText())

    def test_tree_file_binary_corrupted_encoding_triggers_fallback(self):
        """Simulate raw invalid byte stream (UnicodeDecodeError)."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "app" / "resources" / "aliexpress_category_tree.json"
            f.parent.mkdir(parents=True)
            f.write_bytes(b"\xff\xfe\x00\x00\x80\x90\xff\xee\xdd")

            with patch("sys._MEIPASS", tmp, create=True):
                self.panel.category_tree.clear()
                self.panel._load_category_tree()
                self.assertEqual(self.panel.input_tabs.currentIndex(), 1)
                self.assertIn("기본 카테고리 트리를 불러올 수 없습니다", self.panel.log_text.toPlainText())

    def test_tree_file_with_dict_instead_of_list_triggers_fallback(self):
        """Simulate JSON dictionary instead of list (schema violation)."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "app" / "resources" / "aliexpress_category_tree.json"
            f.parent.mkdir(parents=True)
            f.write_text(json.dumps({"name": "잘못된루트", "type": "dict"}), encoding="utf-8")

            with patch("sys._MEIPASS", tmp, create=True):
                self.panel.category_tree.clear()
                self.panel._load_category_tree()
                self.assertEqual(self.panel.input_tabs.currentIndex(), 1)
                self.assertIn("카테고리 트리가 비어 있거나 올바르지 않습니다", self.panel.log_text.toPlainText())

    def test_tree_file_with_heterogeneous_null_or_int_elements_is_safe(self):
        """Simulate JSON list with corrupted non-dict entries [None, 123, 'str']."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "app" / "resources" / "aliexpress_category_tree.json"
            f.parent.mkdir(parents=True)
            f.write_text(json.dumps([None, 123, "invalid"]), encoding="utf-8")

            with patch("sys._MEIPASS", tmp, create=True):
                self.panel.category_tree.clear()
                self.panel._load_category_tree()
                # Should safely catch AttributeError and switch to manual input
                self.assertEqual(self.panel.input_tabs.currentIndex(), 1)
                self.assertIn("기본 카테고리 트리를 불러올 수 없습니다", self.panel.log_text.toPlainText())

    def test_manual_url_input_fully_functional_after_fallback(self):
        """After fallback to tab 1, user can input URL and emit start_requested."""
        self.panel.input_tabs.setCurrentIndex(1)
        self.panel.input_direct_name.setText("수동카테고리")
        self.panel.input_direct_url.setText("https://ko.aliexpress.com/category/123/manual.html")

        emitted_configs = []
        self.panel.start_requested.connect(lambda cfg: emitted_configs.append(cfg))
        self.panel._on_start_clicked()

        self.assertEqual(len(emitted_configs), 1)
        cfg = emitted_configs[0]
        self.assertEqual(cfg.category_name, "수동카테고리")
        self.assertEqual(cfg.category_url, "https://ko.aliexpress.com/category/123/manual.html")


# =====================================================================
# Challenge Area 2: Missing Proxy Credentials Dialog & Mode Transitions
# =====================================================================
@unittest.skipUnless(_QT_OK, "PyQt6 required")
class TestAdversarialProxyCredentialsFlow(unittest.TestCase):
    """Stress-test proxy preflight checks, dialog actions, and crawler launch arguments."""

    def setUp(self):
        self.win = MainWindow()

    def tearDown(self):
        self.win.alicat_worker = None
        self.win.close()

    def test_proxy_missing_local_safe_mode_launches_without_proxy(self):
        """Selecting [로컬 회선으로 안전 수집] configures use_proxy=False and delay=3.5."""
        cfg = AliexpressCategoryRunConfig(
            output_dir=Path("/tmp/ali_test_out"),
            category_name="로컬테스트",
            category_url="https://ko.aliexpress.com/w/local.html",
        )

        def click_local(box_self):
            for btn in box_self.buttons():
                if "로컬 회선" in btn.text():
                    box_self._mock_choice = btn
                    return

        with patch("app.core.decodo.load_settings", return_value=decodo.DecodoSettings(username="", password="")), \
             patch.object(QMessageBox, "exec", click_local), \
             patch.object(QMessageBox, "clickedButton", lambda b: getattr(b, "_mock_choice", None)), \
             patch.object(AliexpressCategoryCrawlWorker, "start"):
            self.win.on_alicat_start(cfg)

        worker = self.win.alicat_worker
        self.assertIsNotNone(worker)
        self.assertFalse(worker.config.use_proxy)
        self.assertEqual(worker.config.delay, 3.5)
        self.win.alicat_worker = None

    def test_proxy_missing_settings_tab_redirect_does_not_launch_worker(self):
        """Selecting [설정 탭으로 이동] switches tab without launching worker."""
        cfg = AliexpressCategoryRunConfig(
            output_dir=Path("/tmp/ali_test_out"),
            category_name="설정테스트",
            category_url="https://ko.aliexpress.com/w/settings.html",
        )

        def click_settings(box_self):
            for btn in box_self.buttons():
                if "설정 탭" in btn.text():
                    box_self._mock_choice = btn
                    return

        with patch("app.core.decodo.load_settings", return_value=decodo.DecodoSettings(username="", password="")), \
             patch.object(QMessageBox, "exec", click_settings), \
             patch.object(QMessageBox, "clickedButton", lambda b: getattr(b, "_mock_choice", None)):
            self.win.on_alicat_start(cfg)

        self.assertIsNone(self.win.alicat_worker)
        self.assertEqual(self.win.tab_widget.currentWidget(), self.win.brightdata_panel)
        self.assertEqual(self.win.aliexpress_category_panel._state, "idle")

    def test_proxy_dialog_window_close_or_escape_aborts_without_side_effects(self):
        """Escaping or closing the proxy dialog aborts start cleanly."""
        cfg = AliexpressCategoryRunConfig(
            output_dir=Path("/tmp/ali_test_out"),
            category_name="취소테스트",
            category_url="https://ko.aliexpress.com/w/cancel.html",
        )

        with patch("app.core.decodo.load_settings", return_value=decodo.DecodoSettings(username="", password="")), \
             patch.object(QMessageBox, "exec", lambda b: None), \
             patch.object(QMessageBox, "clickedButton", lambda b: None):
            self.win.on_alicat_start(cfg)

        self.assertIsNone(self.win.alicat_worker)
        self.assertEqual(self.win.aliexpress_category_panel._state, "idle")

    def test_duplicate_worker_start_blocked_when_another_tab_active(self):
        """Attempting to start when another worker is active triggers warning and blocks."""
        cfg = AliexpressCategoryRunConfig(
            output_dir=Path("/tmp/ali_test_out"),
            category_name="중복테스트",
            category_url="https://ko.aliexpress.com/w/dup.html",
        )
        fake_active = MagicMock()
        with patch.object(self.win, "_active_worker", return_value=fake_active), \
             patch.object(QMessageBox, "warning") as mock_warn:
            self.win.on_alicat_start(cfg)
            mock_warn.assert_called_once()
            self.assertIsNone(self.win.alicat_worker)


# =====================================================================
# Challenge Area 3: 0-Item Listings & Disk Pollution Prevention
# =====================================================================
class TestAdversarialZeroItemListings(unittest.TestCase):
    """Stress-test 0-item listings, early termination, and verify NO empty CSV/JSON files."""

    def setUp(self):
        # 환경에 decodo_settings.json 이 없어도 동일하게 돌도록 자격증명을
        # 주입한다(2026-09-29 Windows 빌드 워크스페이스 실측 — 설정 파일
        # 부재 시 'Decodo 계정 없음'으로 조기 거부되어 수집 시나리오가 실패).
        # 회선 확인(exit IP)도 실제 네트워크를 touch하지 않게 한국 응답으로
        # 못박는다 — 자격증명만 주입하면 더미 프록시로 실제 연결을 시도해
        # 타임아웃까지 대기한다.
        patchers = [
            patch.object(
                decodo, "load_settings",
                return_value=decodo.DecodoSettings(
                    username="test-user", password="test-pass",
                ),
            ),
            patch.object(
                decodo, "fetch_exit_ip",
                side_effect=lambda _proxy: decodo.ExitIpInfo(
                    ip="203.0.113.10", country_code="KR",
                    country_name="South Korea",
                ),
            ),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_zero_item_listing_never_creates_csv_or_json_files(self):
        """0 items returned in Phase 1 MUST NOT create empty CSV or JSON files on disk."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "ali_empty_run"
            cfg = AliexpressCategoryRunConfig(
                output_dir=run_dir,
                category_name="완전빈카테고리",
                category_url="https://ko.aliexpress.com/category/000/empty.html",
                max_pages=3,
                delay=0.01,
            )
            fake_page = FakePageForAdversarial(items_by_page={1: [], 2: [], 3: []})
            launcher = FakePlaywrightLauncher(fake_page)
            crawler = AliexpressCategoryCrawler(config=cfg)

            with patch("app.core.aliexpress_category_crawler.async_playwright", return_value=launcher):
                summary = crawler.crawl()

            self.assertEqual(summary.total_items, 0)
            self.assertEqual(summary.collected_items, 0)
            self.assertEqual(summary.unique_vendors, 0)

            # Crucial assertion: Neither CSV nor JSON should exist
            self.assertFalse(summary.csv_file.exists(), f"Pollution detected: {summary.csv_file} was created!")
            self.assertFalse(summary.json_file.exists(), f"Pollution detected: {summary.json_file} was created!")

            # Check that no CSV or JSON files exist in the run dir at all
            csv_files = list(run_dir.rglob("*.csv"))
            json_files = list(run_dir.rglob("*.json"))
            self.assertEqual(len(csv_files), 0, f"Pollution: found CSV files {csv_files}")
            self.assertEqual(len(json_files), 0, f"Pollution: found JSON files {json_files}")

    def test_zero_item_listing_early_streak_break_stops_at_page_two(self):
        """Crawler breaks after 2 consecutive empty pages even if max_pages=10."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AliexpressCategoryRunConfig(
                output_dir=Path(tmp),
                category_name="빈카테고리조기종료",
                category_url="https://ko.aliexpress.com/category/000/empty.html",
                max_pages=10,
                delay=0.01,
            )
            fake_page = FakePageForAdversarial(items_by_page={1: [], 2: [], 3: []})
            launcher = FakePlaywrightLauncher(fake_page)
            crawler = AliexpressCategoryCrawler(config=cfg)

            with patch("app.core.aliexpress_category_crawler.async_playwright", return_value=launcher):
                crawler.crawl()

            # Pages visited should be at most 2 (plus warmup if any)
            listing_visits = [u for u in fake_page.visited_urls if "page=" in u]
            self.assertLessEqual(len(listing_visits), 2, f"Should stop after 2 empty pages, visited: {listing_visits}")

    def test_immediate_cancellation_before_page_1_cleans_up_safely(self):
        """If user cancels immediately, crawler exits without creating CSV or JSON."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AliexpressCategoryRunConfig(
                output_dir=Path(tmp),
                category_name="즉시취소",
                category_url="https://ko.aliexpress.com/category/111/cancel.html",
                max_pages=5,
                delay=0.01,
            )
            control = Control()
            control.request_cancel()

            crawler = AliexpressCategoryCrawler(config=cfg, control=control)
            fake_page = FakePageForAdversarial(items_by_page={1: []})
            launcher = FakePlaywrightLauncher(fake_page)

            with patch("app.core.aliexpress_category_crawler.async_playwright", return_value=launcher):
                with self.assertRaises(CancelledError):
                    crawler.crawl()

            csv_files = list(Path(tmp).rglob("*.csv"))
            json_files = list(Path(tmp).rglob("*.json"))
            self.assertEqual(len(csv_files), 0, "No CSV should be created on immediate cancellation")
            self.assertEqual(len(json_files), 0, "No JSON should be created on immediate cancellation")


# =====================================================================
# Challenge Area 4: 17-Column Dataset & 7 Mandatory Fields Integrity
# =====================================================================
class TestAdversarialDatasetSchemaAndBusinessFields(unittest.TestCase):
    """Stress-test 17-column CSV/JSON schema and 7 mandatory business fields extraction."""

    def setUp(self):
        # 환경에 decodo_settings.json 이 없어도 동일하게 돌도록 자격증명을
        # 주입한다(2026-09-29 Windows 빌드 워크스페이스 실측). 회선 확인도
        # 실제 네트워크를 touch하지 않게 한국 응답으로 못박는다.
        patchers = [
            patch.object(
                decodo, "load_settings",
                return_value=decodo.DecodoSettings(
                    username="test-user", password="test-pass",
                ),
            ),
            patch.object(
                decodo, "fetch_exit_ip",
                side_effect=lambda _proxy: decodo.ExitIpInfo(
                    ip="203.0.113.10", country_code="KR",
                    country_name="South Korea",
                ),
            ),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    EXPECTED_17_COLUMNS = [
        "vendor_id",
        "url",
        "store_name",
        "company_name",
        "ceo_name",
        "business_number",
        "phone",
        "email",
        "address",
        "ecommerce_report_number",
        "power_seller",
        "power_seller_title",
        "rating_count",
        "thumb_up_ratio",
        "product_title",
        "price",
        "collected_at",
    ]

    MANDATORY_7_BUSINESS_FIELDS = [
        "company_name",
        "ceo_name",
        "business_number",
        "phone",
        "email",
        "address",
        "ecommerce_report_number",
    ]

    def test_schema_exact_17_columns_in_order(self):
        """COUPANG_DATASET_FIELDS must match the exact 17-column specification."""
        self.assertEqual(COUPANG_DATASET_FIELDS, self.EXPECTED_17_COLUMNS)

    def test_full_crawl_produces_valid_17_column_csv_and_json(self):
        """Simulate end-to-end extraction with diverse mtop property names."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AliexpressCategoryRunConfig(
                output_dir=Path(tmp),
                category_name="데이터무결성테스트",
                category_url="https://ko.aliexpress.com/category/999/integrity.html",
                max_pages=1,
                delay=0.01,
            )

            item = {
                "id": "item_999",
                "url": "https://ko.aliexpress.com/item/item_999.html",
                "title": "테스트 프리미엄 상품",
                "price": "₩19,900",
                "orders": "500+ 판매",
                "is_top_seller": True,
            }

            mtop_response = {
                "data": {
                    "result": {
                        "SHOP_CARD_PC": {
                            "storeName": "글로벌파트너스토어",
                            "benefitInfoList": [{"title": "좋아요", "value": "99.1%"}],
                        },
                        "GLOBAL_DATA": {
                            "globalData": {"sellerId": "VENDOR_999"},
                        },
                        "PRODUCT_PROP_PC": {
                            "showedProps": [
                                {"attrName": "회사 이름", "attrValue": "(주)글로벌트레이딩"},
                                {"attrName": "대표자", "attrValue": "김대표"},
                                {"attrName": "사업자등록번호", "attrValue": "123-45-67890"},
                                {"attrName": "고객센터", "attrValue": "1588-0000"},
                                {"attrName": "이메일", "attrValue": "ceo@globaltrading.co.kr"},
                                {"attrName": "사업장소재지", "attrValue": "서울특별시 강남구 테헤란로 123"},
                                {"attrName": "통신판매업신고", "attrValue": "2026-서울강남-1234"},
                            ]
                        },
                    }
                }
            }

            fake_page = FakePageForAdversarial(
                items_by_page={1: [item]},
                mtop_by_item={"item_999": mtop_response},
            )
            launcher = FakePlaywrightLauncher(fake_page)
            crawler = AliexpressCategoryCrawler(config=cfg)

            with patch("app.core.aliexpress_category_crawler.async_playwright", return_value=launcher):
                summary = crawler.crawl()

            self.assertEqual(summary.collected_items, 1)
            self.assertEqual(summary.unique_vendors, 1)

            # Check CSV file structure
            self.assertTrue(summary.csv_file.exists())
            with open(summary.csv_file, "r", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                rows = list(reader)
                header = rows[0]
                data_row = rows[1]

                # 17 columns in header
                self.assertEqual(len(header), 17)
                self.assertEqual(header, self.EXPECTED_17_COLUMNS)

                # 17 columns in data row
                self.assertEqual(len(data_row), 17)

                # Map to dict for easy inspection
                row_dict = dict(zip(header, data_row))

                # Verify 7 mandatory business fields
                self.assertEqual(row_dict["company_name"], "(주)글로벌트레이딩")
                self.assertEqual(row_dict["ceo_name"], "김대표")
                self.assertEqual(row_dict["business_number"], "123-45-67890")
                self.assertEqual(row_dict["phone"], "1588-0000")
                self.assertEqual(row_dict["email"], "ceo@globaltrading.co.kr")
                self.assertEqual(row_dict["address"], "서울특별시 강남구 테헤란로 123")
                self.assertEqual(row_dict["ecommerce_report_number"], "2026-서울강남-1234")

                # Verify extra attributes
                self.assertEqual(row_dict["power_seller"], "TRUE")
                self.assertEqual(row_dict["power_seller_title"], "AliExpress TOP셀러")
                self.assertEqual(row_dict["thumb_up_ratio"], "99.1%")
                self.assertEqual(row_dict["rating_count"], "500+ 판매")
                self.assertEqual(row_dict["product_title"], "테스트 프리미엄 상품")
                self.assertEqual(row_dict["price"], "₩19,900")

            # Check JSON file structure
            self.assertTrue(summary.json_file.exists())
            with open(summary.json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.assertEqual(len(data), 1)
                rec = data[0]
                # Check that all 17 keys are present in JSON record
                for k in self.EXPECTED_17_COLUMNS:
                    self.assertIn(k, rec)
                self.assertEqual(rec["company_name"], "(주)글로벌트레이딩")

    def test_missing_props_fallback_to_detail_unrecorded(self):
        """When seller has no business info, company_name gracefully falls back."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AliexpressCategoryRunConfig(
                output_dir=Path(tmp),
                category_name="정보누락테스트",
                category_url="https://ko.aliexpress.com/category/999/missing.html",
                max_pages=1,
                delay=0.01,
            )

            item = {
                "id": "item_empty_prop",
                "url": "https://ko.aliexpress.com/item/item_empty_prop.html",
                "title": "정보없는상품",
                "price": "₩5,000",
                "orders": "0",
                "is_top_seller": False,
            }

            # Empty product properties
            mtop_response = {
                "data": {
                    "result": {
                        "SHOP_CARD_PC": {},
                        "GLOBAL_DATA": {"globalData": {"sellerId": "VENDOR_EMPTY"}},
                        "PRODUCT_PROP_PC": {"showedProps": []},
                    }
                }
            }

            fake_page = FakePageForAdversarial(
                items_by_page={1: [item]},
                mtop_by_item={"item_empty_prop": mtop_response},
            )
            launcher = FakePlaywrightLauncher(fake_page)
            crawler = AliexpressCategoryCrawler(config=cfg)

            with patch("app.core.aliexpress_category_crawler.async_playwright", return_value=launcher):
                summary = crawler.crawl()

            with open(summary.csv_file, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                row = next(reader)
                self.assertEqual(row["company_name"], "(상세 미기재)")
                self.assertEqual(row["power_seller"], "FALSE")
                self.assertEqual(row["power_seller_title"], "")

    def test_positive_feedback_korean_title_boundary_condition(self):
        """Boundary test: '긍정적 피드백' title vs ('좋아요', 'positive') crawler filter.
        
        AliExpress category crawler line 501 filters benefitInfoList with:
        `if '좋아요' in b.get('title', '') or 'positive' in b.get('title', '').lower():`
        Empirically verifying that when AliExpress mtop returns Korean title '긍정적 피드백',
        the crawler falls back to empty string '' without crashing or corrupting 17 columns.
        """
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AliexpressCategoryRunConfig(
                output_dir=Path(tmp),
                category_name="피드백경계테스트",
                category_url="https://ko.aliexpress.com/category/999/feedback.html",
                max_pages=1,
                delay=0.01,
            )
            item = {"id": "item_fb", "url": "https://ko.aliexpress.com/item/fb.html", "title": "피드백상품", "price": "1000", "orders": "1", "is_top_seller": False}
            mtop_response = {
                "data": {
                    "result": {
                        "SHOP_CARD_PC": {
                            "storeName": "피드백스토어",
                            "benefitInfoList": [{"title": "긍정적 피드백", "value": "97.5%"}],
                        },
                        "GLOBAL_DATA": {"globalData": {"sellerId": "VENDOR_FB"}},
                        "PRODUCT_PROP_PC": {"showedProps": [{"attrName": "회사 이름", "attrValue": "피드백상사"}]},
                    }
                }
            }
            fake_page = FakePageForAdversarial(items_by_page={1: [item]}, mtop_by_item={"item_fb": mtop_response})
            launcher = FakePlaywrightLauncher(fake_page)
            crawler = AliexpressCategoryCrawler(config=cfg)

            with patch("app.core.aliexpress_category_crawler.async_playwright", return_value=launcher):
                summary = crawler.crawl()

            with open(summary.csv_file, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                row = next(reader)
                # Crawler sets empty string when title is '긍정적 피드백' because of line 501 filter
                self.assertEqual(row["thumb_up_ratio"], "")
                # However, 17 columns integrity and mandatory business fields are 100% intact
                self.assertEqual(len(row), 17)
                self.assertEqual(row["company_name"], "피드백상사")


if __name__ == "__main__":
    unittest.main()
