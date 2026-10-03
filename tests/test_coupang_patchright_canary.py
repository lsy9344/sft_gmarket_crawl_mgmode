"""Patchright canary 코어 테스트 — 실제 브라우저·네트워크 없음.

병렬 수집 코어 포팅(M1) 추가 검증: state_dir 기본 경로는 앱 배포
원칙(SellerCollector 산하)을 따르고, patchright_browser 는 channel="chrome"
실행 실패 시 번들 chromium으로 재시도한다.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from app.core.config import PROJECT_ROOT
from app.core.coupang.patchright_canary import (
    CATEGORY_URL,
    HOME_URL,
    advance_recovery_ramp,
    authorize_block_recovery,
    authorize_recovery_resume,
    claim_live_attempt,
    patchright_browser,
    record_block,
    record_recovery_hold,
    run_canary,
    settle_live_attempt,
    state_dir,
)


class _Response:
    status = 200


class _Page:
    def __init__(self, *, blocked=False, fail=False) -> None:
        self.url = "about:blank"
        self.urls: list[str] = []
        self.blocked = blocked
        self.fail = fail

    def goto(self, url, **_kwargs):
        if self.fail:
            raise RuntimeError("navigation failed")
        self.url = str(url)
        self.urls.append(self.url)
        return _Response()

    def wait_for_timeout(self, _milliseconds):
        return None

    def content(self):
        if self.blocked and self.url == HOME_URL:
            return "Access Denied Reference #18.test"
        return "<html><body>normal page content that is not blocked</body></html>"

    def evaluate(self, _script, limit):
        return [{"href": f"/vp/products/{index}", "title": f"상품 {index}"}
                for index in range(limit)]

    def set_content(self, _html):
        self.url = "about:blank"


class _Context:
    def __init__(self, page: _Page) -> None:
        self.pages = [page]
        self.offline = False
        self.closed = False

    def new_page(self):
        return self.pages[0]

    def set_offline(self, value):
        self.offline = bool(value)


def _factory(page: _Page, calls: list[tuple[Path, bool]], context: _Context):
    @contextmanager
    def open_browser(user_data_dir, *, headless=False):
        calls.append((Path(user_data_dir), headless))
        try:
            yield context
        finally:
            context.closed = True

    return open_browser


class PatchrightCanaryTest(unittest.TestCase):
    def test_offline_mode_never_navigates(self):
        page = _Page()
        context = _Context(page)
        calls: list[tuple[Path, bool]] = []
        result = run_canary(
            browser_scope_factory=_factory(page, calls, context),
            limit=2,
        )
        self.assertEqual(result["event"], "offline_completed")
        self.assertEqual(page.urls, [])
        self.assertTrue(context.offline)
        self.assertTrue(context.closed)
        self.assertTrue(calls[0][1])

    def test_live_mode_opens_exactly_home_and_one_category(self):
        page = _Page()
        context = _Context(page)
        calls: list[tuple[Path, bool]] = []
        with tempfile.TemporaryDirectory() as tmp, patch(
            "app.core.coupang.patchright_canary.time.time", return_value=1_000.0
        ):
            result = run_canary(
                live_category="176573",
                state_root=Path(tmp),
                browser_scope_factory=_factory(page, calls, context),
            )
        self.assertEqual(result["event"], "live_completed")
        self.assertEqual(
            page.urls,
            [HOME_URL, CATEGORY_URL.format(category_id="176573")],
        )
        self.assertFalse(calls[0][1])
        self.assertTrue(context.closed)

    def test_second_attempt_inside_interval_is_refused_before_browser(self):
        page = _Page()
        context = _Context(page)
        calls: list[tuple[Path, bool]] = []
        with tempfile.TemporaryDirectory() as tmp, patch(
            "app.core.coupang.patchright_canary.time.time", return_value=1_000.0
        ):
            root = Path(tmp)
            first = run_canary(
                live_category="176573",
                state_root=root,
                browser_scope_factory=_factory(page, calls, context),
            )
            second = run_canary(
                live_category="176573",
                state_root=root,
                browser_scope_factory=_factory(page, calls, context),
            )
        self.assertEqual(first["event"], "live_completed")
        self.assertEqual(second["event"], "guard_refused")
        self.assertEqual(len(calls), 1)

    def test_sixty_minute_interval_is_enforced(self):
        base = time.mktime((2026, 9, 2, 1, 0, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, _reason = claim_live_attempt(root, now=base)
            too_early, reason = claim_live_attempt(
                root, now=base + (60 * 60) - 1
            )
            on_time, _reason = claim_live_attempt(
                root, now=base + (60 * 60)
            )
        self.assertTrue(first)
        self.assertFalse(too_early)
        self.assertIn("1분", reason)
        self.assertTrue(on_time)

    def test_daily_product_envelope_is_not_enforced(self):
        base = time.mktime((2026, 9, 2, 1, 0, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, _reason = claim_live_attempt(
                root, now=base, planned_items=600
            )
            second, _reason = claim_live_attempt(
                root, now=base + (3 * 60 * 60), planned_items=600
            )
            third, _reason = claim_live_attempt(
                root, now=base + (6 * 60 * 60), planned_items=600
            )
            fourth, reason = claim_live_attempt(
                root, now=base + (8 * 60 * 60), planned_items=600
            )
            fifth, _reason = claim_live_attempt(
                root, now=base + (10 * 60 * 60), planned_items=600
            )
            guard = json.loads((root / "canary_guard.json").read_text("utf-8"))
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertTrue(third)
        self.assertTrue(fourth)
        self.assertTrue(fifth)
        self.assertEqual(guard["daily_items_reserved"], 3_000)

    def test_daily_session_envelope_is_not_enforced(self):
        base = time.mktime((2026, 9, 2, 0, 0, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attempts = [
                claim_live_attempt(root, now=base + (index * 61 * 60))
                for index in range(16)
            ]
        self.assertEqual(
            [allowed for allowed, _reason in attempts],
            [True] * 16,
        )

    def test_rolling_page_envelope_is_not_enforced(self):
        base = time.mktime((2026, 9, 2, 1, 0, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, _reason = claim_live_attempt(
                root, now=base, planned_pages=10
            )
            second, _reason = claim_live_attempt(
                root,
                now=base + (2 * 60 * 60),
                planned_pages=10,
            )
            third, _reason = claim_live_attempt(
                root,
                now=base + (4 * 60 * 60),
                planned_pages=10,
            )
            fourth, _reason = claim_live_attempt(
                root,
                now=base + (6 * 60 * 60),
                planned_pages=10,
            )
            fifth, _reason = claim_live_attempt(
                root,
                now=base + (8 * 60 * 60),
                planned_pages=6,
            )
            next_attempt, _reason = claim_live_attempt(
                root,
                now=base + (10 * 60 * 60),
                planned_pages=1,
            )
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertTrue(third)
        self.assertTrue(fourth)
        self.assertTrue(fifth)
        self.assertTrue(next_attempt)

    def test_rolling_item_envelope_is_not_enforced(self):
        base = time.mktime((2026, 9, 2, 23, 0, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, _reason = claim_live_attempt(
                root, now=base, planned_items=600
            )
            second, _reason = claim_live_attempt(
                root,
                now=base + (2 * 60 * 60),
                planned_items=600,
            )
            third, _reason = claim_live_attempt(
                root,
                now=base + (4 * 60 * 60),
                planned_items=600,
            )
            fourth, _reason = claim_live_attempt(
                root,
                now=base + (6 * 60 * 60),
                planned_items=600,
            )
            fifth, _reason = claim_live_attempt(
                root,
                now=base + (8 * 60 * 60),
                planned_items=600,
            )
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertTrue(third)
        self.assertTrue(fourth)
        self.assertTrue(fifth)

    def test_rolling_envelope_expires_after_twenty_four_hours(self):
        base = time.mktime((2026, 9, 2, 1, 0, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, _reason = claim_live_attempt(
                root,
                now=base,
                planned_items=600,
                planned_pages=10,
            )
            next_day, _reason = claim_live_attempt(
                root,
                now=base + (24 * 60 * 60),
                planned_items=600,
                planned_pages=10,
            )
            guard = json.loads((root / "canary_guard.json").read_text("utf-8"))
        self.assertTrue(first)
        self.assertTrue(next_day)
        self.assertEqual(len(guard["attempt_history"]), 1)

    def test_clean_session_returns_unused_rolling_reservation(self):
        base = time.mktime((2026, 9, 2, 1, 0, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allowed, _reason = claim_live_attempt(
                root,
                now=base,
                planned_items=600,
                planned_pages=10,
            )
            settled, _reason = settle_live_attempt(
                600,
                120,
                root,
                planned_pages=10,
                actual_pages=4,
            )
            next_allowed, _reason = claim_live_attempt(
                root,
                now=base + (2 * 60 * 60),
                planned_items=600,
                planned_pages=10,
            )
        self.assertTrue(allowed)
        self.assertTrue(settled)
        self.assertTrue(next_allowed)

    def test_daily_envelope_resets_on_next_calendar_day(self):
        first_day = time.mktime((2026, 9, 2, 1, 0, 0, 0, 0, -1))
        next_day = time.mktime((2026, 9, 3, 1, 0, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, _reason = claim_live_attempt(
                root, now=first_day, planned_items=600
            )
            second, _reason = claim_live_attempt(
                root, now=first_day + (3 * 60 * 60), planned_items=600
            )
            next_day_allowed, _reason = claim_live_attempt(
                root, now=next_day, planned_items=600
            )
            guard = json.loads((root / "canary_guard.json").read_text("utf-8"))
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertTrue(next_day_allowed)
        self.assertEqual(guard["daily_sessions"], 1)
        self.assertEqual(guard["daily_items_reserved"], 600)

    def test_clean_session_returns_only_unused_daily_reservation(self):
        base = time.mktime((2026, 9, 3, 0, 10, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allowed, _reason = claim_live_attempt(
                root, now=base, planned_items=600
            )
            settled, _reason = settle_live_attempt(600, 180, root)
            guard = json.loads((root / "canary_guard.json").read_text("utf-8"))
        self.assertTrue(allowed)
        self.assertTrue(settled)
        self.assertEqual(guard["daily_items_reserved"], 180)

    def test_recovery_resume_requires_next_day_and_enforces_ramp(self):
        attempt = time.mktime((2026, 9, 1, 21, 53, 51, 0, 0, -1))
        hold = time.mktime((2026, 9, 1, 21, 56, 4, 0, 0, -1))
        next_day = time.mktime((2026, 9, 2, 0, 55, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, _reason = claim_live_attempt(root, now=attempt)
            record_recovery_hold(root, now=hold)
            same_day, same_day_reason = authorize_recovery_resume(
                root, now=hold + 60
            )
            resumed, _reason = authorize_recovery_resume(root, now=next_day)
            too_large, too_large_reason = claim_live_attempt(
                root, now=next_day, planned_items=180
            )
            allowed, _reason = claim_live_attempt(
                root, now=next_day, planned_items=60
            )
            guard = json.loads((root / "canary_guard.json").read_text("utf-8"))
        self.assertTrue(first)
        self.assertFalse(same_day)
        self.assertIn("다음 날", same_day_reason)
        self.assertTrue(resumed)
        self.assertFalse(too_large)
        self.assertIn("최대 60개", too_large_reason)
        self.assertTrue(allowed)
        self.assertEqual(guard["recovery_ramp_limit"], 60)

    def test_recovery_ramp_advances_only_in_order(self):
        attempt = time.mktime((2026, 9, 1, 21, 53, 51, 0, 0, -1))
        hold = time.mktime((2026, 9, 1, 21, 56, 4, 0, 0, -1))
        next_day = time.mktime((2026, 9, 2, 0, 55, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claim_live_attempt(root, now=attempt)
            record_recovery_hold(root, now=hold)
            authorize_recovery_resume(root, now=next_day)
            wrong, wrong_reason = advance_recovery_ramp(180, root, now=next_day)
            first, _reason = advance_recovery_ramp(60, root, now=next_day)
            second, _reason = advance_recovery_ramp(
                180, root, now=next_day + (3 * 60 * 60)
            )
            third, _reason = advance_recovery_ramp(
                600, root, now=next_day + (6 * 60 * 60)
            )
            guard = json.loads((root / "canary_guard.json").read_text("utf-8"))
        self.assertFalse(wrong)
        self.assertIn("맞지 않습니다", wrong_reason)
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertTrue(third)
        self.assertNotIn("recovery_ramp_limit", guard)
        self.assertTrue(guard["recovery_ramp_completed"])

    def test_access_denied_stops_at_home_and_latches_guard(self):
        page = _Page(blocked=True)
        context = _Context(page)
        calls: list[tuple[Path, bool]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_canary(
                live_category="176573",
                state_root=root,
                browser_scope_factory=_factory(page, calls, context),
            )
            guard = json.loads((root / "canary_guard.json").read_text("utf-8"))
        self.assertEqual(result["event"], "blocked")
        self.assertEqual(result["reference"], "Reference #18.test")
        self.assertEqual(page.urls, [HOME_URL])
        self.assertTrue(guard["blocked"])
        self.assertTrue(context.closed)

    def test_old_block_record_refuses_without_browser(self):
        calls: list[tuple[Path, bool]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record_block(root, reference="Reference #18.old")
            result = run_canary(
                live_category="176573",
                state_root=root,
                browser_scope_factory=_factory(_Page(), calls, _Context(_Page())),
            )
        self.assertEqual(result["event"], "guard_refused")
        self.assertEqual(calls, [])

    def test_block_recovery_is_refused_before_one_hour(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record_block(root, reference="Reference #18.wait", now=1_000.0)
            allowed, reason = authorize_block_recovery(root, now=4_599.0)
            guard = json.loads((root / "canary_guard.json").read_text("utf-8"))
        self.assertFalse(allowed)
        self.assertIn("1분", reason)
        self.assertTrue(guard["blocked"])

    def test_block_recovery_after_one_hour_preserves_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record_block(root, reference="Reference #18.kept", now=1_000.0)
            allowed, reason = authorize_block_recovery(root, now=4_600.0)
            guard = json.loads((root / "canary_guard.json").read_text("utf-8"))
        self.assertTrue(allowed)
        self.assertEqual(reason, "")
        self.assertFalse(guard["blocked"])
        self.assertEqual(guard["block_history"][0]["reference"], "Reference #18.kept")

    def test_recovery_hold_refuses_another_live_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record_block(root, reference="Reference #18.once", now=1_000.0)
            allowed, _reason = authorize_block_recovery(root, now=4_600.0)
            self.assertTrue(allowed)
            record_recovery_hold(root, now=4_700.0)
            allowed, reason = claim_live_attempt(root, now=10_000.0)
            guard = json.loads((root / "canary_guard.json").read_text("utf-8"))
        self.assertFalse(allowed)
        self.assertIn("소량 확인", reason)
        self.assertTrue(guard["recovery_hold"])

    def test_corrupt_guard_fails_closed(self):
        calls: list[tuple[Path, bool]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "canary_guard.json").write_text("not-json", encoding="utf-8")
            result = run_canary(
                live_category="176573",
                state_root=root,
                browser_scope_factory=_factory(_Page(), calls, _Context(_Page())),
            )
        self.assertEqual(result["event"], "guard_refused")
        self.assertIn("손상", result["reason"])
        self.assertEqual(calls, [])

    def test_wrong_guard_schema_fails_closed(self):
        calls: list[tuple[Path, bool]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "canary_guard.json").write_text(
                '{"blocked": false, "last_attempt_ts": "unknown"}',
                encoding="utf-8",
            )
            result = run_canary(
                live_category="176573",
                state_root=root,
                browser_scope_factory=_factory(_Page(), calls, _Context(_Page())),
            )
        self.assertEqual(result["event"], "guard_refused")
        self.assertIn("형식", result["reason"])
        self.assertEqual(calls, [])

    def test_navigation_error_still_closes_browser(self):
        page = _Page(fail=True)
        context = _Context(page)
        with tempfile.TemporaryDirectory() as tmp:
            result = run_canary(
                live_category="176573",
                state_root=Path(tmp),
                browser_scope_factory=_factory(page, [], context),
            )
        self.assertEqual(result["event"], "failed")
        self.assertTrue(context.closed)


class PatchrightBrowserProxyTest(unittest.TestCase):
    """가짜 patchright 주입으로 proxy 전달만 검증한다(오프라인)."""

    @staticmethod
    def _fake_patchright_modules(launched: dict):
        """launch_persistent_context 호출 인자를 기록하는 가짜 patchright."""
        import types

        class _FakeContext:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class _FakeChromium:
            def launch_persistent_context(self, **kwargs):
                launched.update(kwargs)
                return _FakeContext()

        class _FakePlaywright:
            def __init__(self):
                self.chromium = _FakeChromium()

        class _FakeSyncPlaywright:
            def __enter__(self):
                return _FakePlaywright()

            def __exit__(self, *_args):
                return False

        fake_sync_api = types.ModuleType("patchright.sync_api")
        fake_sync_api.sync_playwright = _FakeSyncPlaywright
        fake_package = types.ModuleType("patchright")
        fake_package.sync_api = fake_sync_api
        return {"patchright": fake_package, "patchright.sync_api": fake_sync_api}

    def test_proxy_dict_is_forwarded_to_launch(self):
        import sys

        launched: dict = {}
        proxy = {
            "server": "http://gate.decodo.com:7000",
            "username": (
                "user-sp3lqmo64w-session-b01"
                "-sessionduration-1440-country-kr"
            ),
            "password": "secret",
        }
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            sys.modules, self._fake_patchright_modules(launched)
        ), patchright_browser(
            Path(tmp) / "profile", headless=True, proxy=proxy
        ) as context:
            self.assertIsNotNone(context)
        self.assertEqual(launched["proxy"], proxy)
        self.assertEqual(launched["channel"], "chrome")
        self.assertTrue(launched["headless"])
        self.assertEqual(launched["user_data_dir"], str(Path(tmp) / "profile"))

    def test_default_call_launches_with_proxy_none(self):
        import sys

        launched: dict = {}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            sys.modules, self._fake_patchright_modules(launched)
        ), patchright_browser(Path(tmp) / "profile", headless=True):
            pass
        self.assertIsNone(launched["proxy"])


class PatchrightBrowserChannelFallbackTest(unittest.TestCase):
    """channel="chrome" 실행 실패 시 번들 chromium 재시도 폴백(오프라인)."""

    @staticmethod
    def _fake_patchright_modules(launched: list[dict], *, fail_chrome: bool):
        """launch_persistent_context 호출 인자를 호출순 리스트로 기록한다.

        fail_chrome 이면 channel="chrome" 시도가 예외로 실패한다(설치
        Chrome 부재 상황). 그 외 호출은 성공한다.
        """
        import types

        class _FakeContext:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class _FakeChromium:
            def launch_persistent_context(self, **kwargs):
                launched.append(dict(kwargs))
                if fail_chrome and kwargs.get("channel") == "chrome":
                    raise RuntimeError("Executable doesn't exist at chrome path")
                return _FakeContext()

        class _FakePlaywright:
            def __init__(self):
                self.chromium = _FakeChromium()

        class _FakeSyncPlaywright:
            def __enter__(self):
                return _FakePlaywright()

            def __exit__(self, *_args):
                return False

        fake_sync_api = types.ModuleType("patchright.sync_api")
        fake_sync_api.sync_playwright = _FakeSyncPlaywright
        fake_package = types.ModuleType("patchright")
        fake_package.sync_api = fake_sync_api
        return {"patchright": fake_package, "patchright.sync_api": fake_sync_api}

    def test_chrome_failure_retries_once_with_bundled_chromium(self):
        """chrome 시도 실패 → channel 없는 번들 chromium으로 정확히 1회 재시도."""
        import sys

        launched: list[dict] = []
        proxy = {"server": "http://gate.decodo.com:7000", "username": "u", "password": "p"}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            sys.modules, self._fake_patchright_modules(launched, fail_chrome=True)
        ), patchright_browser(
            Path(tmp) / "profile", headless=True, proxy=proxy
        ) as context:
            self.assertIsNotNone(context)
        self.assertEqual(len(launched), 2)
        self.assertEqual(launched[0]["channel"], "chrome")
        # 폴백 시도는 channel 인자 없이(번들 chromium), 나머지는 그대로.
        self.assertNotIn("channel", launched[1])
        self.assertEqual(launched[1]["user_data_dir"], str(Path(tmp) / "profile"))
        self.assertTrue(launched[1]["headless"])
        self.assertEqual(launched[1]["proxy"], proxy)

    def test_chrome_success_never_falls_back(self):
        """첫 시도가 성공하면 정확히 1회 호출로 끝난다(기존 동작 보존)."""
        import sys

        launched: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            sys.modules, self._fake_patchright_modules(launched, fail_chrome=False)
        ), patchright_browser(Path(tmp) / "profile", headless=True):
            pass
        self.assertEqual(len(launched), 1)
        self.assertEqual(launched[0]["channel"], "chrome")

    def test_both_channels_failing_propagates_error(self):
        """두 시도 모두 실패하면 예외를 그대로 밖으로 던진다."""
        import sys
        import types

        class _AlwaysFailChromium:
            def launch_persistent_context(self, **kwargs):
                launched.append(dict(kwargs))
                raise RuntimeError("어떤 chromium으로도 시작할 수 없습니다")

        class _FakeSyncPlaywright:
            def __enter__(self):
                playwright = types.SimpleNamespace(chromium=_AlwaysFailChromium())
                return playwright

            def __exit__(self, *_args):
                return False

        launched: list[dict] = []
        modules = self._fake_patchright_modules(launched, fail_chrome=False)
        # sync_playwright 컨텍스트가 항상 실패 chromium을 돌려주게 교체한다.
        modules["patchright.sync_api"].sync_playwright = _FakeSyncPlaywright
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, modules):
            with self.assertRaises(RuntimeError):
                with patchright_browser(Path(tmp) / "profile", headless=True):
                    pass
        self.assertEqual(len(launched), 2)


class StateDirDefaultTest(unittest.TestCase):
    """state_dir 기본 경로 — 앱 배포 원칙(SellerCollector 산하)으로 이전.

    프로토타입 경로(SellerCollectorPatchrightCanary, runtime_profile/
    patchright_canary)와 분리된다. 매니저가 항상 명시적 state_root를 넘기므로
    이 기본값은 폴백 역할이지만, 경로 충돌(프로토타입 장부 공유)은
    원천 차단해야 한다.
    """

    def test_windows_default_uses_sellercollector_coupang_parallel(self):
        # Linux 테스트 환경에선 os.name="nt" 로 concrete Path를 만들 수
        # 없어(pathlib 플래버 선택) 순수 경로형으로 nt 분기를 검증한다.
        from pathlib import PureWindowsPath

        with patch("os.name", "nt"), patch.dict(
            os.environ, {"LOCALAPPDATA": r"C:\Users\u\AppData\Local"}
        ), patch(
            "app.core.coupang.patchright_canary.Path", PureWindowsPath
        ):
            value = state_dir()
        self.assertEqual(
            value,
            PureWindowsPath(
                r"C:\Users\u\AppData\Local\SellerCollector\coupang_parallel\canary"
            ),
        )

    def test_posix_default_uses_project_runtime_profile(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                state_dir(),
                PROJECT_ROOT / "runtime_profile" / "coupang_parallel_canary",
            )
        # LOCALAPPDATA가 있어도 posix에서는 무시한다.
        with patch.dict(os.environ, {"LOCALAPPDATA": "/fake/localappdata"}):
            self.assertEqual(
                state_dir(),
                PROJECT_ROOT / "runtime_profile" / "coupang_parallel_canary",
            )

    def test_profile_and_guard_live_under_state_dir(self):
        """profile_dir/_guard_path 는 같은 루트 아래를 가리킨다(호환 유지)."""
        from app.core.coupang.patchright_canary import _guard_path, profile_dir

        root = Path("/tmp") / "명시적-root"
        self.assertEqual(profile_dir(root), root / "chrome_profile")
        self.assertEqual(_guard_path(root), root / "canary_guard.json")


if __name__ == "__main__":
    unittest.main()
