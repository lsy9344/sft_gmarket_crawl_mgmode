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
from app.core.coupang.search_crawler import SearchCrawler, SearchRunConfig
from app.core.coupang.search_parser import (
    is_blocked,
    parse_extracted,
    parse_href,
    parse_price_bands,
    parse_price,
)

_OK_HTML = "<html><body>" + ("검색결과 콘텐츠 " * 400) + "</body></html>"
_BLOCKED_HTML = "<html><body>요청하신 페이지의 사용권한이 없습니다.</body></html>" + "x" * 2000


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

    def __init__(self, sorter_rows=None, content_html=_OK_HTML, viids=None):
        self.mouse = FakeMouse()
        self.url = "about:blank"
        self._sorter_rows = list(sorter_rows or [])
        self._dom_call = 0
        self._content_html = content_html
        self._viids = viids or []
        self.goto_urls = []
        self.backoff_sleeps = []

    def goto(self, url, **kwargs):
        self.url = str(url)
        self.goto_urls.append(str(url))

    def wait_for_load_state(self, *a, **k):
        pass

    def content(self):
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


def _run(tmp_dir, page, sorters=("saleCountDesc", "salePriceAsc"), exclude_rocket=True,
         content_html=_OK_HTML, include_price_bands=False, category_id="", max_pages=3):
    config = SearchRunConfig(
        output_dir=Path(tmp_dir),
        output_prefix="search_test",
        keyword="뷰티",
        sorters=sorters,
        exclude_rocket=exclude_rocket,
        include_price_bands=include_price_bands,
        category_id=category_id,
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


class SearchConfigTest(unittest.TestCase):
    def test_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                SearchRunConfig(output_dir=Path(tmp), keyword="")
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
