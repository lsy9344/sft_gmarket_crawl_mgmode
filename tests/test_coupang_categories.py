"""카테고리 트리 모듈(categories) 테스트 — 네트워크 없이 합성 픽스처 사용.

검증 항목:
- category-list 응답 파싱 (그룹/뎁스/비카테고리 승격·필터/중복 제거)
- 캐시 저장·로드·만료
- CategoryTreeFetcher (fake browser: 차단 감지/정상 로드/실패)
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from app.core.coupang.categories import (
    CategoryFetchError,
    CategoryNode,
    CategoryTreeCache,
    CategoryTreeFetcher,
    count_nodes,
    find_node,
    flatten_descendants,
    parse_category_groups,
    parse_category_node,
)


def _payload():
    """실측(poc21) 구조를 본뜬 합성 페이로드 — 쇼핑/테마/캠페인 혼재."""
    return {
        "data": {
            "gnb": {
                "shoppingComponent": [
                    {
                        "id": 100, "name": "패션의류/잡화", "linkCode": "200",
                        "linkUri": "/np/categories/200",
                        "visibleChildren": [
                            {"id": 101, "name": "여성패션", "linkCode": "201",
                             "linkUri": "/np/categories/201",
                             "visibleChildren": [
                                 {"id": 102, "name": "의류", "linkCode": "202",
                                  "linkUri": "/np/categories/202", "visibleChildren": []},
                             ]},
                            {"id": 103, "name": "남성패션", "linkCode": "203",
                             "linkUri": "/np/categories/203", "visibleChildren": []},
                        ],
                    },
                    {
                        "id": 110, "name": "출산/유아동", "linkCode": "221934",
                        "linkUri": "/np/categories/221934", "visibleChildren": [],
                    },
                    # 비카테고리 루트 + 카테고리 자식 → 자식 승격
                    {
                        "id": 120, "name": "캠페인모음", "linkCode": "999",
                        "linkUri": "/np/campaigns/999",
                        "visibleChildren": [
                            {"id": 121, "name": "하위카테고리", "linkCode": "300",
                             "linkUri": "/np/categories/300", "visibleChildren": []},
                        ],
                    },
                    # 순수 비카테고리 → 제거
                    {"id": 130, "name": "순수캠페인", "linkCode": "888",
                     "linkUri": "/np/campaigns/888", "visibleChildren": []},
                    # 중복 id → 1회만
                    {"id": 111, "name": "출산/유아동 중복", "linkCode": "221934",
                     "linkUri": "/np/categories/221934", "visibleChildren": []},
                ],
                "themeComponent": [
                    {"id": 200, "name": "안전전문관", "linkCode": "400",
                     "linkUri": "/np/categories/400", "visibleChildren": []},
                ],
                "travelComponent": [],
                "localAndCultureComponent": [],
            }
        }
    }


class ParseTest(unittest.TestCase):
    def test_parse_groups_and_depth(self):
        groups = parse_category_groups(_payload())
        labels = [g[0] for g in groups]
        self.assertIn("쇼핑", labels)
        self.assertIn("테마", labels)
        self.assertNotIn("여행", labels)  # 빈 그룹 제외
        shop = dict(groups)["쇼핑"]
        # 패션 + 출산/유아동 + 승격된 하위카테고리 = 루트 3개 (중복·비카테고리 제거)
        self.assertEqual([n.id for n in shop], ["200", "221934", "300"])
        fashion = shop[0]
        self.assertEqual(fashion.name, "패션의류/잡화")
        self.assertEqual([c.id for c in fashion.children], ["201", "203"])
        self.assertEqual(fashion.children[0].children[0].id, "202")

    def test_count_and_find(self):
        groups = parse_category_groups(_payload())
        self.assertEqual(count_nodes(groups), 7)  # 200,201,202,203,221934,300,400
        node = find_node(groups, "221934")
        self.assertIsNotNone(node)
        self.assertEqual(node.name, "출산/유아동")
        self.assertIsNone(find_node(groups, "999999"))

    def test_flatten_descendants(self):
        groups = parse_category_groups(_payload())
        fashion = find_node(groups, "200")
        subs = flatten_descendants(fashion)
        self.assertEqual([n.id for n in subs], ["201", "202", "203"])
        leaf = find_node(groups, "202")
        self.assertEqual(flatten_descendants(leaf), [])

    def test_parse_node_non_category_without_children_dropped(self):
        node = parse_category_node({"name": "x", "linkUri": "/np/campaigns/1",
                                    "visibleChildren": []})
        self.assertIsNone(node)

    def test_empty_payload(self):
        self.assertEqual(parse_category_groups({}), [])
        self.assertEqual(parse_category_groups({"data": {}}), [])
        # data 가 벗겨진 형태도 방어
        groups = parse_category_groups({"gnb": _payload()["data"]["gnb"]})
        self.assertEqual(count_nodes(groups), 7)

    def test_node_serialization_roundtrip(self):
        node = CategoryNode(id="1", name="테스트", uri="/np/categories/1",
                            children=[CategoryNode(id="2", name="자식",
                                                   uri="/np/categories/2")])
        back = CategoryNode.from_dict(json.loads(json.dumps(node.to_dict())))
        self.assertEqual(back.id, "1")
        self.assertEqual(back.children[0].name, "자식")


class ProductCountTest(unittest.TestCase):
    """productCount 보존(볼륨 인지 분할 설계 §4 1순위) — 파싱·직렬화·캐시."""

    def test_parse_preserves_product_count(self):
        raw = {
            "id": 1, "name": "채소", "linkCode": "194432",
            "linkUri": "/np/categories/194432",
            "productCount": 377000,
            "visibleChildren": [],
        }
        node = parse_category_node(raw)
        self.assertEqual(node.product_count, 377000)

    def test_missing_or_invalid_product_count_is_unknown_zero(self):
        for bad in (None, "377", -5, 1.5, True, {}):
            with self.subTest(bad=bad):
                raw = {
                    "id": 1, "name": "채소", "linkCode": "194432",
                    "linkUri": "/np/categories/194432",
                    "productCount": bad,
                    "visibleChildren": [],
                }
                self.assertEqual(parse_category_node(raw).product_count, 0)
        raw = {
            "id": 1, "name": "채소", "linkCode": "194432",
            "linkUri": "/np/categories/194432",
            "visibleChildren": [],
        }
        self.assertEqual(parse_category_node(raw).product_count, 0)

    def test_float_integer_product_count_is_accepted(self):
        raw = {
            "id": 1, "name": "채소", "linkCode": "194432",
            "linkUri": "/np/categories/194432",
            "productCount": 120.0,
            "visibleChildren": [],
        }
        self.assertEqual(parse_category_node(raw).product_count, 120)

    def test_node_serialization_keeps_product_count(self):
        node = CategoryNode(
            id="194432", name="채소", uri="/np/categories/194432",
            product_count=377000,
        )
        raw = json.loads(json.dumps(node.to_dict()))
        self.assertEqual(raw["productCount"], 377000)
        self.assertEqual(CategoryNode.from_dict(raw).product_count, 377000)
        # 옛 형식(키 없음)도 0(미지)으로 읽힌다.
        raw.pop("productCount")
        self.assertEqual(CategoryNode.from_dict(raw).product_count, 0)

    def test_cache_roundtrip_preserves_product_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = CategoryTreeCache(Path(tmp) / "tree.json")
            payload = _payload()
            payload["data"]["gnb"]["shoppingComponent"][0]["productCount"] = 98765
            cache.save(parse_category_groups(payload))
            groups, _stamp = cache.load()
            node = find_node(groups, "200")
            self.assertEqual(node.product_count, 98765)
            # 키가 없는 옛 캐시도 그대로 읽힌다(0=미지).
            node2 = find_node(groups, "201")
            self.assertEqual(node2.product_count, 0)

    def test_cache_rejects_wrong_product_count_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tree.json"
            payload = _payload()
            groups = parse_category_groups(payload)
            CategoryTreeCache(path).save(groups)
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["groups"][0][1][0]["productCount"] = "many"
            path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            # 형식이 틀린 캐시는 버려진다(시드 폴백과 같은 경로).
            loaded = CategoryTreeCache(path).load()
            self.assertIsNone(loaded)


class CacheTest(unittest.TestCase):
    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = CategoryTreeCache(Path(tmp) / "tree.json")
            groups = parse_category_groups(_payload())
            stamp = cache.save(groups)
            self.assertTrue(stamp)
            loaded = cache.load()
            self.assertIsNotNone(loaded)
            lgroups, _ = loaded
            self.assertEqual(count_nodes(lgroups), count_nodes(groups))
            self.assertEqual(find_node(lgroups, "202").name, "의류")

    def test_missing_or_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = CategoryTreeCache(Path(tmp) / "none.json")
            self.assertIsNone(cache.load())
            # 만료
            cache.save(parse_category_groups(_payload()))
            raw = json.loads(cache.path.read_text(encoding="utf-8"))
            raw["fetched_ts"] = time.time() - 8 * 24 * 3600  # 8일 전
            cache.path.write_text(json.dumps(raw), encoding="utf-8")
            self.assertIsNone(cache.load())
            # 손상
            cache.path.write_text("{broken", encoding="utf-8")
            self.assertIsNone(cache.load())

    def test_seed_fallback_ignores_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = CategoryTreeCache(root / "seed.json")
            seed.save(parse_category_groups(_payload()))
            raw = json.loads(seed.path.read_text(encoding="utf-8"))
            raw["fetched_ts"] = time.time() - 365 * 24 * 3600
            seed.path.write_text(json.dumps(raw), encoding="utf-8")

            cache = CategoryTreeCache(root / "missing.json", seed_path=seed.path)
            loaded = cache.load()
            self.assertIsNotNone(loaded)
            self.assertEqual(count_nodes(loaded[0]), 7)

    def test_fresh_primary_cache_wins_over_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = CategoryTreeCache(root / "seed.json")
            seed.save(parse_category_groups(_payload()))
            primary = CategoryTreeCache(root / "primary.json")
            primary.save([("로컬", [CategoryNode("999", "로컬", "/np/categories/999")])])

            cache = CategoryTreeCache(primary.path, seed_path=seed.path)
            loaded = cache.load()
            self.assertEqual(loaded[0][0][0], "로컬")
            self.assertEqual(count_nodes(loaded[0]), 1)

    def test_malformed_primary_falls_back_to_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = CategoryTreeCache(root / "seed.json")
            seed.save(parse_category_groups(_payload()))
            primary = root / "primary.json"
            primary.write_text(json.dumps({
                "fetched_ts": time.time(),
                "groups": [["손상", ["not-a-node"]]],
            }), encoding="utf-8")

            loaded = CategoryTreeCache(primary, seed_path=seed.path).load()
            self.assertIsNotNone(loaded)
            self.assertEqual(count_nodes(loaded[0]), 7)

    def test_empty_or_future_primary_falls_back_to_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = CategoryTreeCache(root / "seed.json")
            seed.save(parse_category_groups(_payload()))
            primary = root / "primary.json"
            cache = CategoryTreeCache(primary, seed_path=seed.path)

            primary.write_text(json.dumps({
                "fetched_ts": time.time(),
                "groups": [["빈 그룹", []]],
            }), encoding="utf-8")
            self.assertEqual(count_nodes(cache.load()[0]), 7)

            for bad_ts in (time.time() + 1, float("nan")):
                primary.write_text(json.dumps({
                    "fetched_ts": bad_ts,
                    "groups": [["미래", [CategoryNode(
                        "999", "미래", "/np/categories/999"
                    ).to_dict()]]],
                }), encoding="utf-8")
                self.assertEqual(count_nodes(cache.load()[0]), 7)

    def test_coercible_wrong_node_types_fall_back_to_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = CategoryTreeCache(root / "seed.json")
            seed.save(parse_category_groups(_payload()))
            primary = root / "primary.json"
            primary.write_text(json.dumps({
                "fetched_at": "now",
                "fetched_ts": time.time(),
                "groups": [["손상", [{
                    "id": 999,
                    "name": ["목록 이름"],
                    "uri": "/np/categories/999",
                    "children": [],
                }]]],
            }), encoding="utf-8")

            loaded = CategoryTreeCache(primary, seed_path=seed.path).load()
            self.assertIsNotNone(loaded)
            self.assertEqual(count_nodes(loaded[0]), 7)


class FakePage:
    def __init__(self, content_html="<html>ok</html>" + "x" * 3000, evaluate_result=None):
        self._html = content_html
        self._result = evaluate_result

    def goto(self, url, **kwargs):
        pass

    def content(self):
        return self._html

    def evaluate(self, script, *args):
        if "category-list" in script:
            return self._result
        return {}

    @property
    def mouse(self):
        return self

    def move(self, x, y):
        pass

    def wheel(self, x, y):
        pass

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


class FetcherTest(unittest.TestCase):
    def test_fetch_success(self):
        page = FakePage(evaluate_result={"ok": True, "payload": _payload()})
        fetcher = CategoryTreeFetcher(browser_factory=lambda: FakeBrowser(page),
                                      warmup_time=0)
        groups = fetcher.fetch()
        self.assertEqual(count_nodes(groups), 7)

    def test_fetch_blocked(self):
        blocked = "요청하신 페이지의 사용권한이 없습니다." + "x" * 3000
        page = FakePage(content_html="<html>" + blocked + "</html>")
        fetcher = CategoryTreeFetcher(browser_factory=lambda: FakeBrowser(page),
                                      warmup_time=0)
        with self.assertRaises(CategoryFetchError):
            fetcher.fetch()

    def test_fetch_request_failure(self):
        page = FakePage(evaluate_result={"error": "timeout"})
        fetcher = CategoryTreeFetcher(browser_factory=lambda: FakeBrowser(page),
                                      warmup_time=0)
        with self.assertRaises(CategoryFetchError):
            fetcher.fetch()

    def test_fetch_empty_tree(self):
        page = FakePage(evaluate_result={"ok": True, "payload": {"data": {"gnb": {}}}})
        fetcher = CategoryTreeFetcher(browser_factory=lambda: FakeBrowser(page),
                                      warmup_time=0)
        with self.assertRaises(CategoryFetchError):
            fetcher.fetch()


if __name__ == "__main__":
    unittest.main()
