"""Ali 카테고리 — 차단 → 안전 중단 → 우회 회선 재시작(중단 지점 재개) 검증.

쿠팡 카테고리 탭(decodo_run)과 동일해진 규율을 AliExpress 엔진에서 확인한다:
- 연속 차단 시 저장 지점을 남긴 채 안전 중단 (termination_reason="blocked")
- 다시 시작하면 resume.sqlite3 기준 중단 지점부터 이어서 수집
- 재개 경계는 상품이 확인된 마지막 페이지(last_item_page) — 경계 페이지를 다시
  확인하고, 0건으로 기록된 페이지도 다시 읽어 상품 누락을 막는다
- 재개 시 빈 페이지 연속 판정(empty_streak)은 0으로 리셋 — 이전 실행의
  카운트 이월로 목록이 조기 봉인되는 일을 막는다
- punish/tmd 차단 회선은 즉시 새 세션으로 교체, 회선은 한국 출발만 채택
- 상품 데이터(mtop) 미수신 페이지는 기록하지 않는다 (빈 껍데기 확정 저장 금지)
- 자격 미완비·설정 불일치는 하드 중단, 완료 봉인 기록은 아카이브 후 새 수집
"""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core import decodo
from app.core.aliexpress_category_crawler import (
    ALI_RESOURCE_BLOCK_PATTERNS,
    COUPANG_DATASET_FIELDS,
    CONSECUTIVE_BLOCK_ABORT,
    HOME_WARMUP_URL,
    ITEM_GIVE_UP_RUNS,
    JSON_FLUSH_INTERVAL,
    AliexpressCategoryCrawler,
    AliexpressCategoryRunConfig,
    _safe_mtop_error_code,
)
from app.core.aliexpress_resume_store import (
    AliexpressResumeStore,
    ali_category_run_dir,
    peek_resume,
)
from app.core.base import Control

try:
    from PyQt6.QtWidgets import QApplication
    from app.ui.aliexpress_category_panel import AliexpressCategoryPanel
    _QT_OK = True
except ImportError:
    _QT_OK = False

_app = None


def setUpModule():
    global _app
    if _QT_OK:
        _app = QApplication.instance() or QApplication(sys.argv)


# ── Fakes ───────────────────────────────────────────────────────────────

KR_INFO = decodo.ExitIpInfo(ip="1.2.3.4", country_code="KR", country_name="South Korea")
US_INFO = decodo.ExitIpInfo(ip="8.8.8.8", country_code="US", country_name="United States")


class ScriptedPage:
    """목록/PDP 응답을 시나리오 대로 흘려보내는 Fake Page.

    pdp_plan[item_id] = ["punish", "ok", "nodata", ...] — 같은 URL 의 n번째
    goto 동작. 목록이 소진되면 마지막 동작을 반복한다.
    """

    def __init__(self, listing_map=None, pdp_payloads=None, pdp_plan=None):
        self.listing_map = listing_map or {}
        self.pdp_payloads = pdp_payloads or {}
        self.pdp_plan = pdp_plan or {}
        self.goto_urls: list[str] = []
        self._goto_counts: dict[str, int] = {}
        self._listeners: dict[str, list] = {}
        self.url = ""
        self.cdp = None
        self.event_log: list[tuple[str, str]] = []

    async def goto(self, url: str, **kwargs):
        n = self._goto_counts.get(url, 0) + 1
        self._goto_counts[url] = n
        self.goto_urls.append(url)
        self.url = url
        self.event_log.append(("goto", url))
        if self.cdp is not None:
            self.cdp.emit("Network.loadingFinished", {"encodedDataLength": 1024})

        for item_id, payload in self.pdp_payloads.items():
            if f"/item/{item_id}.html" not in url:
                continue
            plan = self.pdp_plan.get(item_id, ["ok"])
            behavior = plan[min(n - 1, len(plan) - 1)]
            if behavior == "punish":
                self.url = f"https://ko.aliexpress.com/_____tmd_____/punish?item={item_id}"
                return
            if behavior == "nodata":
                return
            if payload is None:
                raise RuntimeError(f"Simulated network error for item {item_id}")

            class FakeResponse:
                def __init__(self, payload):
                    self.url = "https://acs.aliexpress.com/h5/mtop.aliexpress.pdp.pc.query/1.0/"
                    self.status = 200
                    self._payload = payload

                async def body(self):
                    return json.dumps(self._payload).encode("utf-8")

            for handler in list(self._listeners.get("response", [])):
                await handler(FakeResponse(payload))
            return

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
            return self.listing_map.get(p_num, [])
        return None

    def on(self, event: str, handler):
        self._listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler):
        if event in self._listeners and handler in self._listeners[event]:
            self._listeners[event].remove(handler)

    async def close(self):
        pass


class PunishListingPage(ScriptedPage):
    """지정 목록 페이지 번호에 punish 차단을 발동하는 Fake Page.

    punish_times 회까지는 차단, 그 뒤에는 정상 응답 — 1차 실행(영구 차단)과
    2차 실행(우회 회선으로 차단 해제) 시나리오를 한 클래스로 처리한다.
    """

    def __init__(self, *args, punish_pages=(), punish_times=99, **kwargs):
        super().__init__(*args, **kwargs)
        self.punish_pages = set(punish_pages)
        self.punish_times = punish_times
        self._punish_hits = 0

    async def goto(self, url: str, **kwargs):
        await super().goto(url, **kwargs)
        if any(f"page={p}" in url for p in self.punish_pages):
            self._punish_hits += 1
            if self._punish_hits <= self.punish_times:
                self.url = f"https://ko.aliexpress.com/_____tmd_____/punish?{self.url}"


class FakeContext:
    def __init__(self, page: ScriptedPage):
        self._page = page
        self.cdp = FakeCdpSession(page.event_log)

    async def add_cookies(self, cookies):
        pass

    async def new_page(self):
        self._page.cdp = self.cdp
        return self._page

    async def new_cdp_session(self, page):
        return self.cdp

    async def close(self):
        pass


class FakeBrowser:
    def __init__(self, context: FakeContext):
        self._context = context

    async def new_context(self, **kwargs):
        return self._context

    async def close(self):
        pass


class FakeCdpSession:
    def __init__(self, event_log=None):
        self.commands: list[tuple[str, dict | None]] = []
        self._listeners: dict[str, list] = {}
        self.event_log = event_log

    async def send(self, method: str, params=None):
        self.commands.append((method, params))
        if self.event_log is not None:
            self.event_log.append(("cdp", method))

    def on(self, event: str, handler):
        self._listeners.setdefault(event, []).append(handler)

    def emit(self, event: str, payload):
        for handler in list(self._listeners.get(event, [])):
            handler(payload)


class FakePlaywrightManager:
    """launch 기록(count/proxy)을 남기는 Fake Playwright."""

    def __init__(self, page: ScriptedPage, record: dict):
        self._page = page
        self._record = record
        self.chromium = MagicMock()

        async def launch(**kwargs):
            self._record["launch_count"] += 1
            self._record["proxies"].append(kwargs.get("proxy"))
            return FakeBrowser(FakeContext(self._page))

        self.chromium.launch = launch

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


def make_mtop(seller_id: str, company: str, ceo: str = "홍길동", bnum: str = "123-45-67890",
              email: str = "shop@example.com") -> dict:
    return {
        "data": {
            "result": {
                "SHOP_CARD_PC": {
                    "storeName": f"store_{seller_id}",
                    "benefitInfoList": [{"title": "긍정적 피드백", "value": "98.5%"}],
                },
                "GLOBAL_DATA": {"globalData": {"sellerId": seller_id}},
                "PRODUCT_PROP_PC": {
                    "showedProps": [
                        {"attrName": "상호명", "attrValue": company},
                        {"attrName": "대표자명", "attrValue": ceo},
                        {"attrName": "사업자번호", "attrValue": bnum},
                        {"attrName": "이메일 주소", "attrValue": email},
                    ]
                },
            }
        }
    }


def make_products(prefix: str, count: int) -> list[dict]:
    return [
        {
            "id": f"{prefix}{i}",
            "url": f"https://ko.aliexpress.com/item/{prefix}{i}.html",
            "title": f"상품 {prefix}{i}",
            "price": "1,000",
            "orders": "10 판매",
            "is_top_seller": False,
        }
        for i in range(1, count + 1)
    ]


class CrawlHarness:
    """크롤러 실행과 launch/회선 기록을 묶은 헬퍼."""

    def __init__(self, page: ScriptedPage, use_proxy: bool, exit_results=None):
        self.page = page
        self.record = {"launch_count": 0, "proxies": []}
        self.sids: list[str] = []
        self.exit_calls = 0
        self.exit_results = list(exit_results) if exit_results is not None else None
        self.manager = FakePlaywrightManager(page, self.record)

    def sticky_stub(self, settings=None, session_id=""):
        self.sids.append(session_id)
        return {
            "server": "http://gate.decodo.com:7000",
            "username": f"user-u-session-{session_id}-sessionduration-1440-country-kr",
            "password": "p",
        }

    def exit_ip_stub(self, proxy):
        self.exit_calls += 1
        if self.exit_results is None:
            return KR_INFO
        idx = min(self.exit_calls - 1, len(self.exit_results) - 1)
        return self.exit_results[idx]

    def crawl(self, config: AliexpressCategoryRunConfig, control: Control | None = None, **kw):
        crawler = AliexpressCategoryCrawler(
            config=config,
            control=control or Control(),
            exit_ip_fn=self.exit_ip_stub,
            **kw,
        )
        with patch("app.core.aliexpress_category_crawler.async_playwright", return_value=self.manager), \
             patch("app.core.decodo.sticky_proxy_dict", side_effect=self.sticky_stub), \
             patch("app.core.aliexpress_category_crawler.RETRY_WAIT_SECONDS", 0):
            summary = crawler.crawl()
        return crawler, summary


def make_cfg(out_dir: Path, name="채소", url="https://ko.aliexpress.com/category/100/v.html", **kw):
    kw.setdefault("max_pages", 1)
    kw.setdefault("delay", 0)
    kw.setdefault("block_cooldown_seconds", 0)
    # 시뮬레이션 테스트는 실제 대기 없이 즉시 진행 — 회선 교체 스로틀 간격만
    # 끈다. 스로틀 동작 자체는 test_aliexpress_rotation_throttle.py 이 담당한다.
    kw.setdefault("rotation_min_interval_seconds", 0)
    return AliexpressCategoryRunConfig(output_dir=out_dir, category_name=name, category_url=url, **kw)


# ── Tests ───────────────────────────────────────────────────────────────

class TestPunishBlockRotation(unittest.TestCase):
    """punish 차단 → 즉시 회선 교체 → 회복 수집."""

    def test_punish_rotates_line_and_recovers(self):
        products = make_products("p", 3)
        with tempfile.TemporaryDirectory() as tmp:
            page = ScriptedPage(
                listing_map={1: products},
                pdp_payloads={p["id"]: make_mtop(f"S{i+1}", f"회사{i+1}") for i, p in enumerate(products)},
                pdp_plan={"p1": ["punish", "ok"]},
            )
            h = CrawlHarness(page, use_proxy=True)
            _, summary = h.crawl(make_cfg(Path(tmp), rotation_batch_size=100))

            self.assertEqual(summary.termination_reason, "success")
            self.assertEqual(summary.collected_items, 3, "차단 회복 후 3건 모두 수집")
            self.assertGreaterEqual(h.record["launch_count"], 2, "punish 감지로 세션이 교체돼야 합니다")
            self.assertGreaterEqual(len(set(h.sids)), 2, "회선 교체 시 세션 ID 가 달라야 합니다")
            self.assertNotIn("(상세 미기재)", repr(summary.__dict__), "빈 껍데기 행이 없어야 합니다")


class TestSoftBlockGate(unittest.TestCase):
    """상품 데이터 미수신(소프트 차단) — 기록 금지 + 차단 중단."""

    def test_no_mtop_is_never_recorded_and_marks_blocked(self):
        products = make_products("q", 2)
        with tempfile.TemporaryDirectory() as tmp:
            page = ScriptedPage(
                listing_map={1: products},
                pdp_payloads={p["id"]: None for p in products},
                pdp_plan={p["id"]: ["nodata"] for p in products},
            )
            h = CrawlHarness(page, use_proxy=False)
            # 시도 1회로 제한 — 회선 교체 재시도까지 누적되면 포기 규율이
            # 개입하므로(TestGiveUpCounter), 이 테스트는 1회차 실패만 본다.
            with patch("app.core.aliexpress_category_crawler.MAX_ATTEMPTS", 1):
                _, summary = h.crawl(make_cfg(Path(tmp), use_proxy=False))

            self.assertEqual(summary.collected_items, 0, "데이터 없는 페이지는 기록되지 않아야 합니다")
            self.assertEqual(summary.termination_reason, "blocked")
            self.assertIn("다시 시작", summary.error or "")
            run_dir = ali_category_run_dir(Path(tmp), "채소", "https://ko.aliexpress.com/category/100/v.html")
            csv_path = run_dir / "ali_category_채소.csv"
            with open(csv_path, encoding="utf-8-sig") as f:
                rows = list(csv.reader(f))
            self.assertEqual(len(rows), 1, "헤더만 있어야 합니다 (빈 껍데기 행 금지)")
            store = AliexpressResumeStore(run_dir)
            with store:
                self.assertEqual(store.processed_item_ids(), set(), "실패 상품은 재개 대상으로 남아야 합니다")
                self.assertNotEqual(store.status, "finished", "실패 상품이 남으면 완료 봉인하지 않습니다")

    def test_consecutive_block_abort_stops_early(self):
        products = make_products("r", 5)
        with tempfile.TemporaryDirectory() as tmp:
            page = ScriptedPage(
                listing_map={1: products},
                pdp_payloads={p["id"]: None for p in products},
                pdp_plan={p["id"]: ["nodata"] for p in products},
            )
            h = CrawlHarness(page, use_proxy=False)
            with patch("app.core.aliexpress_category_crawler.CONSECUTIVE_BLOCK_ABORT", 2):
                _, summary = h.crawl(make_cfg(Path(tmp), use_proxy=False))

            self.assertEqual(summary.termination_reason, "blocked")
            self.assertEqual(summary.collected_items, 0)
            self.assertLess(h.page.goto_urls.count(
                "https://ko.aliexpress.com/item/r3.html"), 1,
                "차단기 도달 후 남은 상품을 계속 긁으면 안 됩니다")

    def test_module_abort_constant_is_ten(self):
        self.assertEqual(CONSECUTIVE_BLOCK_ABORT, 10)


class TestBlockStopThenResume(unittest.TestCase):
    """중단(취소) → 다시 시작 → 중단 지점부터 이어서 수집."""

    def test_cancel_midrun_then_resume_from_stop_point(self):
        products = make_products("s", 4)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            page = ScriptedPage(
                listing_map={1: products},
                pdp_payloads={p["id"]: make_mtop(f"S{i+1}", f"회사{i+1}") for i, p in enumerate(products)},
            )
            control = Control()
            collected_a: list[dict] = []

            def on_collected(rec):
                collected_a.append(rec)
                if len(collected_a) >= 2:
                    control.request_cancel()

            h = CrawlHarness(page, use_proxy=True)
            _, summary_a = h.crawl(make_cfg(out), control=control, on_collected=on_collected)

            self.assertTrue(summary_a.cancelled)
            self.assertEqual(summary_a.collected_items, 2)
            run_dir = ali_category_run_dir(out, "채소", "https://ko.aliexpress.com/category/100/v.html")
            self.assertIsNotNone(peek_resume(run_dir), "미완료 진행 안내가 있어야 합니다")

            # ── 다시 시작(우회 회선) → 중단 지점부터 이어서 ──
            page_b = ScriptedPage(
                listing_map={1: products},
                pdp_payloads={p["id"]: make_mtop(f"S{i+1}", f"회사{i+1}") for i, p in enumerate(products)},
            )
            h_b = CrawlHarness(page_b, use_proxy=True)
            collected_b: list[dict] = []
            _, summary_b = h_b.crawl(make_cfg(out), on_collected=collected_b.append)

            self.assertEqual(summary_b.termination_reason, "success")
            urls_b = {r["url"] for r in collected_b}
            self.assertEqual(len(urls_b), 4, "재개 실행에 4건 모두 표시(기존 2 + 신규 2)")
            self.assertEqual(summary_b.collected_items, 4, "요약은 저장 누적 기준")
            csv_path = run_dir / "ali_category_채소.csv"
            with open(csv_path, encoding="utf-8-sig") as f:
                rows = [r for r in csv.DictReader(f)]
            self.assertEqual(len(rows), 4, "CSV 에 중복 없이 4건")
            self.assertIsNone(peek_resume(run_dir), "완료 봉인 후 재개 안내는 없어야 합니다")


class TestHardGates(unittest.TestCase):
    """자격 미완비·설정 불일치 하드 중단."""

    def test_missing_credentials_hard_stop_without_browser(self):
        products = make_products("t", 2)
        with tempfile.TemporaryDirectory() as tmp:
            page = ScriptedPage(listing_map={1: products})
            h = CrawlHarness(page, use_proxy=True)
            with patch("app.core.decodo.sticky_proxy_dict", return_value=None):
                with patch("app.core.aliexpress_category_crawler.async_playwright", return_value=h.manager):
                    from app.core.aliexpress_category_crawler import AliexpressCategoryCrawler as C
                    crawler = C(config=make_cfg(Path(tmp)), control=Control(), exit_ip_fn=h.exit_ip_stub)
                    summary = crawler.crawl()

            self.assertIn("Decodo 계정", summary.error or "")
            self.assertEqual(summary.termination_reason, "error")
            self.assertEqual(h.record["launch_count"], 0, "자격 없으면 브라우저를 열지 않습니다")

    def test_config_mismatch_hard_stop(self):
        url_a = "https://ko.aliexpress.com/category/100/v1.html"
        url_b = "https://ko.aliexpress.com/category/100/v2.html"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            run_dir = ali_category_run_dir(out, "채소", url_a)
            store = AliexpressResumeStore(run_dir)
            with store:
                store.check_config("채소", url_a, 3)
                store.record_page(1, make_products("u", 1))
            page = ScriptedPage(listing_map={1: make_products("u", 1)})
            h = CrawlHarness(page, use_proxy=True)
            _, summary = h.crawl(make_cfg(out, url=url_b))

            self.assertIn("일치하지 않아 재개를 중단", summary.error or "")
            self.assertIn("처음부터 다시 수집", summary.error or "")
            self.assertEqual(h.record["launch_count"], 0, "불일치 시 수집을 시작하지 않습니다")

    def test_finished_run_archives_then_recolllects(self):
        products = make_products("w", 1)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            page_a = ScriptedPage(
                listing_map={1: products},
                pdp_payloads={"w1": make_mtop("S1", "회사A")},
            )
            h_a = CrawlHarness(page_a, use_proxy=True)
            _, summary_a = h_a.crawl(make_cfg(out))
            self.assertEqual(summary_a.termination_reason, "success")

            run_dir = ali_category_run_dir(out, "채소", "https://ko.aliexpress.com/category/100/v.html")
            self.assertIsNone(peek_resume(run_dir))

            logs: list[str] = []
            page_b = ScriptedPage(
                listing_map={1: products},
                pdp_payloads={"w1": make_mtop("S1", "회사A")},
            )
            h_b = CrawlHarness(page_b, use_proxy=True)
            _, summary_b = h_b.crawl(make_cfg(out), on_log=logs.append)

            archives = list(run_dir.glob("resume_archive_*"))
            self.assertGreaterEqual(len(archives), 1, "완료 봉인 기록은 아카이브로 보존돼야 합니다")
            self.assertTrue(any("보관하고 처음부터" in m for m in logs))
            self.assertEqual(summary_b.termination_reason, "success")
            self.assertEqual(summary_b.collected_items, 1, "보관 후 새로 수집")
            store = AliexpressResumeStore(run_dir)
            with store:
                self.assertEqual(store.status, "finished")


class TestKoreaLineVerification(unittest.TestCase):
    """회선 확보 시 한국 출발 확인 — 아니면 교체, 끝내 못 얻으면 중단."""

    def test_non_korea_lines_are_swapped(self):
        products = make_products("x", 1)
        with tempfile.TemporaryDirectory() as tmp:
            page = ScriptedPage(
                listing_map={1: products},
                pdp_payloads={"x1": make_mtop("S1", "회사X")},
            )
            h = CrawlHarness(page, use_proxy=True, exit_results=[US_INFO, US_INFO, KR_INFO])
            _, summary = h.crawl(make_cfg(Path(tmp)))

            self.assertEqual(summary.termination_reason, "success")
            self.assertGreaterEqual(h.exit_calls, 3, "한국 확인 전까지 회선을 갈아끼웁니다")
            self.assertGreaterEqual(len(set(h.sids)), 3, "교체마다 새 세션 ID")

    def test_no_korea_line_aborts_with_line_error(self):
        products = make_products("y", 1)
        with tempfile.TemporaryDirectory() as tmp:
            page = ScriptedPage(
                listing_map={1: products},
                pdp_payloads={"y1": make_mtop("S1", "회사Y")},
            )
            h = CrawlHarness(page, use_proxy=True, exit_results=[US_INFO])
            _, summary = h.crawl(make_cfg(Path(tmp)))

            self.assertEqual(summary.termination_reason, "line_error")
            self.assertIn("한국 회선을 확보하지 못했습니다", summary.error or "")
            self.assertEqual(h.record["launch_count"], 0, "확인 안 된 회선으로는 열지 않습니다")


class TestScheduledRotation(unittest.TestCase):
    """rotation_batch_size 건마다 회선 순환."""

    def test_batch_rotation_creates_new_sessions(self):
        products = make_products("z", 5)
        with tempfile.TemporaryDirectory() as tmp:
            page = ScriptedPage(
                listing_map={1: products},
                pdp_payloads={p["id"]: make_mtop(f"S{i+1}", f"회사{i+1}") for i, p in enumerate(products)},
            )
            h = CrawlHarness(page, use_proxy=True)
            _, summary = h.crawl(make_cfg(Path(tmp), rotation_batch_size=2))

            self.assertEqual(summary.termination_reason, "success")
            self.assertEqual(summary.collected_items, 5)
            self.assertGreaterEqual(h.record["launch_count"], 3, "5건/2건 배치 → 초기+교체 2회 이상")
            self.assertEqual(len(h.sids), len(set(h.sids)), "세션 ID 는 모두 달라야 합니다")

    def test_cdp_resource_blocks_are_installed_before_warmup(self):
        products = make_products("bandwidth", 1)
        with tempfile.TemporaryDirectory() as tmp:
            page = ScriptedPage(
                listing_map={1: products},
                pdp_payloads={"bandwidth1": make_mtop("S1", "회사1")},
            )
            h = CrawlHarness(page, use_proxy=True)
            _, summary = h.crawl(make_cfg(Path(tmp)))

        methods = [method for method, _ in page.cdp.commands]
        self.assertLess(
            page.event_log.index(("cdp", "Network.setBlockedURLs")),
            page.event_log.index(("goto", HOME_WARMUP_URL)),
        )
        blocked = dict(page.cdp.commands)["Network.setBlockedURLs"]["urls"]
        self.assertEqual(tuple(blocked), ALI_RESOURCE_BLOCK_PATTERNS)
        self.assertIn("https://ae-pic-a1.aliexpress-media.com/*", blocked)
        self.assertNotIn("https://assets.aliexpress-media.com/*", blocked)
        self.assertGreater(summary.estimated_browser_bytes, 0)
        self.assertEqual(summary.browser_bytes_by_session[-1], summary.estimated_browser_bytes)

    # 2026-09-29: 회선 소모 예산(교체 횟수 상한) 폐지로 예산 발동 테스트 2건을
    # 삭제했다. 회선이 많이 나와도 수집은 계속 진행한다(사용자 결정).


class TestResumeStoreEmptyStreak(unittest.TestCase):
    """record_page new_count — 크롤러 중단 판정과 저장값 일치."""

    def test_duplicate_page_counts_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AliexpressResumeStore(Path(tmp))
            with store:
                store.check_config("채소", "https://x/category/1/a.html", 5)
                store.record_page(1, make_products("a", 2), new_count=2)
                self.assertEqual(store.empty_streak, 0)
                store.record_page(2, make_products("a", 2), new_count=0)  # 전부 중복
                self.assertEqual(store.empty_streak, 1, "신규 0개면 빈 페이지로 센다")
                store.record_page(3, make_products("a", 2), new_count=0)
                self.assertEqual(store.empty_streak, 2, "연속 2 → 크롤러가 순회를 종료하는 지점")

    def test_legacy_call_keeps_old_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AliexpressResumeStore(Path(tmp))
            with store:
                store.check_config("채소", "https://x/category/1/a.html", 5)
                store.record_page(1, make_products("a", 1))
                store.record_page(2, make_products("a", 1))  # 구버전: 상품만 있으면 리셋
                self.assertEqual(store.empty_streak, 0)


class TestResumeBoundary(unittest.TestCase):
    """재개 경계 규율(쿠팡 search_crawler 동일) — 0건 페이지 재확인 + streak 리셋."""

    def test_store_last_item_page_ignores_zero_item_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AliexpressResumeStore(Path(tmp))
            with store:
                store.check_config("채소", "https://x/category/1/b.html", 5)
                store.record_page(1, make_products("k", 2), new_count=2)
                store.record_page(2, [], new_count=0)
            with store:
                self.assertEqual(store.last_completed_page, 2, "0건 페이지도 기록 자체는 완료다")
                self.assertEqual(store.last_item_page, 1, "재개 경계는 상품이 확인된 마지막 페이지다")

    def test_zero_item_page_before_block_is_reread_on_resume(self):
        """0건 기록 페이지(소프트 차단·렌더 지연) 직후 차단 → 재개 시 그 페이지를 다시 읽는다."""
        prods = make_products("n", 2)
        late_prods = make_products("f", 3)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            # 1차: 페이지1=2건 기록 / 페이지2=렌더 지연으로 0건 '기록' / 페이지3=punish → 중단
            page_a = PunishListingPage(listing_map={1: prods, 2: []}, punish_pages=(3,))
            h_a = CrawlHarness(page_a, use_proxy=False)
            _, summary_a = h_a.crawl(make_cfg(out, max_pages=3, use_proxy=False))
            self.assertEqual(summary_a.termination_reason, "blocked")

            run_dir = ali_category_run_dir(out, "채소", "https://ko.aliexpress.com/category/100/v.html")
            with AliexpressResumeStore(run_dir) as store:
                self.assertEqual(store.last_completed_page, 2, "0건 페이지 2도 기록됐다")
                self.assertEqual(store.last_item_page, 1, "경계는 상품이 있는 페이지 1이다")

            # 2차(우회): 페이지2에 실제로는 상품 3건이 있었다
            page_b = PunishListingPage(
                listing_map={1: prods, 2: late_prods, 3: []},
                punish_pages=(),
                pdp_payloads={
                    **{p["id"]: make_mtop(f"S{i+1}", f"회사{i+1}") for i, p in enumerate(prods)},
                    **{p["id"]: make_mtop(f"S{i+10}", f"회사{i+10}") for i, p in enumerate(late_prods)},
                },
            )
            h_b = CrawlHarness(page_b, use_proxy=True)
            collected: list[dict] = []
            _, summary_b = h_b.crawl(make_cfg(out, max_pages=3, use_proxy=True), on_collected=collected.append)

            page2_reads = [u for u in page_b.goto_urls if "page=2" in u]
            self.assertGreaterEqual(len(page2_reads), 1, "재개 시 0건으로 기록된 페이지2를 다시 확인해야 합니다")
            self.assertEqual(summary_b.termination_reason, "success")
            self.assertEqual(summary_b.collected_items, 5, "페이지1의 2건 + 재확인된 페이지2의 3건")

    def test_carried_empty_streak_does_not_seal_listing_after_resume(self):
        """이월된 streak=1 + 재개 첫 페이지도 0건 → 조기 봉인하지 않고 다음 페이지를 읽는다.

        경계가 없는(boundary=0) 실행 — 모든 기록 페이지가 0건 — 이므로 이월값
        리셋 여부가 곧바로 갈린다. 리셋이 없으면 첫 0건 재읽음 한 장으로
        streak=2 가 되어 목록을 봉인해 버린다.
        """
        late_prods = make_products("e", 2)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            # 1차: 페이지1=렌더 지연으로 0건(streak=1) / 페이지2=punish → 중단
            page_a = PunishListingPage(listing_map={1: []}, punish_pages=(2,))
            h_a = CrawlHarness(page_a, use_proxy=False)
            _, summary_a = h_a.crawl(make_cfg(out, max_pages=3, use_proxy=False))
            self.assertEqual(summary_a.termination_reason, "blocked")

            run_dir = ali_category_run_dir(out, "채소", "https://ko.aliexpress.com/category/100/v.html")
            with AliexpressResumeStore(run_dir) as store:
                self.assertEqual(store.last_item_page, 0, "상품이 확인된 페이지가 없다 (경계 없음)")
                self.assertEqual(store.empty_streak, 1, "차단 직전 0건 페이지로 streak=1 이 남는다")

            # 2차(우회): 페이지1이 여전히 0건이어도 페이지2에 실제 상품이 있다
            page_b = PunishListingPage(
                listing_map={1: [], 2: late_prods, 3: []},
                punish_pages=(),
                pdp_payloads={p["id"]: make_mtop(f"S{i+10}", f"회사{i+10}") for i, p in enumerate(late_prods)},
            )
            h_b = CrawlHarness(page_b, use_proxy=True)
            collected: list[dict] = []
            _, summary_b = h_b.crawl(make_cfg(out, max_pages=3, use_proxy=True), on_collected=collected.append)

            page2_reads = [u for u in page_b.goto_urls if "page=2" in u]
            self.assertGreaterEqual(
                len(page2_reads), 1,
                "streak 리셋 덕에 재개 첫 페이지가 0건이어도 다음 페이지를 계속 읽어야 합니다",
            )
            self.assertEqual(summary_b.termination_reason, "success")
            self.assertEqual(summary_b.collected_items, 2, "페이지2의 신규 2건")

    def test_boundary_reread_merges_shifted_items(self):
        """경계 페이지 재확인 — 이미 지나간 페이지로 밀려온 신규 상품도 수집한다."""
        prods = make_products("a", 2)
        shifted = make_products("x", 1)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            # 1차: 페이지1=2건 기록 / 페이지2=punish → 중단 (Phase 2 미진행)
            page_a = PunishListingPage(listing_map={1: prods}, punish_pages=(2,))
            h_a = CrawlHarness(page_a, use_proxy=False)
            _, summary_a = h_a.crawl(make_cfg(out, max_pages=2, use_proxy=False))
            self.assertEqual(summary_a.termination_reason, "blocked")

            # 2차(우회): 페이지1에 신규 상품 x1 이 밀려와 있다
            page_b = PunishListingPage(
                listing_map={1: prods + shifted, 2: []},
                punish_pages=(),
                pdp_payloads={
                    **{p["id"]: make_mtop(f"S{i+1}", f"회사{i+1}") for i, p in enumerate(prods)},
                    **{p["id"]: make_mtop("S77", "회사X") for p in shifted},
                },
            )
            h_b = CrawlHarness(page_b, use_proxy=True)
            collected: list[dict] = []
            _, summary_b = h_b.crawl(make_cfg(out, max_pages=2, use_proxy=True), on_collected=collected.append)

            boundary_reads = [u for u in page_b.goto_urls if "page=1" in u]
            self.assertGreaterEqual(len(boundary_reads), 1, "재개 시 경계 페이지(1)를 다시 확인해야 합니다")
            self.assertEqual(summary_b.termination_reason, "success")
            self.assertEqual(
                summary_b.collected_items, 3,
                "경계 재확인으로 밀려온 x1 도 누적에 반영되어야 합니다",
            )


class TestFailureRetention(unittest.TestCase):
    """회선·mtop 실패 상품은 영구 제외하지 않고 재개 대상으로 남긴다."""

    URL = "https://ko.aliexpress.com/category/100/v.html"

    def test_repeated_network_failure_is_pending_on_every_run(self):
        """반복 실패가 쌓여도 상품을 포기하지 않고 다음 실행으로 넘긴다."""
        products = make_products("g", 2)  # g1 = 실패, g2 = 정상
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            run_dir = ali_category_run_dir(out, "채소", self.URL)

            def make_page():
                return ScriptedPage(
                    listing_map={1: products},
                    pdp_payloads={"g1": None, "g2": make_mtop("S2", "회사2")},
                    pdp_plan={"g1": ["nodata"], "g2": ["ok"]},
                )

            summaries = []
            g1_failures = []
            for _ in range(3):
                h = CrawlHarness(make_page(), use_proxy=False)
                # 실행 1회 = 시도 1회 — 실행 경계 간 누적을 명확히 보기 위해서다.
                with patch("app.core.aliexpress_category_crawler.MAX_ATTEMPTS", 1):
                    _, s = h.crawl(make_cfg(out, use_proxy=False))
                summaries.append(s)
                with AliexpressResumeStore(run_dir) as store:
                    g1_failures.append(store.item_failure_counts().get("g1"))

            self.assertEqual([s.termination_reason for s in summaries], ["blocked"] * 3)
            self.assertEqual(g1_failures, [None, None, None], "실패 카운터를 영구 판정에 사용하지 않는다")
            with AliexpressResumeStore(run_dir) as store:
                self.assertNotIn("g1", store.processed_item_ids(), "실패 상품은 재개 대상으로 남아야 한다")
                self.assertNotEqual(store.status, "finished")

    def test_legacy_failure_counter_does_not_skip_recovered_item(self):
        """구버전 실패 카운터가 있어도 데이터가 살아 있으면 다시 수집한다."""
        products = make_products("u", 1)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            run_dir = ali_category_run_dir(out, "채소", self.URL)
            with AliexpressResumeStore(run_dir) as store:
                store.check_config("채소", self.URL, 1)
                store.record_page(1, products, new_count=1)
                store.mark_listing_done()
                for _ in range(ITEM_GIVE_UP_RUNS):
                    store.record_item_failure("u1")

            page = ScriptedPage(
                listing_map={1: products},
                pdp_payloads={"u1": make_mtop("S1", "회사1")},
            )
            h = CrawlHarness(page, use_proxy=False)
            _, summary = h.crawl(make_cfg(out, use_proxy=False))

            self.assertEqual(summary.termination_reason, "success")
            self.assertEqual(summary.given_up_items, 0)
            self.assertIn("https://ko.aliexpress.com/item/u1.html", page.goto_urls)
            with AliexpressResumeStore(run_dir) as store:
                self.assertEqual(store.status, "finished")

    def test_legacy_finished_run_with_pending_item_is_reopened(self):
        """구버전의 조기 완료 봉인도 성공 상품을 보존한 채 미확인분을 재개한다."""
        products = make_products("l", 2)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            run_dir = ali_category_run_dir(out, "채소", self.URL)
            with AliexpressResumeStore(run_dir) as store:
                store.check_config("채소", self.URL, 1)
                store.record_page(1, products, new_count=2)
                store.mark_listing_done()
                store.record_item_failure("l1")
                saved = {key: "" for key in COUPANG_DATASET_FIELDS}
                saved.update(vendor_id="S2", url=products[1]["url"], company_name="기존 성공")
                store.record_item_result("l2", "S2", saved)
                store.record_seller("S2", saved)
                store.mark_finished()  # 구버전이 남긴 상태
                self.assertEqual(store.pending_product_count(), 1)

            page = ScriptedPage(
                listing_map={1: products},
                pdp_payloads={
                    "l1": make_mtop("S1", "회사1"),
                    "l2": make_mtop("S2", "회사2"),
                },
            )
            h = CrawlHarness(page, use_proxy=False)
            _, summary = h.crawl(make_cfg(out, use_proxy=False))

            self.assertEqual(summary.termination_reason, "success")
            self.assertEqual(summary.collected_items, 2)
            self.assertNotIn(products[1]["url"], page.goto_urls)
            with AliexpressResumeStore(run_dir) as store:
                preserved = next(r for r in store.load_item_results() if r["vendor_id"] == "S2")
                self.assertEqual(preserved, saved)
            self.assertEqual(list(run_dir.glob("resume_archive_*.sqlite3")), [])


class TestPdpLineRotation(unittest.TestCase):
    """mtop 미수신 시 상품을 보류하고 회선을 바꾼 뒤 같은 상품을 재확인한다."""

    def test_proxy_recovery_stops_after_two_failed_products(self):
        products = make_products("bounded", 5)
        with tempfile.TemporaryDirectory() as tmp:
            page = ScriptedPage(
                listing_map={1: products},
                pdp_payloads={p["id"]: None for p in products},
                pdp_plan={p["id"]: ["nodata"] for p in products},
            )
            h = CrawlHarness(page, use_proxy=True)
            with patch("app.core.aliexpress_category_crawler.MAX_ATTEMPTS", 1):
                _, summary = h.crawl(make_cfg(Path(tmp), use_proxy=True))
            self.assertEqual(summary.termination_reason, "blocked")
            self.assertEqual(sum("/item/" in url for url in page.goto_urls), 6)
            self.assertNotIn(products[2]["url"], page.goto_urls)
            run_dir = ali_category_run_dir(Path(tmp), "채소", TestFailureRetention.URL)
            with AliexpressResumeStore(run_dir) as store:
                self.assertEqual(store.pending_product_count(), 5)
                self.assertEqual(store.item_failure_counts(), {})

    def test_missing_mtop_rotates_before_retrying_same_item(self):
        products = make_products("m", 1)
        logs: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            page = ScriptedPage(
                listing_map={1: products},
                pdp_payloads={"m1": make_mtop("S1", "회사1")},
                pdp_plan={"m1": ["nodata", "ok"]},
            )
            h = CrawlHarness(page, use_proxy=True)
            _, summary = h.crawl(make_cfg(Path(tmp), use_proxy=True), on_log=logs.append)

        self.assertEqual(summary.termination_reason, "success")
        self.assertEqual(summary.collected_items, 1)
        self.assertEqual(page.goto_urls.count("https://ko.aliexpress.com/item/m1.html"), 2)
        self.assertGreaterEqual(h.record["launch_count"], 2, "첫 실패 직후 새 회선을 열어야 한다")
        self.assertTrue(any("새 회선에서 즉시 재확인" in log for log in logs))

    def test_last_missing_attempt_does_not_rotate_again(self):
        products = make_products("n", 1)
        with tempfile.TemporaryDirectory() as tmp:
            page = ScriptedPage(
                listing_map={1: products},
                pdp_payloads={"n1": None},
                pdp_plan={"n1": ["nodata"]},
            )
            h = CrawlHarness(page, use_proxy=True)
            with patch("app.core.aliexpress_category_crawler.MAX_ATTEMPTS", 1):
                _, summary = h.crawl(make_cfg(Path(tmp), use_proxy=True))

        self.assertEqual(summary.termination_reason, "blocked")
        self.assertEqual(h.record["launch_count"], 3, "3회 시도에 필요한 초기 세션+중간 교체만 열어야 한다")
        self.assertEqual(summary.given_up_items, 0)

    def test_missing_data_log_contains_safe_diagnostic(self):
        products = make_products("d", 1)
        logs: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            page = ScriptedPage(
                listing_map={1: products},
                pdp_payloads={"d1": None},
                pdp_plan={"d1": ["nodata"]},
            )
            h = CrawlHarness(page, use_proxy=False)
            with patch("app.core.aliexpress_category_crawler.MAX_ATTEMPTS", 1):
                h.crawl(make_cfg(Path(tmp), use_proxy=False), on_log=logs.append)

        self.assertTrue(any("mtop API 응답 없음" in log for log in logs))
        self.assertFalse(any('"data"' in log for log in logs))

    def test_missing_data_log_only_contains_sanitized_ret_code(self):
        products = make_products("code", 1)
        logs: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            page = ScriptedPage(
                listing_map={1: products},
                pdp_payloads={"code1": {"ret": ["FAIL_SYS_BLOCKED::private-token"]}},
            )
            h = CrawlHarness(page, use_proxy=False)
            with patch("app.core.aliexpress_category_crawler.MAX_ATTEMPTS", 1):
                h.crawl(make_cfg(Path(tmp), use_proxy=False), on_log=logs.append)

        self.assertTrue(any("payload_fields_missing (ret FAIL_SYS_BLOCKED)" in log for log in logs))
        self.assertFalse(any("private-token" in log for log in logs))

    def test_empty_mtop_ret_code_is_ignored_safely(self):
        self.assertIsNone(_safe_mtop_error_code({"ret": ["", "  ", None]}))


class TestJsonFlushInterval(unittest.TestCase):
    """결과 JSON 주기 재작성 — 매건 재작성 O(n²) 완화 (검토 2026-09-18)."""

    def test_flush_interval_keeps_final_json_complete(self):
        products = make_products("j", 3)
        with tempfile.TemporaryDirectory() as tmp:
            page = ScriptedPage(
                listing_map={1: products},
                pdp_payloads={p["id"]: make_mtop(f"S{i+1}", f"회사{i+1}") for i, p in enumerate(products)},
            )
            h = CrawlHarness(page, use_proxy=True)
            with patch("app.core.aliexpress_category_crawler.JSON_FLUSH_INTERVAL", 1):
                _, summary = h.crawl(make_cfg(Path(tmp)))

            self.assertEqual(summary.termination_reason, "success")
            run_dir = ali_category_run_dir(Path(tmp), "채소", "https://ko.aliexpress.com/category/100/v.html")
            with open(run_dir / "ali_category_채소.json", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(len(data), 3, "주기 플러시를 써도 최종 JSON 은 전체 건수를 담는다")

    def test_flush_interval_constant_is_fifty(self):
        self.assertEqual(JSON_FLUSH_INTERVAL, 50)


@unittest.skipUnless(_QT_OK, "PyQt6 필요")
class TestPanelTerminalStates(unittest.TestCase):
    """종료 상태에서 다시 시작 가능 — 차단 중단 뒤 우회 재시작 UX."""

    def setUp(self):
        self.panel = AliexpressCategoryPanel()

    def tearDown(self):
        self.panel.deleteLater()

    def test_start_reenabled_after_finished_and_failed(self):
        for state in ("finished", "failed"):
            self.panel.set_state(state)
            self.assertTrue(self.panel.btn_start.isEnabled(), f"{state} 에서 시작 가능해야 합니다")
            self.assertTrue(self.panel.edit_output_dir.isEnabled())

    def test_cancelling_keeps_everything_disabled(self):
        self.panel.set_state("running")
        self.panel.set_state("cancelling")
        self.assertFalse(self.panel.btn_start.isEnabled())
        self.assertFalse(self.panel.btn_cancel.isEnabled())

    def test_open_folder_emits_typed_path(self):
        got: list[str] = []
        self.panel.open_output_requested.connect(got.append)
        self.panel.edit_output_dir.setText("/tmp/ali_out")
        self.panel._open_output_folder()
        self.assertEqual(got, ["/tmp/ali_out"], "입력된 경로가 시그널로 전달돼야 합니다")


if __name__ == "__main__":
    unittest.main()
