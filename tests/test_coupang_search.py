"""키워드 검색 엔진(SearchCrawler) 테스트 — 네트워크 없이 fake page 사용.

검증 항목:
- 정렬 순회 + 로켓 제외 + 중복 제거 후 records 생성
- 차단 페이지(사용권한) 감지 시 즉시 중단
- 결과 0건 시 백오프 재시도 후 no_items 종료
- 파서 순수 함수(href/가격/차단 감지)
"""

import json
import tempfile
import unittest
from pathlib import Path

from app.core.base import Control
from app.core.coupang import blockguard
from app.core.coupang.crawler import COUPANG_HOME
from app.core.coupang.search_crawler import SearchCrawler, SearchRunConfig
from app.core.coupang.search_parser import (
    is_blocked,
    parse_extracted,
    parse_href,
    parse_price,
    parse_price_bands,
)

_OK_HTML = "<html><body>" + ("검색결과 콘텐츠 " * 400) + "</body></html>"
_BLOCKED_HTML = "<html><body>요청하신 페이지의 사용권한이 없습니다.</body></html>" + "x" * 2000
_AKAMAI_HTML = "<html><body>Access Denied<br>Reference #18.4a2b1c3.1234</body></html>" + "x" * 2000


def _row(viid: str, rocket: bool = False, title: str = "상품") -> dict:
    return {
        "href": f"/vp/products/1{viid}?itemId=2{viid}&vendorItemId={viid}&q=test",
        "title": title,
        "priceText": "9,920원",
        "rocket": rocket,
        "sponsored": False,
    }


def _individual_response(viids):
    products = [
        {
            "productId": f"P{v}",
            "itemId": f"2{v}",
            "vendorItemId": v,
            "storeInfoArea": {"vendorId": f"V{v}", "storeId": 109671, "displayName": f"스토어{v}"},
        }
        for v in viids
    ]
    return {"status": 200, "body": json.dumps({"code": 200, "data": {"products": products}})}


def _review_response():
    return {"status": 200, "body": json.dumps({
        "name": "테스트상호", "repPersonName": "대표", "businessNumber": "123-45-67890",
        "repPhoneNum": "02-000-0000", "repEmail": "t@example.com",
        "repAddr1": "서울시", "repAddr2": "테스트동",
        "eCommerceReportNumber": "2024-서울테스트-0001",
        "qualitySellerBadgeDto": None, "ratingCount": 10, "thumbUpRatio": 95.0,
    })}


class FakeMouse:
    def move(self, x, y):
        pass

    def wheel(self, x, y):
        pass


class FakeSearchPage:
    """정렬별 DOM 추출 결과·개별 API 응답을 스크립팅하는 fake page."""

    def __init__(self, sorter_rows=None, content_html=_OK_HTML, viids=None,
                 menu_has_link=False, home_html=_OK_HTML):
        self.mouse = FakeMouse()
        self.url = "about:blank"
        self._sorter_rows = list(sorter_rows or [])
        self._dom_call = 0
        self._content_html = content_html
        self._home_html = home_html
        self._viids = viids or []
        self.menu_has_link = menu_has_link
        self.goto_urls = []
        self.waited_url_patterns = []
        self.backoff_sleeps = []

    def goto(self, url, **kwargs):
        self.url = str(url)
        self.goto_urls.append(str(url))

    def wait_for_load_state(self, *a, **k):
        pass

    def wait_for_url(self, url, timeout=None):
        self.waited_url_patterns.append(str(url))

    def content(self):
        if self.url == COUPANG_HOME:
            return self._home_html
        return self._content_html

    def title(self):
        return "fake"

    def on(self, event, handler):
        pass

    def evaluate(self, script, *args):
        if "ProductUnit_productUnit" in script:
            if self._dom_call < len(self._sorter_rows):
                rows = self._sorter_rows[self._dom_call]
                self._dom_call += 1
                return rows
            return []
        if "'/np/categories/' + cid" in script:
            # 메뉴 클릭 진입 JS — menu_has_link 에 따라 성공/실패를 시뮬레이션.
            # 성공 시 실제 클릭처럼 페이지 URL 을 목표 PLP 로 전환한다.
            cid = args[0] if args else ""
            if self.menu_has_link:
                self.url = f"https://www.coupang.com/np/categories/{cid}"
                return True
            return False
        if "individualInfo" in script:
            return _individual_response(self._viids)
        if "getStoreReview" in script:
            return _review_response()
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


class FastSearchCrawler(SearchCrawler):
    _backoff_seconds = (0, 0, 0)


class ClickJsErrorPage(FakeSearchPage):
    """메뉴 클릭 JS 평가 자체가 실패하는 페이지 — goto 폴백 경로 검증용."""

    def evaluate(self, script, *args):
        if "'/np/categories/' + cid" in script:
            raise RuntimeError("click js boom")
        return super().evaluate(script, *args)


def _run(tmp_dir, page, sorters=("saleCountDesc", "salePriceAsc"), exclude_rocket=True,
         content_html=_OK_HTML, include_price_bands=False, category_id="", max_pages=3,
         keyword="뷰티", category_name="", subcategories=()):
    config = SearchRunConfig(
        output_dir=Path(tmp_dir),
        output_prefix="search_test",
        keyword=keyword,
        category_name=category_name,
        sorters=sorters,
        exclude_rocket=exclude_rocket,
        include_price_bands=include_price_bands,
        category_id=category_id,
        subcategories=subcategories,
        category_cooldown_min=0,
        category_cooldown_max=0,
        max_pages=max_pages,
        warmup_time=0,
        page_delay_min=0,
        page_delay_max=0,
        delay_min=0,
        delay_max=0,
    )
    browser = FakeBrowser(page)
    crawler = FastSearchCrawler(
        config=config,
        control=Control(),
        browser_factory=lambda: browser,
    )
    return crawler.run()


class SearchPipelineTest(unittest.TestCase):
    def test_success_with_rocket_exclusion_and_dedup(self):
        # 정렬 1: 일반 2 + 로켓 1 / 정렬 2: 중복 1 + 신규 1
        rows1 = [_row("11", title="A"), _row("22", title="B"), _row("33", rocket=True)]
        rows2 = [_row("11", title="A중복"), _row("44", title="C")]
        page = FakeSearchPage(sorter_rows=[rows1, rows2], viids=["11", "22", "44"])
        with tempfile.TemporaryDirectory() as tmp:
            summary = _run(tmp, page)

            self.assertIsNone(summary.error)
            self.assertEqual(summary.products_seen, 3)  # 로켓 1건 제외, 중복 1건 제거
            self.assertEqual(summary.unique_vendors, 3)
            self.assertEqual(summary.business_info_success, 3)
            self.assertEqual(len(summary.records), 3)
            # 정렬 파라미터가 URL 에 반영됐는지
            self.assertIn("q=%EB%B7%B0%ED%8B%B0", page.goto_urls[1])
            self.assertIn("sorter=saleCountDesc", page.goto_urls[1])
            self.assertIn("sorter=salePriceAsc", page.goto_urls[2])
            # 파일 저장 확인
            self.assertTrue(summary.json_path and Path(summary.json_path).exists())
            self.assertTrue(summary.csv_path and Path(summary.csv_path).exists())

    def test_rocket_included_when_configured(self):
        rows1 = [_row("11"), _row("33", rocket=True)]
        page = FakeSearchPage(sorter_rows=[rows1], viids=["11", "33"])
        with tempfile.TemporaryDirectory() as tmp:
            summary = _run(tmp, page, sorters=("saleCountDesc",), exclude_rocket=False)
            self.assertEqual(summary.products_seen, 2)

    def test_blocked_page_stops_immediately(self):
        page = FakeSearchPage(sorter_rows=[[_row("11")]], content_html=_BLOCKED_HTML)
        with tempfile.TemporaryDirectory() as tmp:
            summary = _run(tmp, page)
            dumps = list(Path(tmp).glob("blocked_*.html"))
            # 차단 응답 원본이 진단용으로 저장됐는지
            self.assertEqual(len(dumps), 1)
            self.assertIn("사용권한", dumps[0].read_text(encoding="utf-8"))
        self.assertEqual(summary.termination_reason, "blocked")
        self.assertIn("차단", summary.error)
        # 첫 정렬에서 중단 — 두 번째 정렬 시도 없음
        self.assertEqual(len(page.goto_urls), 2)  # 홈 웜업 + 검색 1회

    def test_empty_results_backoff_then_no_items(self):
        page = FakeSearchPage(sorter_rows=[[], [], [], []])
        with tempfile.TemporaryDirectory() as tmp:
            summary = _run(tmp, page, sorters=("saleCountDesc",))
        self.assertEqual(summary.termination_reason, "no_items")
        # 초기 1회 + 백오프 재시도 3회
        self.assertEqual(sum(1 for u in page.goto_urls if "/np/search" in u), 4)


    def test_full_three_layer_pipeline(self):
        """층1 정렬 + 층2 가격 밴드 + 층3 PLP 순회 통합 파이프라인."""
        # 정렬 1종 + 밴드 2종 + PLP 2페이지 = DOM 추출 5회
        rows_seq = [
            [_row("11"), _row("22")],            # 층1 정렬
            [_row("33")],                          # 층2 밴드 1
            [_row("44"), _row("11")],              # 층2 밴드 2 (중복 포함)
            [_row("55"), _row("66", rocket=True)],  # 층3 PLP p1
            [_row("77")],                          # 층3 PLP p2
        ]
        bands_html = (
            "<html><body>" + "검색결과 콘텐츠 " * 400 +
            '{"id":"0-6000","text":"6천원 이하","minPrice":0,"maxPrice":6000}'
            '{"id":"6000-12000","text":"6천~1만2","minPrice":6000,"maxPrice":12000}'
            "</body></html>"
        )
        page = FakeSearchPage(sorter_rows=rows_seq, content_html=bands_html,
                              viids=["11", "22", "33", "44", "55", "77"])
        with tempfile.TemporaryDirectory() as tmp:
            summary = _run(tmp, page, sorters=("saleCountDesc",),
                           include_price_bands=True, category_id="176522", max_pages=2)
            self.assertIsNone(summary.error)
            # 로켓 1건 제외: 7건 중 6건
            self.assertEqual(summary.products_seen, 6)
            self.assertEqual(len(summary.records), 6)
            # PLP URL 확인
            plp_urls = [u for u in page.goto_urls if "/np/categories/176522" in u]
            self.assertEqual(len(plp_urls), 2)
            self.assertIn("page=1", plp_urls[0])
            self.assertIn("page=2", plp_urls[1])
            # 밴드 URL 확인
            band_urls = [u for u in page.goto_urls if "isPriceRange=true" in u]
            self.assertEqual(len(band_urls), 2)

    def test_plp_stops_on_empty_pages(self):
        """PLP 빈 페이지 연속 시 상한 도달로 정상 종료."""
        rows_seq = [
            [_row("11")],  # 층1 정렬
            [],            # PLP p1 빈 결과
            [],            # PLP p2 빈 결과 → 연속 2회로 종료
        ]
        page = FakeSearchPage(sorter_rows=rows_seq, viids=["11"])
        with tempfile.TemporaryDirectory() as tmp:
            summary = _run(tmp, page, sorters=("saleCountDesc",),
                           category_id="176522", max_pages=10)
            self.assertIsNone(summary.error)
            plp_urls = [u for u in page.goto_urls if "/np/categories/176522" in u]
            self.assertEqual(len(plp_urls), 2)  # 10페이지 안 가고 빈 페이지 2회에 종료

    def test_category_only_mode_skips_srp_layers(self):
        """카테고리 전용 모드: 층1/층2 건너뛰고 PLP만 순회 (컨셉 전환 기본 경로)."""
        rows_seq = [
            [_row("11"), _row("22", rocket=True)],  # PLP p1
            [_row("33"), _row("11")],                # PLP p2 (중복 포함)
        ]
        page = FakeSearchPage(sorter_rows=rows_seq, viids=["11", "33"])
        with tempfile.TemporaryDirectory() as tmp:
            summary = _run(tmp, page, keyword="", category_id="221934",
                           category_name="출산/유아동", max_pages=5,
                           subcategories=(("310632", "비타민/미네랄"),
                                          ("310655", "건강식품")))
            self.assertIsNone(summary.error)
            # SRP/밴드 URL 없음 — 카테고리 PLP 만 방문.
            # 2 페이지 수집 + 상한 확인용 빈 페이지 2회(연속 시 종료) = 4회 로드
            self.assertFalse(any("/np/search" in u for u in page.goto_urls))
            plp_urls = [u for u in page.goto_urls if "/np/categories/221934" in u]
            self.assertEqual(len(plp_urls), 4)
            self.assertIn("page=1", plp_urls[0])
            self.assertIn("page=2", plp_urls[1])
            # 로켓 1건 제외 + 중복 1건 제거 = 2건
            self.assertEqual(summary.products_seen, 2)
            self.assertEqual(summary.business_info_success, 2)
            self.assertEqual(summary.termination_reason, "success")

    def test_first_category_page_enters_via_menu_click(self):
        """첫 카테고리 page 1 은 홈 메뉴 클릭으로 진입 (딥링크 goto 회피)."""
        rows_seq = [
            [_row("11"), _row("22")],   # PLP p1 — 클릭 진입 (goto 없음)
            [_row("33"), _row("11")],   # PLP p2 (중복 포함)
            [], [],                     # 빈 페이지 2회 → 종료
        ]
        page = FakeSearchPage(sorter_rows=rows_seq, viids=["11", "22", "33"],
                              menu_has_link=True)
        with tempfile.TemporaryDirectory() as tmp:
            summary = _run(tmp, page, keyword="", category_id="221934",
                           category_name="출산/유아동", max_pages=5)
            self.assertIsNone(summary.error)
            # page=1 직접 goto 없음 — 클릭 진입, p2 부터 goto
            plp_urls = [u for u in page.goto_urls if "/np/categories/221934" in u]
            self.assertEqual(len(plp_urls), 3)  # p2 + 빈 2회
            self.assertFalse(any("page=1" in u for u in plp_urls))
            self.assertTrue(any("page=2" in u for u in plp_urls))
            # 클릭 후 목표 URL 대기 1회
            self.assertEqual(len(page.waited_url_patterns), 1)
            self.assertIn("/np/categories/221934", page.waited_url_patterns[0])
            self.assertEqual(summary.products_seen, 3)  # 중복 1건 제거
            self.assertEqual(summary.termination_reason, "success")

    def test_menu_click_failure_falls_back_to_goto(self):
        """클릭 JS 평가가 예외를 던져도 goto 경로로 폴백 — 수집은 정상 완료."""
        rows_seq = [
            [_row("11")],   # PLP p1 — goto 진입
            [], [],
        ]
        page = ClickJsErrorPage(sorter_rows=rows_seq, viids=["11"])
        with tempfile.TemporaryDirectory() as tmp:
            summary = _run(tmp, page, keyword="", category_id="221934",
                           category_name="출산/유아동", max_pages=5)
            self.assertIsNone(summary.error)
            plp_urls = [u for u in page.goto_urls if "/np/categories/221934" in u]
            self.assertTrue(any("page=1" in u for u in plp_urls))
            self.assertEqual(summary.termination_reason, "success")

    def test_blocked_akamai_page_is_classified(self):
        """Akamai 거부 응답은 분류 메시지와 스냅샷으로 진단된다."""
        page = FakeSearchPage(sorter_rows=[[_row("11")]], content_html=_AKAMAI_HTML)
        with tempfile.TemporaryDirectory() as tmp:
            summary = _run(tmp, page)
            dumps = list(Path(tmp).glob("blocked_*.html"))
            self.assertEqual(len(dumps), 1)
            self.assertIn("Access Denied", dumps[0].read_text(encoding="utf-8"))
        self.assertEqual(summary.termination_reason, "blocked")
        self.assertIn("Akamai", summary.error)

    def test_blocked_home_stops_immediately(self):
        """홈 웜업 단계 차단 — IP 평판 차단 신호로 목록 로드 전에 즉시 중단."""
        page = FakeSearchPage(home_html=_AKAMAI_HTML)
        with tempfile.TemporaryDirectory() as tmp:
            summary = _run(tmp, page)
            dumps = list(Path(tmp).glob("blocked_home_warmup_*.html"))
            self.assertEqual(len(dumps), 1)
        self.assertEqual(summary.termination_reason, "blocked")
        self.assertIn("홈 웜업", summary.error)
        self.assertEqual(len(page.goto_urls), 1)  # 홈만 로드 — 목록 시도 없음

    def test_blocked_run_records_state_with_reference(self):
        """차단 종료 시 output_dir 에 차단 상태 기록 — Akamai Reference 포함."""
        page = FakeSearchPage(sorter_rows=[[_row("11")]], content_html=_AKAMAI_HTML)
        with tempfile.TemporaryDirectory() as tmp:
            summary = _run(tmp, page)
            self.assertEqual(summary.termination_reason, "blocked")
            state = blockguard.read_block_state(Path(tmp))
            self.assertIsNotNone(state)
            self.assertIn("Reference #", str(state.get("reference", "")))
            self.assertGreater(blockguard.cooldown_remaining_seconds(Path(tmp)), 0.0)

    def test_block_cooldown_gate_blocks_rerun(self):
        """쿨다운 중 재실행은 브라우저를 건드리지 않고 거부된다."""
        page = FakeSearchPage(sorter_rows=[[_row("11")]], content_html=_AKAMAI_HTML)
        with tempfile.TemporaryDirectory() as tmp:
            first = _run(tmp, page)
            self.assertEqual(first.termination_reason, "blocked")
            gotos_after_first = len(page.goto_urls)
            second = _run(tmp, page)
            self.assertEqual(second.termination_reason, "block_cooldown")
            self.assertIn("쿨다운", second.error)
            self.assertEqual(len(page.goto_urls), gotos_after_first)


class SearchParserTest(unittest.TestCase):
    def test_parse_href(self):
        legacy, iid, viid = parse_href(
            "/vp/products/8730441955?itemId=25505650270&vendorItemId=92497699471")
        self.assertEqual((legacy, iid, viid), ("8730441955", "25505650270", "92497699471"))

    def test_parse_price(self):
        self.assertEqual(parse_price("9,920원"), 9920)
        self.assertEqual(parse_price("129,000"), 129000)
        self.assertEqual(parse_price(""), 0)

    def test_is_blocked_keywords(self):
        big = "x" * 3000
        self.assertEqual(is_blocked(big + "사용권한이 없습니다")[0], True)
        self.assertEqual(is_blocked(big + "Access Denied")[0], True)
        self.assertEqual(is_blocked(big + "정상 페이지")[0], False)
        self.assertEqual(is_blocked("짧은응답")[0], True)  # 소프트 블록

    def test_parse_price_bands(self):
        html = (
            'x' * 2000 +
            '{"id":"all","text":"가격 전체","minPrice":null,"maxPrice":null}'
            '{"id":"0-6000","text":"6천원 이하","minPrice":0,"maxPrice":6000}'
            '{"id":"6000-12000","text":"6천~1만2","minPrice":6000,"maxPrice":12000}'
            '{"id":"24000-2147483647","text":"2만4 이상","minPrice":24000,"maxPrice":2147483647}'
        )
        bands = parse_price_bands(html)
        self.assertEqual(bands, [(0, 6000), (6000, 12000), (24000, 2147483647)])
        self.assertEqual(parse_price_bands("밴드 없음"), [])

    def test_parse_price_bands_escaped_quotes(self):
        html = (
            '[{\\"id\\":\\"all\\",\\"text\\":\\"가격 전체\\",'
            '\\"minPrice\\":null,\\"maxPrice\\":null},'
            '{\\"id\\":\\"0-6000\\",\\"text\\":\\"6천원 이하\\",'
            '\\"minPrice\\":0,\\"maxPrice\\":6000},'
            '{\\"id\\":\\"6000-12000\\",\\"text\\":\\"6천원~1만 2천원\\",'
            '\\"minPrice\\":6000,\\"maxPrice\\":12000}]'
        )
        bands = parse_price_bands(html)
        self.assertEqual(bands, [(0, 6000), (6000, 12000)])

    def test_parse_extracted_dedup(self):
        rows = [_row("11"), _row("11"), _row("22", rocket=True)]
        items = parse_extracted(rows)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].price, 9920)
        self.assertTrue(items[1].rocket)
        self.assertIn("vendorItemId=11", items[0].url)


class SearchSubcategoryTest(unittest.TestCase):
    def test_subcategory_queue_traversal(self):
        """부모 + 하위 2개 큐 순회 — 순서·중복 제거·전체 합산."""
        rows_seq = [
            [_row("11")],              # 부모 p1
            [], [],                    # 부모 빈 페이지 2회 → 부모 종료
            [_row("22"), _row("11")],  # 하위1 p1 (중복 포함)
            [], [],                    # 하위1 종료
            [_row("33")],              # 하위2 p1
            [], [],                    # 하위2 종료
        ]
        page = FakeSearchPage(sorter_rows=rows_seq, viids=["11", "22", "33"])
        with tempfile.TemporaryDirectory() as tmp:
            summary = _run(tmp, page, keyword="", category_id="305798",
                           category_name="헬스/건강식품", max_pages=5,
                           subcategories=(("310632", "비타민/미네랄"),
                                          ("310655", "건강식품")))
            self.assertIsNone(summary.error)
            cat_urls = [u for u in page.goto_urls if "/np/categories/" in u]
            self.assertEqual(len([u for u in cat_urls if "305798" in u]), 3)
            self.assertTrue(any("310632" in u for u in cat_urls))
            self.assertTrue(any("310655" in u for u in cat_urls))
            self.assertEqual(summary.products_seen, 3)  # 중복 1건 제거
            self.assertEqual(summary.business_info_success, 3)
            self.assertEqual(summary.termination_reason, "success")

    def test_subcategory_id_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                SearchRunConfig(output_dir=Path(tmp), category_id="305798",
                                subcategories=(("abc", "잘못"),))


class SearchConfigTest(unittest.TestCase):
    def test_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 키워드도 카테고리도 없으면 불가
            with self.assertRaises(ValueError):
                SearchRunConfig(output_dir=Path(tmp), keyword="")
            # 카테고리 전용 모드는 허용 (컨셉 전환 기본 경로)
            cfg = SearchRunConfig(output_dir=Path(tmp), keyword="",
                                  category_id="221934", category_name="출산/유아동")
            self.assertTrue(cfg.category_only)
            self.assertFalse(SearchRunConfig(output_dir=Path(tmp),
                                             keyword="뷰티").category_only)
            with self.assertRaises(ValueError):
                SearchRunConfig(output_dir=Path(tmp), keyword="뷰티", sorters=("bogus",))
            with self.assertRaises(ValueError):
                SearchRunConfig(output_dir=Path(tmp), keyword="뷰티", sorters=())
            with self.assertRaises(ValueError):
                SearchRunConfig(output_dir=Path(tmp), keyword="뷰티", category_id="abc")
            with self.assertRaises(ValueError):
                SearchRunConfig(output_dir=Path(tmp), keyword="뷰티", max_pages=0)
            with self.assertRaises(ValueError):
                SearchRunConfig(output_dir=Path(tmp), keyword="뷰티", max_pages=51)


if __name__ == "__main__":
    unittest.main()
