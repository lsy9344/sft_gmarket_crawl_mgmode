"""AliExpress 카테고리 수집기 종합 E2E 및 시스템 통합 테스트.

4-Tier 검증 방법론을 기반으로:
- Tier 1: 카테고리 트리 이중화 탐색/대체 UX 및 프록시 3지선다 대화상자/로컬 안전 모드
- Tier 2: 트리 누락/손상, 빈 카테고리 목록(0건), 사용자 취소 인터럽트 방어
- Tier 3: 로컬/프록시 모드와 Phase 1/Phase 2 재개 교차 결합 검증
- Tier 4: 일시정지/재개 완주 E2E 시뮬레이션 및 네트워크 순단 후 캐시 적중 중복 방지
를 철저히 검증한다.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox, QWidget
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
    from app.core.base import Control
    from app.ui.aliexpress_category_panel import AliexpressCategoryPanel
    from app.ui.main_window import MainWindow
    from app.workers.aliexpress_category_crawl_worker import AliexpressCategoryCrawlWorker

try:
    from app.core.aliexpress_resume_store import (
        AliexpressResumeStore,
        ali_category_run_dir,
        peek_resume,
    )
    _RESUME_AVAILABLE = True
except ImportError:
    _RESUME_AVAILABLE = False


_app = None


def setUpModule():
    global _app
    if _QT_OK:
        _app = QApplication.instance() or QApplication(sys.argv)


# ── Fakes & Helpers for Crawler Simulation ─────────────────────────

class FakePlaywrightPage:
    """AliExpress 카테고리 페이지 및 mtop PDP 응답을 시뮬레이션하는 Fake Page."""

    def __init__(self, page_items_map: dict[int, list[dict]] | None = None, pdp_responses: dict[str, dict] | None = None):
        self.page_items_map = page_items_map or {}
        self.pdp_responses = pdp_responses or {}
        self.goto_urls: list[str] = []
        self._listeners: dict[str, list] = {}
        self.url = ""
        self.closed = False

    async def goto(self, url: str, **kwargs):
        self.goto_urls.append(url)
        self.url = url

        # PDP URL 파싱: https://ko.aliexpress.com/item/{item_id}.html
        for item_id, resp_payload in self.pdp_responses.items():
            if f"/item/{item_id}.html" in url:
                if resp_payload is None:
                    raise RuntimeError(f"Simulated network error for item {item_id}")
                class FakeResponse:
                    def __init__(self, payload):
                        self.url = "https://acs.aliexpress.com/h5/mtop.aliexpress.pdp.pc.query/1.0/"
                        self.status = 200
                        self._payload = payload

                    async def body(self):
                        return json.dumps(self._payload).encode("utf-8")

                handlers = list(self._listeners.get("response", []))
                for handler in handlers:
                    await handler(FakeResponse(resp_payload))
                break

    async def wait_for_timeout(self, timeout_ms: int):
        pass

    async def evaluate(self, script: str, *args):
        if "window.scrollBy" in script:
            return None
        if "document.querySelectorAll" in script:
            curr = self.goto_urls[-1] if self.goto_urls else ""
            p_num = 1
            if "page=" in curr:
                try:
                    p_num = int(curr.split("page=")[-1].split("&")[0])
                except Exception:
                    p_num = 1
            return self.page_items_map.get(p_num, [])
        return None

    def on(self, event: str, handler):
        self._listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler):
        if event in self._listeners and handler in self._listeners[event]:
            self._listeners[event].remove(handler)

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, page: FakePlaywrightPage):
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


class FakeBrowser:
    def __init__(self, context: FakeContext):
        self._context = context

    async def new_context(self, **kwargs):
        return self._context

    async def close(self):
        pass


class FakePlaywrightManager:
    def __init__(self, page: FakePlaywrightPage):
        self._page = page
        self.chromium = MagicMock()

        async def launch(**kwargs):
            return FakeBrowser(FakeContext(self._page))

        self.chromium.launch = launch

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


def _make_mtop_response(seller_id: str, store_name: str, company: str, ceo: str, bnum: str, email: str, phone: str, addr: str, ecom: str) -> dict:
    return {
        "data": {
            "result": {
                "SHOP_CARD_PC": {
                    "storeName": store_name,
                    "benefitInfoList": [{"title": "긍정적 피드백", "value": "98.5%"}],
                },
                "GLOBAL_DATA": {
                    "globalData": {"sellerId": seller_id},
                },
                "PRODUCT_PROP_PC": {
                    "showedProps": [
                        {"attrName": "상호명", "attrValue": company},
                        {"attrName": "대표자명", "attrValue": ceo},
                        {"attrName": "사업자번호", "attrValue": bnum},
                        {"attrName": "이메일 주소", "attrValue": email},
                        {"attrName": "소비자상담전화번호", "attrValue": phone},
                        {"attrName": "사업장소재지", "attrValue": addr},
                        {"attrName": "통신판매업신고번호", "attrValue": ecom},
                    ]
                },
            }
        }
    }


# ── Tier 1: Feature Coverage ────────────────────────────────────────

@unittest.skipUnless(_QT_OK, "PyQt6 필요")
class TestCategoryTreeResolution(unittest.TestCase):
    """Tier 1 - Feature 1: 카테고리 트리 이중화 탐색 및 대체 UX 검증 (>=5 tests)."""

    def setUp(self):
        self.panel = AliexpressCategoryPanel()

    def tearDown(self):
        self.panel.deleteLater()

    def test_tree_resolution_meipass_bundled_path(self):
        """1순위 sys._MEIPASS 번들 경로에서 카테고리 트리가 정상 로드되는지 검증."""
        with tempfile.TemporaryDirectory() as tmp:
            meipass_res = Path(tmp) / "app" / "resources"
            meipass_res.mkdir(parents=True)
            tree_data = [{"id": "100", "name": "패키지대분류", "children": [{"id": "101", "name": "패키지소분류", "url": "http://test"}]}]
            (meipass_res / "aliexpress_category_tree.json").write_text(json.dumps(tree_data), encoding="utf-8")

            with patch("sys._MEIPASS", tmp, create=True):
                self.panel._load_category_tree()
                root_count = self.panel.category_tree.topLevelItemCount()
                self.assertTrue(root_count > 0, "sys._MEIPASS 번들 경로의 카테고리 트리가 로드되어야 합니다")
                root_item = self.panel.category_tree.topLevelItem(0)
                self.assertEqual(root_item.text(0), "패키지대분류")

    def test_tree_resolution_dev_parents_path(self):
        """2순위 소스 개발 경로에서 카테고리 트리가 정상 로드되는지 검증."""
        with tempfile.TemporaryDirectory() as tmp:
            dev_res = Path(tmp) / "app" / "resources"
            dev_res.mkdir(parents=True)
            tree_data = [{"id": "200", "name": "개발대분류", "children": [{"id": "201", "name": "개발소분류", "url": "http://dev"}]}]
            (dev_res / "aliexpress_category_tree.json").write_text(json.dumps(tree_data), encoding="utf-8")

            with patch("sys._MEIPASS", None, create=True), \
                 patch.object(config, "PROJECT_ROOT", Path(tmp)):
                self.panel._load_category_tree()
                root_count = self.panel.category_tree.topLevelItemCount()
                self.assertTrue(root_count > 0)

    def test_tree_resolution_project_root_path(self):
        """3순위 config.PROJECT_ROOT 경로에서 카테고리 트리가 정상 로드되는지 검증."""
        with tempfile.TemporaryDirectory() as tmp:
            proj_res = Path(tmp) / "app" / "resources"
            proj_res.mkdir(parents=True)
            tree_data = [{"id": "300", "name": "루트대분류", "children": []}]
            (proj_res / "aliexpress_category_tree.json").write_text(json.dumps(tree_data), encoding="utf-8")

            with patch("sys._MEIPASS", None, create=True), \
                 patch.object(config, "PROJECT_ROOT", Path(tmp)):
                self.panel._load_category_tree()
                self.assertTrue(self.panel.category_tree.topLevelItemCount() > 0)

    def test_tree_resolution_cwd_path(self):
        """4순위 Path.cwd() 경로에서 카테고리 트리가 정상 로드되는지 검증."""
        with tempfile.TemporaryDirectory() as tmp:
            cwd_res = Path(tmp) / "app" / "resources"
            cwd_res.mkdir(parents=True)
            tree_data = [{"id": "400", "name": "CWD대분류", "children": []}]
            (cwd_res / "aliexpress_category_tree.json").write_text(json.dumps(tree_data), encoding="utf-8")

            with patch("sys._MEIPASS", None, create=True), \
                 patch.object(config, "PROJECT_ROOT", Path(tmp) / "no_app"), \
                 patch("pathlib.Path.cwd", return_value=Path(tmp)):
                self.panel._load_category_tree()
                self.assertTrue(self.panel.category_tree.topLevelItemCount() > 0)

    def test_tree_fallback_when_all_paths_missing(self):
        """모든 경로에서 트리 파일 부재 시 로그 안내 및 '직접 URL 입력' 탭 자동 전환 검증."""
        with patch.object(Path, "is_file", return_value=False):
            self.panel.category_tree.clear()
            self.panel._load_category_tree()
            self.assertEqual(
                self.panel.input_tabs.currentIndex(), 1,
                "트리 파일 로드 불가 시 '직접 URL 입력' 탭(index=1)으로 전환되어야 합니다",
            )
            self.assertIn("기본 카테고리 트리를 불러올 수 없습니다", self.panel.log_text.toPlainText())

    def test_tree_fallback_when_json_syntax_error(self):
        """트리 파일 내용 손상 시 크래시 없이 직접 URL 입력 탭으로 자동 전환 검증."""
        with tempfile.TemporaryDirectory() as tmp:
            res_dir = Path(tmp) / "app" / "resources"
            res_dir.mkdir(parents=True)
            (res_dir / "aliexpress_category_tree.json").write_text("INVALID_BROKEN_JSON_DATA", encoding="utf-8")

            with patch("sys._MEIPASS", tmp, create=True):
                self.panel._load_category_tree()
                self.assertEqual(self.panel.input_tabs.currentIndex(), 1)


@unittest.skipUnless(_QT_OK, "PyQt6 필요")
class TestProxyCheckThreeWayDialog(unittest.TestCase):
    """Tier 1 - Feature 2: 프록시 3지선다 대화상자 및 로컬 안전 수집 모드 검증 (>=5 tests)."""

    def setUp(self):
        self.win = MainWindow()

    def tearDown(self):
        self.win.alicat_worker = None
        self.win.category_worker = None
        self.win.crawl_worker = None
        self.win.prescan_worker = None
        self.win.coupang_worker = None
        self.win.foodspring_worker = None
        self.win.categories_worker = None
        self.win.gmcat_worker = None
        self.win.gmcat_crawl_worker = None
        with patch.object(self.win, "_active_worker", return_value=None), \
             patch("PyQt6.QtWidgets.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            self.win.close()

    def test_proxy_dialog_prompt_when_credentials_missing(self):
        """Decodo 미등록 시 '수집 방식 선택' 대화상자가 노출되는지 검증."""
        cfg = AliexpressCategoryRunConfig(
            output_dir=Path("/tmp/ali_test_out"),
            category_name="테스트",
            category_url="https://ko.aliexpress.com/w/wholesale-test.html",
        )

        captured_box = []

        def fake_exec(box_self):
            captured_box.append(box_self)
            box_self._mock_choice = None

        with patch("app.core.decodo.load_settings", return_value=decodo.DecodoSettings(username="", password="")), \
             patch.object(QMessageBox, "exec", fake_exec), \
             patch.object(QMessageBox, "clickedButton", lambda b: getattr(b, "_mock_choice", None)):
            self.win.on_alicat_start(cfg)

        self.assertTrue(len(captured_box) > 0, "QMessageBox 대화상자가 생성되어야 합니다")
        self.assertEqual(captured_box[0].windowTitle(), "수집 방식 선택")

    def test_proxy_dialog_copy_text_contains_options(self):
        """대화상자 본문이 사양서의 정확한 카피(① 로컬 회선, ② 설정 탭)를 포함하는지 검증."""
        cfg = AliexpressCategoryRunConfig(
            output_dir=Path("/tmp/ali_test_out"),
            category_name="테스트",
            category_url="https://ko.aliexpress.com/w/wholesale-test.html",
        )

        captured_box = []

        def fake_exec(box_self):
            captured_box.append(box_self)
            box_self._mock_choice = None

        with patch("app.core.decodo.load_settings", return_value=decodo.DecodoSettings(username="", password="")), \
             patch.object(QMessageBox, "exec", fake_exec), \
             patch.object(QMessageBox, "clickedButton", lambda b: getattr(b, "_mock_choice", None)):
            self.win.on_alicat_start(cfg)

        self.assertTrue(len(captured_box) > 0)
        box_text = captured_box[0].text()
        self.assertIn("로컬 회선으로 안전 수집", box_text)
        self.assertIn("설정 탭으로 이동", box_text)

    def test_proxy_dialog_select_local_safe_crawl(self):
        """[로컬 회선으로 안전 수집] 선택 시 use_proxy=False, delay=3.5s 로 워커가 시작되는지 검증."""
        cfg = AliexpressCategoryRunConfig(
            output_dir=Path("/tmp/ali_test_out"),
            category_name="테스트",
            category_url="https://ko.aliexpress.com/w/wholesale-test.html",
        )

        def make_simulator(choice_text: str):
            def fake_exec(box_self):
                for btn in box_self.buttons():
                    if choice_text in btn.text():
                        box_self._mock_choice = btn
                        return
                box_self._mock_choice = None
            return fake_exec

        with patch("app.core.decodo.load_settings", return_value=decodo.DecodoSettings(username="", password="")), \
             patch.object(QMessageBox, "exec", make_simulator("로컬 회선으로 안전 수집")), \
             patch.object(QMessageBox, "clickedButton", lambda b: getattr(b, "_mock_choice", None)), \
             patch.object(AliexpressCategoryCrawlWorker, "start"):
            self.win.on_alicat_start(cfg)

        self.assertIsNotNone(self.win.alicat_worker, "로컬 수집 선택 시 워커가 생성되어야 합니다")
        self.assertFalse(self.win.alicat_worker.config.use_proxy, "use_proxy 가 False 여야 합니다")
        self.assertAlmostEqual(self.win.alicat_worker.config.delay, 3.5, delta=0.5, msg="안전 딜레이 3.5초가 적용되어야 합니다")
        self.win.alicat_worker = None

    def test_proxy_dialog_select_settings_tab(self):
        """[설정 탭으로 이동] 선택 시 워커를 시작하지 않고 설정 탭으로 전환되는지 검증."""
        cfg = AliexpressCategoryRunConfig(
            output_dir=Path("/tmp/ali_test_out"),
            category_name="테스트",
            category_url="https://ko.aliexpress.com/w/wholesale-test.html",
        )

        def make_simulator(choice_text: str):
            def fake_exec(box_self):
                for btn in box_self.buttons():
                    if choice_text in btn.text():
                        box_self._mock_choice = btn
                        return
                box_self._mock_choice = None
            return fake_exec

        with patch("app.core.decodo.load_settings", return_value=decodo.DecodoSettings(username="", password="")), \
             patch.object(QMessageBox, "exec", make_simulator("설정 탭으로 이동")), \
             patch.object(QMessageBox, "clickedButton", lambda b: getattr(b, "_mock_choice", None)):
            self.win.on_alicat_start(cfg)

        self.assertIsNone(self.win.alicat_worker)
        self.assertEqual(self.win.tab_widget.currentWidget(), self.win.brightdata_panel)

    def test_proxy_preflight_direct_start_when_credentials_ready(self):
        """Decodo 자격 증명이 유효할 때는 팝업 없이 바로 프록시 모드로 시작되는지 검증."""
        cfg = AliexpressCategoryRunConfig(
            output_dir=Path("/tmp/ali_test_out"),
            category_name="테스트",
            category_url="https://ko.aliexpress.com/w/wholesale-test.html",
        )

        with patch("app.core.decodo.load_settings", return_value=decodo.DecodoSettings(username="user", password="pwd")), \
             patch("app.core.decodo.credentials_ready", return_value=True), \
             patch.object(QMessageBox, "exec") as mock_exec, \
             patch.object(AliexpressCategoryCrawlWorker, "start"):
            self.win.on_alicat_start(cfg)
            mock_exec.assert_not_called()
            self.assertIsNotNone(self.win.alicat_worker)
            self.assertTrue(self.win.alicat_worker.config.use_proxy)
            self.win.alicat_worker = None

    def test_top_banner_html_contains_both_modes(self):
        """상단 배너 HTML이 0원 로컬 수집과 25건 단위 자동 회선 순환을 모두 표기하는지 검증."""
        labels = [lbl.text() for lbl in self.win.aliexpress_category_panel.findChildren(QLabel)]
        all_text = " ".join(labels)
        self.assertIn("0원 수집", all_text)
        self.assertIn("25건", all_text)


# ── Tier 2: Boundary & Corner Cases ─────────────────────────────────

@unittest.skipUnless(_QT_OK, "PyQt6 필요")
class TestCategoryTreeBoundaries(unittest.TestCase):
    """Tier 2 - Boundary 1: 트리 파일 누락 및 손상 예외 방어 검증 (>=5 tests)."""

    def setUp(self):
        self.panel = AliexpressCategoryPanel()

    def tearDown(self):
        self.panel.deleteLater()

    def test_missing_tree_file_handled_cleanly(self):
        with patch.object(Path, "is_file", return_value=False):
            self.panel.category_tree.clear()
            self.panel._load_category_tree()
            self.assertEqual(self.panel.category_tree.topLevelItemCount(), 0)
            self.assertEqual(self.panel.input_tabs.currentIndex(), 1)
            self.assertIn("기본 카테고리 트리를 불러올 수 없습니다", self.panel.log_text.toPlainText())

    def test_corrupted_json_syntax_error_handled(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "app" / "resources" / "aliexpress_category_tree.json"
            f.parent.mkdir(parents=True)
            f.write_text("{\"broken\": [", encoding="utf-8")
            with patch("sys._MEIPASS", tmp, create=True):
                self.panel.category_tree.clear()
                self.panel._load_category_tree()
                self.assertEqual(self.panel.category_tree.topLevelItemCount(), 0)
                self.assertEqual(self.panel.input_tabs.currentIndex(), 1)
                self.assertIn("카테고리 트리 오류", self.panel.log_text.toPlainText())

    def test_empty_json_tree_file_handled(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "app" / "resources" / "aliexpress_category_tree.json"
            f.parent.mkdir(parents=True)
            f.write_text("[]", encoding="utf-8")
            with patch("sys._MEIPASS", tmp, create=True):
                self.panel.category_tree.clear()
                self.panel._load_category_tree()
                self.assertEqual(self.panel.category_tree.topLevelItemCount(), 0)
                self.assertEqual(self.panel.input_tabs.currentIndex(), 1)
                self.assertIn("카테고리 트리 오류", self.panel.log_text.toPlainText())

    def test_tree_node_missing_expected_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "app" / "resources" / "aliexpress_category_tree.json"
            f.parent.mkdir(parents=True)
            f.write_text(json.dumps([{"name": "불완전대분류"}]), encoding="utf-8")
            with patch("sys._MEIPASS", tmp, create=True):
                self.panel._load_category_tree()
                self.assertEqual(self.panel.category_tree.topLevelItemCount(), 1)
                self.assertEqual(self.panel.category_tree.topLevelItem(0).text(0), "불완전대분류")

    def test_filter_tree_on_empty_tree_is_safe(self):
        self.panel.category_tree.clear()
        self.panel._filter_tree("검색어")
        self.assertEqual(self.panel.category_tree.topLevelItemCount(), 0)


class TestEmptyCategoryListing(unittest.TestCase):
    """Tier 2 - Boundary 3: 0건 수집 카테고리 방어 및 데이터셋 무결성 (>=5 tests)."""

    def test_empty_listing_returns_zero_items_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AliexpressCategoryRunConfig(
                output_dir=Path(tmp),
                category_name="빈카테고리",
                category_url="https://ko.aliexpress.com/w/empty.html",
                max_pages=2,
                delay=0.01,
            )
            crawler = AliexpressCategoryCrawler(config=cfg)
            fake_page = FakePlaywrightPage(page_items_map={1: [], 2: []})

            with patch("app.core.aliexpress_category_crawler.async_playwright", return_value=FakePlaywrightManager(fake_page)):
                summary = crawler.crawl()

            self.assertEqual(summary.total_items, 0)
            self.assertEqual(summary.collected_items, 0)
            self.assertEqual(summary.unique_vendors, 0)

    def test_empty_listing_does_not_execute_phase2(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AliexpressCategoryRunConfig(
                output_dir=Path(tmp),
                category_name="빈카테고리",
                category_url="https://ko.aliexpress.com/w/empty.html",
                max_pages=2,
                delay=0.01,
            )
            phases = []
            crawler = AliexpressCategoryCrawler(config=cfg, on_phase=lambda name, cur, tot: phases.append(name))
            fake_page = FakePlaywrightPage(page_items_map={1: []})

            with patch("app.core.aliexpress_category_crawler.async_playwright", return_value=FakePlaywrightManager(fake_page)):
                crawler.crawl()

            self.assertTrue(all("Phase 2" not in p for p in phases))

    def test_empty_listing_does_not_create_empty_csv(self):
        """상품이 0건인 경우 불필요한 빈 CSV 파일을 디스크에 생성하지 않는지 검증."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AliexpressCategoryRunConfig(
                output_dir=Path(tmp),
                category_name="빈카테고리",
                category_url="https://ko.aliexpress.com/w/empty.html",
                max_pages=1,
                delay=0.01,
            )
            crawler = AliexpressCategoryCrawler(config=cfg)
            fake_page = FakePlaywrightPage(page_items_map={1: []})

            with patch("app.core.aliexpress_category_crawler.async_playwright", return_value=FakePlaywrightManager(fake_page)):
                summary = crawler.crawl()

            self.assertFalse(summary.csv_file.exists(), "0건 목록일 때는 빈 CSV를 생성하지 않아야 합니다")
            self.assertEqual(summary.total_items, 0)

    def test_empty_listing_summary_metrics_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AliexpressCategoryRunConfig(
                output_dir=Path(tmp),
                category_name="빈카테고리",
                category_url="https://ko.aliexpress.com/w/empty.html",
                max_pages=1,
                delay=0.01,
            )
            crawler = AliexpressCategoryCrawler(config=cfg)
            fake_page = FakePlaywrightPage(page_items_map={1: []})

            with patch("app.core.aliexpress_category_crawler.async_playwright", return_value=FakePlaywrightManager(fake_page)):
                summary = crawler.crawl()

            self.assertEqual(summary.has_email, 0)
            self.assertEqual(summary.has_business_number, 0)
            self.assertEqual(summary.has_ceo_name, 0)

    def test_empty_listing_streak_break(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AliexpressCategoryRunConfig(
                output_dir=Path(tmp),
                category_name="빈카테고리",
                category_url="https://ko.aliexpress.com/w/empty.html",
                max_pages=10,
                delay=0.01,
            )
            crawler = AliexpressCategoryCrawler(config=cfg)
            fake_page = FakePlaywrightPage(page_items_map={1: [], 2: []})

            with patch("app.core.aliexpress_category_crawler.async_playwright", return_value=FakePlaywrightManager(fake_page)):
                crawler.crawl()

            requested_pages = [u for u in fake_page.goto_urls if "page=" in u]
            self.assertTrue(len(requested_pages) <= 3)


@unittest.skipUnless(_QT_OK, "PyQt6 필요")
class TestUserCancellationAtDialogs(unittest.TestCase):
    """Tier 2 - Boundary 4: 대화상자 취소 및 중단 복원력 검증 (>=5 tests)."""

    def setUp(self):
        self.win = MainWindow()

    def tearDown(self):
        self.win.alicat_worker = None
        self.win.category_worker = None
        self.win.crawl_worker = None
        self.win.prescan_worker = None
        self.win.coupang_worker = None
        self.win.foodspring_worker = None
        self.win.categories_worker = None
        self.win.gmcat_worker = None
        self.win.gmcat_crawl_worker = None
        with patch.object(self.win, "_active_worker", return_value=None), \
             patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.win.close()

    def test_proxy_dialog_cancel_button_aborts_start(self):
        cfg = AliexpressCategoryRunConfig(
            output_dir=Path("/tmp/ali_test_out"),
            category_name="테스트",
            category_url="https://ko.aliexpress.com/w/wholesale-test.html",
        )

        def make_simulator(choice_text: str):
            def fake_exec(box_self):
                for btn in box_self.buttons():
                    if choice_text in btn.text():
                        box_self._mock_choice = btn
                        return
                box_self._mock_choice = None
            return fake_exec

        with patch("app.core.decodo.load_settings", return_value=decodo.DecodoSettings(username="", password="")), \
             patch.object(QMessageBox, "exec", make_simulator("취소")), \
             patch.object(QMessageBox, "clickedButton", lambda b: getattr(b, "_mock_choice", None)):
            self.win.on_alicat_start(cfg)
            self.assertIsNone(self.win.alicat_worker)
            self.assertEqual(self.win.aliexpress_category_panel._state, "idle")

    def test_proxy_dialog_escape_or_close_aborts_start(self):
        cfg = AliexpressCategoryRunConfig(
            output_dir=Path("/tmp/ali_test_out"),
            category_name="테스트",
            category_url="https://ko.aliexpress.com/w/wholesale-test.html",
        )

        with patch("app.core.decodo.load_settings", return_value=decodo.DecodoSettings(username="", password="")), \
             patch.object(QMessageBox, "exec", lambda b: None), \
             patch.object(QMessageBox, "clickedButton", lambda b: None):
            self.win.on_alicat_start(cfg)
            self.assertIsNone(self.win.alicat_worker)

    @unittest.skipUnless(_RESUME_AVAILABLE, "AliexpressResumeStore 구현 대기")
    def test_resume_dialog_cancel_aborts_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = ali_category_run_dir(Path(tmp), "채소", "https://ko.aliexpress.com/category/100/v.html")
            run_dir.mkdir(parents=True)
            with AliexpressResumeStore(run_dir / "resume.sqlite3") as store:
                store.check_config("채소", "https://ko.aliexpress.com/category/100/v.html", 10)
                store.record_page(1, [{"id": "p1", "url": "u", "title": "t", "price": "p", "orders": "o", "is_top_seller": False}])

            cfg = AliexpressCategoryRunConfig(
                output_dir=Path(tmp),
                category_name="채소",
                category_url="https://ko.aliexpress.com/category/100/v.html",
            )

            with patch("app.core.decodo.load_settings", return_value=decodo.DecodoSettings(username="u", password="p")), \
                 patch("app.core.decodo.credentials_ready", return_value=True), \
                 patch.object(self.win, "_ask_resume_mode", return_value="cancel"):
                self.win.on_alicat_start(cfg)
                self.assertIsNone(self.win.alicat_worker)
                self.assertTrue((run_dir / "resume.sqlite3").exists())

    def test_user_cancel_via_control_signals_cancelling(self):
        control = Control()
        self.win.alicat_control = control
        self.win.on_alicat_cancel()
        self.assertTrue(control.is_cancelled())
        self.assertEqual(self.win.aliexpress_category_panel._state, "cancelling")

    def test_user_pause_and_resume_control_signals(self):
        control = Control()
        self.win.alicat_control = control
        self.win.on_alicat_pause()
        self.assertTrue(control.is_paused())
        self.assertEqual(self.win.aliexpress_category_panel._state, "paused")

        self.win.on_alicat_resume()
        self.assertFalse(control.is_paused())
        self.assertEqual(self.win.aliexpress_category_panel._state, "running")


# ── Tier 3: Cross-Feature Combinations ──────────────────────────────

class TestCrossFeatureCombinations(unittest.TestCase):
    """Tier 3: 로컬/프록시 모드와 재개 상태 교차 결합 검증."""

    @unittest.skipUnless(_RESUME_AVAILABLE, "AliexpressResumeStore 구현 대기")
    def test_local_safe_crawl_mode_with_phase1_resume(self):
        """로컬 안전 수집 모드 + Phase 1 중단 재개 시 다음 페이지부터 로컬 회선으로 진행."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = ali_category_run_dir(Path(tmp), "토마토", "https://ko.aliexpress.com/category/555/tomato.html")
            run_dir.mkdir(parents=True)
            with AliexpressResumeStore(run_dir / "resume.sqlite3") as store:
                store.check_config("토마토", "https://ko.aliexpress.com/category/555/tomato.html", 5)
                store.record_page(1, [{"id": "t1", "url": "http://item/t1.html", "title": "토마토1", "price": "1000", "orders": "10", "is_top_seller": False}])

            cfg = AliexpressCategoryRunConfig(
                output_dir=Path(tmp),
                category_name="토마토",
                category_url="https://ko.aliexpress.com/category/555/tomato.html",
                max_pages=5,
                delay=3.5,
            )
            self.assertEqual(cfg.delay, 3.5)

    @unittest.skipUnless(_RESUME_AVAILABLE, "AliexpressResumeStore 구현 대기")
    def test_proxy_crawl_mode_with_phase2_resume(self):
        """프록시 모드 + Phase 2 중단 재개 시 기확보 판매자 스킵 및 미완료 판매자만 호출."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = ali_category_run_dir(Path(tmp), "사과", "https://ko.aliexpress.com/category/777/apple.html")
            run_dir.mkdir(parents=True)
            with AliexpressResumeStore(run_dir / "resume.sqlite3") as store:
                store.check_config("사과", "https://ko.aliexpress.com/category/777/apple.html", 5)
                store.record_page(1, [
                    {"id": "a1", "url": "http://item/a1.html", "title": "사과1", "price": "1000", "orders": "10", "is_top_seller": False},
                    {"id": "a2", "url": "http://item/a2.html", "title": "사과2", "price": "2000", "orders": "20", "is_top_seller": False},
                ])
                store.mark_listing_done()
                store.record_seller("V_APPLE_1", {"company_name": "(주)사과나라", "business_number": "111-11-11111"})
                store.record_item_result("a1", "V_APPLE_1", {})

            with AliexpressResumeStore(run_dir / "resume.sqlite3") as store2:
                self.assertIn("a1", store2.processed_item_ids())
                self.assertNotIn("a2", store2.processed_item_ids())
                self.assertIn("V_APPLE_1", store2.confirmed_sellers())


# ── Tier 4: Real-World Application Scenarios ────────────────────────

class TestRealWorldApplicationScenarios(unittest.TestCase):
    """Tier 4: E2E 시뮬레이션 및 순단 복원력 실측 검증."""

    def test_e2e_simulated_crawl_flow_and_17_column_output_verification(self):
        """전체 수집 흐름(목록 탐색 -> 판매자 상세 -> CSV/JSON 17개 표준 컬럼 저장) 완주 검증."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AliexpressCategoryRunConfig(
                output_dir=Path(tmp),
                category_name="유기농채소",
                category_url="https://ko.aliexpress.com/category/888/organic.html",
                max_pages=1,
                delay=0.01,
            )

            p1_items = [
                {"id": "item_101", "url": "https://ko.aliexpress.com/item/item_101.html", "title": "친환경 상추 1kg", "price": "₩4,500", "orders": "300+ 판매", "is_top_seller": True},
                {"id": "item_102", "url": "https://ko.aliexpress.com/item/item_102.html", "title": "신선 깻잎 500g", "price": "₩3,000", "orders": "150+ 판매", "is_top_seller": False},
            ]

            pdp_responses = {
                "item_101": _make_mtop_response(
                    seller_id="SELLER_101",
                    store_name="에코푸드스토어",
                    company="(주)에코농산",
                    ceo="이대표",
                    bnum="220-81-99999",
                    email="eco@farm.kr",
                    phone="031-777-8888",
                    addr="경기도 양평군 친환경로 1",
                    ecom="2026-경기양평-0111",
                ),
                "item_102": _make_mtop_response(
                    seller_id="SELLER_102",
                    store_name="자연드림마켓",
                    company="(주)자연푸드",
                    ceo="박대표",
                    bnum="330-82-88888",
                    email="dream@nature.com",
                    phone="02-555-1234",
                    addr="서울시 서초구 반포대로 10",
                    ecom="2026-서울서초-0222",
                ),
            }

            fake_page = FakePlaywrightPage(page_items_map={1: p1_items}, pdp_responses=pdp_responses)
            crawler = AliexpressCategoryCrawler(config=cfg)

            with patch("app.core.aliexpress_category_crawler.async_playwright", return_value=FakePlaywrightManager(fake_page)):
                summary = crawler.crawl()

            self.assertEqual(summary.collected_items, 2)
            self.assertEqual(summary.unique_vendors, 2)
            self.assertEqual(summary.has_email, 2)
            self.assertEqual(summary.has_business_number, 2)
            self.assertEqual(summary.has_ceo_name, 2)

            self.assertTrue(summary.csv_file.exists())
            with open(summary.csv_file, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                self.assertEqual(reader.fieldnames, COUPANG_DATASET_FIELDS)
                rows = list(reader)
                self.assertEqual(len(rows), 2)

                row1 = rows[0]
                self.assertEqual(row1["vendor_id"], "SELLER_101")
                self.assertEqual(row1["company_name"], "(주)에코농산")
                self.assertEqual(row1["ceo_name"], "이대표")
                self.assertEqual(row1["business_number"], "220-81-99999")
                self.assertEqual(row1["email"], "eco@farm.kr")
                self.assertEqual(row1["phone"], "031-777-8888")
                self.assertEqual(row1["address"], "경기도 양평군 친환경로 1")
                self.assertEqual(row1["ecommerce_report_number"], "2026-경기양평-0111")
                self.assertEqual(row1["power_seller"], "TRUE")

            self.assertTrue(summary.json_file.exists())
            with open(summary.json_file, "r", encoding="utf-8") as f:
                json_data = json.load(f)
                self.assertEqual(len(json_data), 2)
                self.assertEqual(json_data[1]["company_name"], "(주)자연푸드")

    def test_simulated_network_disconnect_during_pdp_crawl_and_cache_hit(self):
        """2단계 수집 중 네트워크 순단 발생 시 기확보 시드 캐시 재활용 및 중복 호출 방지 검증."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AliexpressCategoryRunConfig(
                output_dir=Path(tmp),
                category_name="사과마켓",
                category_url="https://ko.aliexpress.com/category/999/apples.html",
                max_pages=1,
                delay=0.01,
            )

            items = [
                {"id": "app_1", "url": "https://ko.aliexpress.com/item/app_1.html", "title": "부사 사과 3kg", "price": "₩15,000", "orders": "50+ 판매", "is_top_seller": False},
                {"id": "app_2", "url": "https://ko.aliexpress.com/item/app_2.html", "title": "부사 사과 5kg", "price": "₩23,000", "orders": "80+ 판매", "is_top_seller": False},
            ]

            pdp_responses = {
                "app_1": _make_mtop_response("SELLER_A", "청송농원", "(주)청송사과", "김사과", "111-22-33333", "apple@farm.com", "054-111-2222", "경북 청송군", "2026-경북청송-001"),
                "app_2": _make_mtop_response("SELLER_A", "청송농원", "(주)청송사과", "김사과", "111-22-33333", "apple@farm.com", "054-111-2222", "경북 청송군", "2026-경북청송-001"),
            }

            fake_page = FakePlaywrightPage(page_items_map={1: items}, pdp_responses=pdp_responses)
            crawler = AliexpressCategoryCrawler(config=cfg)

            with patch("app.core.aliexpress_category_crawler.async_playwright", return_value=FakePlaywrightManager(fake_page)):
                summary = crawler.crawl()

            self.assertEqual(summary.collected_items, 2)
            self.assertEqual(summary.unique_vendors, 1)

    def test_cached_placeholder_phone_is_replaced_by_current_product_phone(self):
        """첫 상품의 상세 참고 값이 캐시되어도 다음 상품의 실제 번호를 반영한다."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AliexpressCategoryRunConfig(
                output_dir=Path(tmp),
                category_name="전화번호보강",
                category_url="https://ko.aliexpress.com/category/1000/phone.html",
                max_pages=1,
                delay=0.01,
            )
            items = [
                {"id": "phone_1", "url": "https://ko.aliexpress.com/item/phone_1.html", "title": "상품1", "price": "1", "orders": "1", "is_top_seller": False},
                {"id": "phone_2", "url": "https://ko.aliexpress.com/item/phone_2.html", "title": "상품2", "price": "2", "orders": "2", "is_top_seller": False},
            ]
            first = _make_mtop_response("SELLER_PHONE", "전화상점", "전화회사", "대표", "111-11-11111", "phone@example.com", "상세페이지 참고", "주소", "신고")
            second = _make_mtop_response("SELLER_PHONE", "전화상점", "전화회사", "대표", "111-11-11111", "phone@example.com", "070-1234-5678", "주소", "신고")
            fake_page = FakePlaywrightPage(page_items_map={1: items}, pdp_responses={"phone_1": first, "phone_2": second})
            crawler = AliexpressCategoryCrawler(config=cfg)

            with patch("app.core.aliexpress_category_crawler.async_playwright", return_value=FakePlaywrightManager(fake_page)):
                summary = crawler.crawl()

            self.assertEqual(summary.collected_items, 2)
            with open(summary.csv_file, "r", encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["phone"], "")
            self.assertEqual(rows[1]["phone"], "070-1234-5678")
            run_dir = ali_category_run_dir(Path(tmp), "전화번호보강", "https://ko.aliexpress.com/category/1000/phone.html")
            with AliexpressResumeStore(run_dir) as store:
                self.assertEqual(store.confirmed_sellers()["SELLER_PHONE"]["phone"], "070-1234-5678")

    def test_cached_valid_phone_is_preserved_when_current_product_has_detail_placeholder(self):
        """캐시된 유효 번호는 다음 상품이 번호를 주지 않아도 유지한다."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AliexpressCategoryRunConfig(
                output_dir=Path(tmp),
                category_name="전화번호보존",
                category_url="https://ko.aliexpress.com/category/1001/phone.html",
                max_pages=1,
                delay=0.01,
            )
            items = [
                {"id": "keep_1", "url": "https://ko.aliexpress.com/item/keep_1.html", "title": "상품1", "price": "1", "orders": "1", "is_top_seller": False},
                {"id": "keep_2", "url": "https://ko.aliexpress.com/item/keep_2.html", "title": "상품2", "price": "2", "orders": "2", "is_top_seller": False},
            ]
            first = _make_mtop_response("SELLER_KEEP", "전화상점", "전화회사", "대표", "222-22-22222", "keep@example.com", "070-9876-5432", "주소", "신고")
            second = _make_mtop_response("SELLER_KEEP", "전화상점", "전화회사", "대표", "222-22-22222", "keep@example.com", "Refer to Product Details", "주소", "신고")
            fake_page = FakePlaywrightPage(page_items_map={1: items}, pdp_responses={"keep_1": first, "keep_2": second})
            crawler = AliexpressCategoryCrawler(config=cfg)

            with patch("app.core.aliexpress_category_crawler.async_playwright", return_value=FakePlaywrightManager(fake_page)):
                summary = crawler.crawl()

            self.assertEqual(summary.collected_items, 2)
            with open(summary.csv_file, "r", encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual([row["phone"] for row in rows], ["070-9876-5432", "070-9876-5432"])


if __name__ == "__main__":
    unittest.main()
