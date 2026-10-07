"""물량 probe(§4 2순위) 단위 테스트 — 네트워크 없음.

productCount 가 트리에 없을 때 [수집 시작] 직전에 목록 1페이지로 카테고리
크기를 추정한다(카테고리당 요청 1회 + 홈 웜업 1회). 가짜 브라우저
팩토리는 test_coupang_parallel_pieces 패턴을 따른다.
"""

from __future__ import annotations

import os
import unittest
from contextlib import contextmanager

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core.coupang.patchright_canary import HOME_URL  # noqa: E402
from app.core.coupang.patchright_top_thousand import MAX_LISTING_ITEMS  # noqa: E402
from app.core.coupang.volume_probe import (  # noqa: E402
    EXTRACT_LISTING_SIZE_JS,
    PROBE_CACHE_FILENAME,
    probe_family_volumes,
)

_FAMILY = [
    ("194688", "축산/계란/식용곤충"),
    ("194810", "계란/알류/가공란"),
    ("194817", "축산선물세트"),
    ("194620", "소고기"),
    ("194621", "돼지고기"),
]


class _Response:
    status = 200


class _BlockedResponse:
    status = 403


class _ProbePage:
    """목록 1페이지 probe용 가짜 페이지 — URL 별 결과를 스크립트로 준다."""

    def __init__(self, script=None, blocked_urls=None):
        self.script = script or {}
        self.blocked_urls = set(blocked_urls or ())
        self.url = "about:blank"
        self.visited: list[str] = []

    def goto(self, url, **_kwargs):
        self.url = str(url)
        self.visited.append(str(url))
        if any(marker in str(url) for marker in self.blocked_urls):
            return _BlockedResponse()
        return _Response()

    def wait_for_timeout(self, _milliseconds):
        return None

    def content(self):
        return "<html><body>normal page content</body></html>"

    def evaluate(self, script, argument=None):
        if script != EXTRACT_LISTING_SIZE_JS:
            raise AssertionError("unexpected script")
        result = self.script.get(self.url)
        if isinstance(result, Exception):
            raise result
        if result is None:
            return {"totalPages": None, "links": 0}
        return result


class _Factory:
    def __init__(self, page):
        self.page = page
        self.captured: list = []

    @contextmanager
    def _open(self, user_data_dir, headless=False, proxy=None):
        self.captured.append((user_data_dir, headless, proxy))
        yield _Context(self.page)

    def __call__(self, user_data_dir, *, headless=False, proxy=None):
        return self._open(user_data_dir, headless, proxy)


class _Context:
    def __init__(self, page):
        self.pages = [page]


def _url(category_id: str) -> str:
    return f"https://www.coupang.com/np/categories/{category_id}?page=1"


class ProbeVolumeTest(unittest.TestCase):
    """probe — 페이지 수·카드 수 판독, 요청 상한, 차단·실패 처리."""

    def test_reads_total_pages_and_fallbacks(self):
        script = {
            _url("194688"): {"totalPages": 120, "links": 58},   # 120쪽 → 7,200
            _url("194810"): {"totalPages": None, "links": 30},  # 총수 미상 → 1페이지 분
            _url("194817"): {"totalPages": None, "links": 0},   # 빈 목록 → 최소
        }
        page = _ProbePage(script)
        volumes = probe_family_volumes(_FAMILY[:3], _Factory(page))
        self.assertEqual(volumes["194688"], 120 * MAX_LISTING_ITEMS)
        self.assertEqual(volumes["194810"], MAX_LISTING_ITEMS)
        self.assertEqual(volumes["194817"], 1)

    def test_request_cap_is_one_page_per_category_plus_home(self):
        """요청 상한 — 홈 웜업 1회 + 카테고리당 목록 1페이지(§4)."""
        page = _ProbePage()
        probe_family_volumes(_FAMILY, _Factory(page))
        category_visits = [
            url for url in page.visited if "/np/categories/" in url
        ]
        self.assertEqual(len(category_visits), len(_FAMILY))
        for url in category_visits:
            self.assertIn("?page=1", url)  # 1페이지만 본다
        self.assertIn(HOME_URL, page.visited)

    def test_progress_callback_reports_every_category(self):
        page = _ProbePage({_url("194688"): {"totalPages": 3, "links": 9}})
        seen: list = []
        volumes = probe_family_volumes(
            _FAMILY[:2], _Factory(page),
            on_progress=lambda name, volume, index, total: seen.append(
                (name, volume, index, total)
            ),
        )
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], ("축산/계란/식용곤충", 3 * MAX_LISTING_ITEMS, 1, 2))
        self.assertEqual(seen[1][2:], (2, 2))
        self.assertIn("194688", volumes)

    def test_evaluate_failure_leaves_category_unknown(self):
        """판독 실패 카테고리는 물량 미지 — 결과에 포함하지 않고 계속한다."""
        script = {
            _url("194688"): {"totalPages": 5, "links": 9},
            _url("194810"): RuntimeError("세션 종료"),
        }
        page = _ProbePage(script)
        volumes = probe_family_volumes(_FAMILY[:2], _Factory(page))
        self.assertEqual(volumes, {"194688": 5 * MAX_LISTING_ITEMS})

    def test_block_signal_stops_probe_and_returns_partial(self):
        """차단 신호를 만나면 조사를 멈추고 그때까지의 결과만 돌려준다."""
        script = {_url("194688"): {"totalPages": 5, "links": 9}}
        page = _ProbePage(script, blocked_urls=["194810"])
        volumes = probe_family_volumes(_FAMILY[:3], _Factory(page))
        self.assertEqual(volumes, {"194688": 5 * MAX_LISTING_ITEMS})
        # 차단 이후 카테고리는 조사하지 않는다.
        self.assertFalse(any("194817" in url for url in page.visited))

    def test_blocked_home_returns_empty(self):
        page = _ProbePage(blocked_urls=[HOME_URL])
        volumes = probe_family_volumes(_FAMILY[:2], _Factory(page))
        self.assertEqual(volumes, {})
        self.assertEqual(
            [url for url in page.visited if "/np/categories/" in url], []
        )


class PlanVolumeProbeTest(unittest.TestCase):
    """상위 진입 — 캐시 재사용, 직접 회선 차단 시 decodo 조사 회선 재시도."""

    def _fake_decodo_module(self):
        import sys
        import types

        module = types.ModuleType("app.core.decodo")
        module.credentials_ready = lambda settings=None: True
        module.load_settings = lambda path=None: object()
        module.sticky_proxy_dict = (
            lambda settings=None, session_id="": {"server": "decodo", "sid": session_id}
        )
        return module

    def test_direct_success_saves_cache_and_skips_decodo(self):
        import tempfile
        from pathlib import Path

        from app.core.coupang import volume_probe

        page = _ProbePage({_url("194688"): {"totalPages": 4, "links": 9}})
        factory = _Factory(page)
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / PROBE_CACHE_FILENAME
            volumes = volume_probe.plan_volume_probe(
                _FAMILY[:2], factory, cache_path=cache_path
            )
            self.assertIn("194688", volumes)
            self.assertTrue(cache_path.exists())
            # 직접 회선 1회만 — 프록시가 붙은 조사 회선은 쓰지 않는다.
            self.assertEqual([proxy for _, _, proxy in factory.captured], [None])
            # 같은 가족 재조사는 캐시를 쓴다 — 브라우저를 다시 안 연다.
            cached_calls = len(factory.captured)
            seen_cached: list = []
            again = volume_probe.plan_volume_probe(
                _FAMILY[:2], factory,
                on_cached=lambda: seen_cached.append(True),
                cache_path=cache_path,
            )
            self.assertEqual(again, volumes)
            self.assertEqual(len(factory.captured), cached_calls)
            self.assertEqual(seen_cached, [True])

    def test_blocked_direct_falls_back_to_decodo_probe_line(self):
        """직접 회선 차단 → decodo 조사 전용 회선(sid i990)으로 재시도한다."""
        import sys
        from unittest import mock

        from app.core.coupang import volume_probe

        blocked_page = _ProbePage(blocked_urls=[HOME_URL])
        ok_page = _ProbePage({_url("194688"): {"totalPages": 10, "links": 9}})

        class _Routing:
            def __init__(self):
                self.captured: list = []

            @contextmanager
            def _open(self, user_data_dir, headless=False, proxy=None):
                self.captured.append(proxy)
                # 직접(proxy=None)은 차단 페이지, 프록시 회선은 정상 페이지.
                yield _Context(blocked_page if proxy is None else ok_page)

            def __call__(self, user_data_dir, *, headless=False, proxy=None):
                return self._open(user_data_dir, headless, proxy)

        routing = _Routing()
        fake = self._fake_decodo_module()
        with mock.patch.dict(sys.modules, {"app.core.decodo": fake}):
            volumes = volume_probe.plan_volume_probe(_FAMILY[:2], routing)
        self.assertEqual(routing.captured, [None, {"server": "decodo", "sid": "i990"}])
        # 194810 은 빈 목록(판독 결과 없음) — 최소 물량 1로 계획에 참여.
        self.assertEqual(
            volumes, {"194688": 10 * MAX_LISTING_ITEMS, "194810": 1}
        )

    def test_stale_or_other_family_cache_is_ignored(self):
        import tempfile
        from pathlib import Path

        from app.core.coupang import volume_probe

        page = _ProbePage({_url("194688"): {"totalPages": 2, "links": 9}})
        factory = _Factory(page)
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / PROBE_CACHE_FILENAME
            # 다른 가족의 캐시 — 재사용하지 않는다.
            volume_probe._save_cached_volumes(
                cache_path, [("999999", "다른가족")], {"999999": 100}
            )
            volumes = volume_probe.plan_volume_probe(
                _FAMILY[:2], factory, cache_path=cache_path
            )
            self.assertIn("194688", volumes)
            self.assertEqual(len(factory.captured), 1)  # 브라우저를 열었다
            # 만료된 캐시 — 재사용하지 않는다.
            stale = Path(tmp) / "stale.json"
            volume_probe._save_cached_volumes(stale, _FAMILY[:2], {"194688": 5})
            raw = __import__("json").loads(stale.read_text(encoding="utf-8"))
            raw["saved_ts"] -= volume_probe.PROBE_CACHE_TTL_SECONDS + 1
            stale.write_text(
                __import__("json").dumps(raw), encoding="utf-8"
            )
            factory2 = _Factory(
                _ProbePage({_url("194688"): {"totalPages": 3, "links": 9}})
            )
            volumes = volume_probe.plan_volume_probe(
                _FAMILY[:2], factory2, cache_path=stale
            )
            self.assertIn("194688", volumes)
            self.assertEqual(len(factory2.captured), 1)


if __name__ == "__main__":
    unittest.main()
