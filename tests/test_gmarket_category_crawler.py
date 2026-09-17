"""Gmarket 카테고리 리스팅 엔진(gmarket_category_crawler) 테스트 — 네트워크 없음.

검증 항목:
- 리스팅 URL 생성 (1페이지 / 2페이지+ — 실측 파라미터 `&k=0&p=N&keep-ssid=y`)
- goodscode 목록 → PrescanResult 상태 판정 (수집 가능/완료/빈/차단)
- RunConfig 검증
- _fetch_page / _collect_one (fake Unlocker 응답: 정상/빈 페이지/오류·차단)
- 토큰 누락 시 collect() 즉시 실패, 요청 페이로드(zone/url/country) 구성
"""

from __future__ import annotations

import unittest
from unittest import mock

from app.core import config
from app.core.base import Control
from app.core.gmarket_category_crawler import (
    CategoryTarget,
    GmarketCategoryLister,
    GmarketCategoryRunConfig,
    build_result_from_codes,
    category_list_url,
)
from app.models.records import (
    SOURCE_CATEGORY,
    STATUS_BLOCKED,
    STATUS_COLLECTABLE,
    STATUS_COMPLETED,
    STATUS_EMPTY,
)


class UrlTest(unittest.TestCase):
    def test_page1_no_page_param(self):
        self.assertEqual(
            category_list_url("200002669"),
            "https://www.gmarket.co.kr/n/list?category=200002669",
        )
        self.assertEqual(
            category_list_url("200002669", 1),
            "https://www.gmarket.co.kr/n/list?category=200002669",
        )

    def test_page2_appends_unlocker_pagination(self):
        # 실측(2026-09-08): &page=N 은 무시되고 &k=0&p=N&keep-ssid=y 만 유효
        self.assertEqual(
            category_list_url("200002669", 2),
            "https://www.gmarket.co.kr/n/list?category=200002669&k=0&p=2&keep-ssid=y",
        )


class BuildResultTest(unittest.TestCase):
    def test_collectable(self):
        r = build_result_from_codes("여성의류 > 티셔츠", ["111", "222"], set())
        self.assertEqual(r.status, STATUS_COLLECTABLE)
        self.assertEqual(r.source, SOURCE_CATEGORY)
        self.assertEqual(r.new_codes, 2)
        self.assertEqual(r.total_codes, 2)

    def test_partially_collected(self):
        r = build_result_from_codes("A", ["111", "222"], {"111"})
        self.assertEqual(r.status, STATUS_COLLECTABLE)
        self.assertEqual(r.already_collected, 1)
        self.assertEqual(r.new_codes, 1)

    def test_completed_and_empty(self):
        r = build_result_from_codes("A", ["111"], {"111"})
        self.assertEqual(r.status, STATUS_COMPLETED)
        r2 = build_result_from_codes("A", [], set())
        self.assertEqual(r2.status, STATUS_EMPTY)

    def test_blocked_when_fetch_failed(self):
        r = build_result_from_codes("A", [], set(), blocked=True)
        self.assertEqual(r.status, STATUS_BLOCKED)

    def test_blocked_with_partial_codes_stays_blocked(self):
        # 재시도 소진 후 부분 목록만 확보된 경우 — '수집 가능'이 아니라
        # '차단/오류'로 표기돼야 나머지 페이지가 영구 누락되지 않는다.
        r = build_result_from_codes("A", ["111"], set(), blocked=True)
        self.assertEqual(r.status, STATUS_BLOCKED)
        self.assertEqual(r.total_codes, 1)

    def test_blocked_wins_even_when_all_collected(self):
        # fetch 실패는 목록이 불완전하다는 신호 — 전량 기확보여도 재시도를
        # 유도하도록 '차단/오류'로 표기한다(정상 재조사 시 '완료'로 갱신됨).
        r = build_result_from_codes("A", ["111"], {"111"}, blocked=True)
        self.assertEqual(r.status, STATUS_BLOCKED)


class RunConfigTest(unittest.TestCase):
    def test_valid(self):
        from pathlib import Path

        cfg = GmarketCategoryRunConfig(
            output_dir=Path("/tmp/out"),
            output_prefix="gmarket_category_test",
            targets=(CategoryTarget("200002669", "여성의류 > 티셔츠"),),
        )
        self.assertEqual(cfg.targets[0].code, "200002669")

    def test_invalid(self):
        from pathlib import Path

        base = {"output_dir": Path("/tmp/out"), "output_prefix": "x"}
        with self.assertRaises(ValueError):
            GmarketCategoryRunConfig(**base, targets=())
        with self.assertRaises(ValueError):
            GmarketCategoryRunConfig(**base, targets=(CategoryTarget("abc", "라벨"),))
        with self.assertRaises(ValueError):
            GmarketCategoryRunConfig(
                **base,
                targets=(CategoryTarget("200002669", "라벨"),),
                page_delay_max=1.0, page_delay_min=5.0,
            )
        with self.assertRaises(ValueError):
            GmarketCategoryRunConfig(
                **base, targets=(CategoryTarget("200002669", "라벨"),), max_items=0
            )


# ── fake Unlocker (requests.Session.post) 페이지 응답 ────────────────────────

class _FakeResponse:
    def __init__(self, status: int, html: str = "", headers: dict | None = None) -> None:
        self.status_code = status
        self.text = html
        self.headers = headers or {}


class _FakeUnlockerSession:
    """post/get 제공 + 컨텍스트 매니저 지원 fake requests.Session."""

    def __init__(self, page_map) -> None:
        self.page_map = page_map  # 대상 URL → _FakeResponse | list[_FakeResponse]
        self.calls: list[dict] = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        target_url = (json or {}).get("url", "")
        resp = self.page_map.get(target_url)
        if isinstance(resp, list) and resp:
            return resp.pop(0)
        if isinstance(resp, _FakeResponse):
            return resp
        return _FakeResponse(200, "")

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def _listing_html(codes: list[str]) -> str:
    links = "".join(
        f'<a href="https://item.gmarket.co.kr/Item?goodscode={c}">상품</a>' for c in codes
    )
    return f"<html><body>{links}</body></html>"


class ListerTest(unittest.TestCase):
    def setUp(self):
        self.control = Control()
        self.logs: list[str] = []
        self.progress: list[tuple] = []

    def _lister(self, max_pages=3, delay=0.0, token="tok"):
        lister = GmarketCategoryLister(
            control=self.control,
            on_log=self.logs.append,
            on_progress=lambda *a: self.progress.append(a),
            max_pages=max_pages,
            page_delay_min=delay,
            page_delay_max=delay,
        )
        lister._token = token
        return lister

    def test_collect_one_stops_on_empty_pages(self):
        # 1페이지: 코드 2개, 2~3페이지: 빈 페이지 → 연속 2회(허용치)면 종료.
        # 빈 페이지는 이제 1회 재요청을 거친다(§9 회복 재시도) — 대기만 0으로.
        url1 = category_list_url("200002669", None)
        session = _FakeUnlockerSession({
            url1: _FakeResponse(200, _listing_html(["111111111", "222222222"])),
        })
        lister = self._lister(max_pages=3)
        target = CategoryTarget("200002669", "티셔츠")
        with mock.patch.object(config, "UNLOCKER_EMPTY_RETRY_WAIT", 0):
            out = lister._collect_one(session, target)
        self.assertEqual(out.codes, ["111111111", "222222222"])
        self.assertFalse(out.blocked)

    def test_collect_one_dedupes_across_pages(self):
        url1 = category_list_url("200002669", None)
        url2 = category_list_url("200002669", 2)
        session = _FakeUnlockerSession({
            url1: _FakeResponse(200, _listing_html(["111111111"])),
            url2: _FakeResponse(200, _listing_html(["111111111", "222222222"])),
        })
        lister = self._lister(max_pages=2)
        out = lister._collect_one(session, CategoryTarget("200002669", "티셔츠"))
        self.assertEqual(out.codes, ["111111111", "222222222"])

    def test_fetch_page_payload_and_header(self):
        url = category_list_url("200002669", None)
        session = _FakeUnlockerSession({
            url: _FakeResponse(200, _listing_html(["111111111"])),
        })
        lister = self._lister(token="tok123")
        codes, blocked = lister._fetch_page(
            session, url, CategoryTarget("200002669", "티")
        )
        self.assertEqual(codes, ["111111111"])
        self.assertFalse(blocked)
        call = session.calls[0]
        self.assertEqual(call["url"], config.BRIGHTDATA_API_URL)
        self.assertEqual(call["headers"]["Authorization"], "Bearer tok123")
        self.assertEqual(call["json"]["zone"], config.BRIGHTDATA_ZONE)
        self.assertEqual(call["json"]["url"], url)
        self.assertEqual(call["json"]["country"], config.BRIGHTDATA_COUNTRY)
        self.assertEqual(call["json"]["format"], "raw")

    def test_fetch_page_error_status_blocked(self):
        url = category_list_url("200002669", None)
        # 모든 시도가 500 → 재시도 소진 후 blocked
        session = _FakeUnlockerSession({
            url: [_FakeResponse(500)] * (config.LISTING_MAX_RETRIES + 1),
        })
        lister = self._lister()
        with mock.patch.object(config, "UNLOCKER_RETRY_WAIT", 0):
            codes, blocked = lister._fetch_page(
                session, url, CategoryTarget("200002669", "티")
            )
        self.assertEqual(codes, [])
        self.assertTrue(blocked)
        self.assertEqual(len(session.calls), config.LISTING_MAX_RETRIES + 1)

    def test_bot_html_blocked(self):
        url = category_list_url("200002669", None)
        bot_html = "<html>자동입력 방지 문자를 입력하세요</html>"
        session = _FakeUnlockerSession({
            url: [_FakeResponse(200, bot_html)] * (config.LISTING_MAX_RETRIES + 1),
        })
        lister = self._lister()
        with mock.patch.object(config, "UNLOCKER_RETRY_WAIT", 0):
            _codes, blocked = lister._fetch_page(
                session, url, CategoryTarget("200002669", "티")
            )
        self.assertTrue(blocked)

    def test_legacy_large_variant_treated_as_empty(self):
        # 실측(2026-09-08): 대분류(L-code)는 CategoryLarge 레거시 변형 —
        # 광고 goodscode 링크만 담겨 상품으로 오인되면 안 된다.
        url = category_list_url("100000003", None)
        legacy_html = (
            '<html><head><script src="/listview/js/CategoryLargeFuction.js">'
            "</script></head><body>"
            '<a href="http://item.gmarket.co.kr/item?goodscode=7777777777">광고</a>'
            "</body></html>"
        )
        session = _FakeUnlockerSession({url: _FakeResponse(200, legacy_html)})
        lister = self._lister()
        codes, blocked = lister._fetch_page(
            session, url, CategoryTarget("100000003", "여성의류")
        )
        self.assertEqual(codes, [])
        self.assertFalse(blocked)

    def test_fetch_page_retries_empty_once_and_recovers(self):
        # 빈 껍데기(200 + 코드 0)는 1회 재요청으로 회복을 시도한다 —
        # 대량 실측(ACCESS_ROUTES_RESEARCH §9)에서 재시도로 81% 회복됐다.
        url = category_list_url("200002669", None)
        session = _FakeUnlockerSession({
            url: [_FakeResponse(200, ""), _FakeResponse(200, _listing_html(["111111111"]))],
        })
        lister = self._lister()
        with mock.patch.object(config, "UNLOCKER_EMPTY_RETRY_WAIT", 0):
            codes, blocked = lister._fetch_page(
                session, url, CategoryTarget("200002669", "티")
            )
        self.assertEqual(codes, ["111111111"])
        self.assertFalse(blocked)
        self.assertEqual(len(session.calls), 2)

    def test_fetch_page_empty_stays_empty_after_single_retry(self):
        # 재요청 후에도 비면 빈 페이지로 반환 — 연속 종료 판정은 호출자 몫.
        # 무한 재시도가 아니라 정확히 1회만 재요청한다.
        url = category_list_url("200002669", None)
        session = _FakeUnlockerSession({
            url: [_FakeResponse(200, ""), _FakeResponse(200, "")],
        })
        lister = self._lister()
        with mock.patch.object(config, "UNLOCKER_EMPTY_RETRY_WAIT", 0):
            codes, blocked = lister._fetch_page(
                session, url, CategoryTarget("200002669", "티")
            )
        self.assertEqual(codes, [])
        self.assertFalse(blocked)
        self.assertEqual(len(session.calls), 2)

    def test_fetch_page_xbrd_error_header_is_blocked(self):
        # 200 + x-brd-error 헤더 = Unlocker가 포장한 실패
        # (BRIGHTDATA_AKAMAI_REVIEW §2) — 빈 페이지가 아니라 오류로 재시도한다.
        url = category_list_url("200002669", None)
        session = _FakeUnlockerSession({
            url: [_FakeResponse(200, "", headers={
                "x-brd-error": "captcha or protection page found",
            })] * (config.LISTING_MAX_RETRIES + 1),
        })
        lister = self._lister()
        with mock.patch.object(config, "UNLOCKER_RETRY_WAIT", 0):
            codes, blocked = lister._fetch_page(
                session, url, CategoryTarget("200002669", "티")
            )
        self.assertEqual(codes, [])
        self.assertTrue(blocked)
        self.assertEqual(len(session.calls), config.LISTING_MAX_RETRIES + 1)


class CollectTokenTest(unittest.TestCase):
    def test_collect_raises_without_token(self):
        from app.core import brightdata

        lister = GmarketCategoryLister(on_log=lambda m: None)
        with mock.patch.object(brightdata, "resolve_api_token", lambda s=None: ""), \
                self.assertRaises(RuntimeError):
            lister.collect([CategoryTarget("200002669", "티셔츠")])

    def test_collect_returns_empty_without_targets(self):
        lister = GmarketCategoryLister(on_log=lambda m: None)
        self.assertEqual(lister.collect([]), [])

    def test_collect_uses_settings_zone_and_token(self):
        # 설정 탭에서 입력한 계정 키·존이 Unlocker 요청에 그대로 쓰인다 —
        # 사용량 차감 대상 계정이 입력 키로 결정되는 것의 엔진 단면.
        from app.core import brightdata

        lister = GmarketCategoryLister(
            on_log=lambda m: None, max_pages=1, page_delay_min=0, page_delay_max=0,
        )
        url = category_list_url("200002669", None)
        session = _FakeUnlockerSession({
            url: _FakeResponse(200, _listing_html(["111111111"])),
        })
        settings = brightdata.BrightDataSettings(
            api_token="ui-tok", unlocker_zone="my_zone", country="us")
        with mock.patch.object(brightdata, "load_settings", lambda: settings), \
             mock.patch.object(brightdata, "resolve_api_token", lambda s: "ui-tok"), \
             mock.patch("app.core.gmarket_category_crawler.requests.Session",
                        return_value=session):
            outcomes = lister.collect([CategoryTarget("200002669", "티셔츠")])
        self.assertEqual(outcomes[0].codes, ["111111111"])
        call = session.calls[0]
        self.assertEqual(call["json"]["zone"], "my_zone")
        self.assertEqual(call["json"]["country"], "us")
        self.assertEqual(call["headers"]["Authorization"], "Bearer ui-tok")


if __name__ == "__main__":
    unittest.main()
