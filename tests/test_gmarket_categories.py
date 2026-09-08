"""Gmarket 카테고리 트리 모듈(gmarket_categories) 테스트 — 네트워크 없이 합성 픽스처.

검증 항목:
- listview HTML 의 대분류 네비 파싱 (순서/중복/이름 정리)
- listview HTML 의 중/소분류 서브트리 재구성 (문서 순서 기반 계층 복원)
- 노드 유틸 (count/find/flatten)
- 캐시 저장·로드·만료·손상 → seed 폴백
- GmarketCategoryTreeFetcher (fake HTTP 세션: 정상/부분 실패/네비 없음)
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from app.core.gmarket_categories import (
    DEFAULT_BOOTSTRAP_CODE,
    GmarketCategoryFetchError,
    GmarketCategoryNode,
    GmarketCategoryTreeCache,
    GmarketCategoryTreeFetcher,
    count_nodes,
    find_node,
    flatten_descendants,
    parse_nav,
    parse_subtree,
)

# ── 합성 listview HTML (실측 구조 모사, euc-kr 로 인코딩해 제공) ─────────────

_NAV_HTML = """
<div id="gnb">
  <a href="http://category.gmarket.co.kr/listview/LList.aspx?gdlc_cd=100000103">브랜드 여성의류</a>
  <a href="http://category.gmarket.co.kr/listview/LList.aspx?gdlc_cd=100000003">여성의류</a>
  <a href="http://category.gmarket.co.kr/listview/LList.aspx?gdlc_cd=100000005">화장품/향수</a>
  <!-- 중복 링크는 1회만 -->
  <a href="http://category.gmarket.co.kr/listview/LList.aspx?gdlc_cd=100000003">여성의류</a>
  <a href="http://category.gmarket.co.kr/listview/LList.aspx?gdlc_cd=100000046">남성의류</a>
</div>
"""

_SUBTREE_HTML = """
<div class="tree">
  <a href="http://www.gmarket.co.kr/n/list?category=200002669"><span>티셔츠</span></a>
  <a href="http://www.gmarket.co.kr/n/list?category=300026660">무지 티셔츠</a>
  <a href="http://www.gmarket.co.kr/n/list?category=300026664">카라/PK티셔츠</a>
  <a href="http://www.gmarket.co.kr/n/list?categoryCode=300026666">민소매/나시 티셔츠</a>
  <a href="http://www.gmarket.co.kr/n/list?category=200002670">블라우스/셔츠</a>
  <a href="http://www.gmarket.co.kr/n/list?category=300026668">쉬폰 블라우스</a>
  <!-- 대분류(1로 시작) / 잘못된 깊이 / 빈 이름은 무시 -->
  <a href="http://www.gmarket.co.kr/n/list?category=100000103">전체보기</a>
  <a href="http://www.gmarket.co.kr/n/list?category=999999999"></a>
  <!-- 다른 페이지(마케팅)의 category 파라미터는 제외 -->
  <a href="http://rpp.gmarket.co.kr/?category=200002670">배너</a>
</div>
"""


def _enc(html: str) -> bytes:
    return html.encode("euc-kr")


class ParseNavTest(unittest.TestCase):
    def test_nav_order_and_dedupe(self):
        roots = parse_nav(_NAV_HTML)
        self.assertEqual([n.name for n in roots],
                         ["브랜드 여성의류", "여성의류", "화장품/향수", "남성의류"])
        self.assertEqual(roots[0].code, "100000103")
        self.assertEqual(roots[1].level, "L")
        self.assertTrue(all(len(n.code) == 9 for n in roots))

    def test_nav_empty_html(self):
        self.assertEqual(parse_nav(""), [])
        self.assertEqual(parse_nav("<html>없음</html>"), [])


class ParseSubtreeTest(unittest.TestCase):
    def test_hierarchy_reconstruction(self):
        roots = parse_subtree(_SUBTREE_HTML)
        self.assertEqual(len(roots), 2)
        tshirts, blouses = roots
        self.assertEqual(tshirts.name, "티셔츠")
        self.assertEqual(tshirts.code, "200002669")
        self.assertEqual(tshirts.level, "M")
        self.assertEqual([c.name for c in tshirts.children],
                         ["무지 티셔츠", "카라/PK티셔츠", "민소매/나시 티셔츠"])
        self.assertTrue(all(c.level == "S" for c in tshirts.children))
        self.assertEqual(blouses.name, "블라우스/셔츠")
        self.assertEqual([c.name for c in blouses.children], ["쉬폰 블라우스"])
        # 첫 번째 소분류가 두 번째 중분류로 잘못 붙지 않았는지
        self.assertNotIn("무지 티셔츠", [c.name for c in blouses.children])

    def test_empty(self):
        self.assertEqual(parse_subtree(""), [])
        self.assertEqual(parse_subtree("<a href='http://www.gmarket.co.kr/n/list?category=200002669'>x</a>"), [])


class NodeUtilTest(unittest.TestCase):
    def _roots(self):
        return [
            GmarketCategoryNode("100000003", "여성의류", "L", children=[
                GmarketCategoryNode("200000498", "티셔츠", "M", children=[
                    GmarketCategoryNode("300026660", "무지 티셔츠", "S"),
                ]),
            ]),
            GmarketCategoryNode("100000005", "화장품", "L"),
        ]

    def test_count_and_find(self):
        roots = self._roots()
        self.assertEqual(count_nodes(roots), 4)
        self.assertEqual(find_node(roots, "200000498").name, "티셔츠")
        self.assertIsNone(find_node(roots, "999999999"))

    def test_flatten(self):
        roots = self._roots()
        flat = flatten_descendants(roots[0])
        self.assertEqual([n.code for n in flat], ["200000498", "300026660"])


class CacheTest(unittest.TestCase):
    def _roots(self):
        return [GmarketCategoryNode("100000003", "여성의류", "L", children=[
            GmarketCategoryNode("200000498", "티셔츠", "M"),
        ])]

    def _write_seed(self, path: Path) -> None:
        payload = {
            "fetched_at": "2020-01-01 00:00:00",
            "fetched_ts": 1577808000.0,
            "node_count": 1,
            "roots": [n.to_dict() for n in self._roots()],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            cache = GmarketCategoryTreeCache(Path(td) / "cache.json")
            cache.save(self._roots())
            loaded = cache.load()
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded[0].name, "여성의류")
            self.assertEqual(loaded[0].children[0].code, "200000498")

    def test_expired_falls_back_to_seed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cache.json"
            seed = Path(td) / "seed.json"
            self._write_seed(seed)
            payload = {
                "fetched_ts": time.time() - 1000 * 3600,  # 만료
                "roots": [n.to_dict() for n in self._roots()],
            }
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            cache = GmarketCategoryTreeCache(path, ttl_hours=1, seed_path=seed)
            loaded = cache.load()
            self.assertIsNotNone(loaded)  # seed 폴백

    def test_corrupt_no_seed_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cache.json"
            path.write_text("{손상", encoding="utf-8")
            cache = GmarketCategoryTreeCache(path, ttl_hours=1)
            self.assertIsNone(cache.load())


# ── Fetcher (fake HTTP 세션) ────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, status_code: int, content: bytes) -> None:
        self.status_code = status_code
        self.content = content


class _FakeSession:
    """URL → 응답 매핑. 모든 대분류 페이지는 해당 대분류 서브트리를 반환."""

    def __init__(self, pages: dict[str, str], fail_codes: set[str] | None = None,
                 nav_html: str = _NAV_HTML) -> None:
        self.pages = pages          # code -> subtree html (본문만)
        self.fail_codes = fail_codes or set()
        self.nav_html = nav_html
        self.calls: list[str] = []

    def get(self, url: str, timeout: float):
        self.calls.append(url)
        import re

        m = re.search(r"listview/L(\d{9})\.aspx", url)
        code = m.group(1) if m else ""
        if code in self.fail_codes:
            return _FakeResponse(404, b"not found")
        body = self.pages.get(code, "")
        html = f"<html>{self.nav_html}{body}</html>"
        return _FakeResponse(200, _enc(html))


class FetcherTest(unittest.TestCase):
    def setUp(self):
        subtree_a = _SUBTREE_HTML  # 브랜드 여성의류용(실측과 유사)
        self.session = _FakeSession({"100000103": subtree_a})

    def test_fetch_builds_full_tree(self):
        fetcher = GmarketCategoryTreeFetcher(
            session=self.session, delay=0, on_log=lambda m: None
        )
        roots = fetcher.fetch()
        names = [r.name for r in roots]
        self.assertEqual(names[0], "브랜드 여성의류")
        self.assertIn("화장품/향수", names)
        brand = roots[0]
        self.assertTrue(brand.children)  # 서브트리 부착
        self.assertEqual(brand.children[0].name, "티셔츠")
        # 대분류 네비 4개 + 브랜드 여성의류에만 부착된 서브트리(중 2 + 소 4)
        self.assertEqual(count_nodes(roots), 4 + 2 + 4)

    def test_partial_failure_keeps_root(self):
        self.session.fail_codes = {"100000003"}
        fetcher = GmarketCategoryTreeFetcher(session=self.session, delay=0)
        roots = fetcher.fetch()
        self.assertEqual(len(roots), 4)
        by_code = {r.code: r for r in roots}
        self.assertFalse(by_code["100000003"].children)  # 실패해도 루트 유지
        self.assertTrue(by_code["100000103"].children)

    def test_empty_nav_raises(self):
        empty = _FakeSession({}, nav_html="<html>카테고리 없음</html>")
        fetcher = GmarketCategoryTreeFetcher(session=empty, delay=0)
        with self.assertRaises(GmarketCategoryFetchError):
            fetcher.fetch()

    def test_default_bootstrap_is_known_large(self):
        self.assertEqual(DEFAULT_BOOTSTRAP_CODE, "100000103")


if __name__ == "__main__":
    unittest.main()
