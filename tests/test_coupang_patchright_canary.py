"""Patchright canary 코어 테스트 — 실제 브라우저·네트워크 없음."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from app.core.coupang.patchright_canary import (
    CATEGORY_URL,
    HOME_URL,
    advance_recovery_ramp,
    authorize_block_recovery,
    authorize_recovery_resume,
    claim_live_attempt,
    record_block,
    record_recovery_hold,
    run_canary,
    settle_live_attempt,
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

    def test_two_hour_interval_is_enforced(self):
        base = time.mktime((2026, 9, 2, 1, 0, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, _reason = claim_live_attempt(root, now=base)
            too_early, reason = claim_live_attempt(
                root, now=base + (2 * 60 * 60) - 1
            )
            on_time, _reason = claim_live_attempt(
                root, now=base + (2 * 60 * 60)
            )
        self.assertTrue(first)
        self.assertFalse(too_early)
        self.assertIn("1분", reason)
        self.assertTrue(on_time)

    def test_daily_product_envelope_refuses_more_than_fifteen_hundred(self):
        base = time.mktime((2026, 9, 2, 1, 0, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, _reason = claim_live_attempt(
                root, now=base, planned_items=600
            )
            second, _reason = claim_live_attempt(
                root, now=base + (3 * 60 * 60), planned_items=600
            )
            third, reason = claim_live_attempt(
                root, now=base + (6 * 60 * 60), planned_items=600
            )
            guard = json.loads((root / "canary_guard.json").read_text("utf-8"))
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertFalse(third)
        self.assertIn("1,500", reason)
        self.assertEqual(guard["daily_items_reserved"], 1_200)

    def test_daily_session_envelope_refuses_fourth_session(self):
        base = time.mktime((2026, 9, 2, 1, 0, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attempts = [
                claim_live_attempt(root, now=base + (index * 3 * 60 * 60))
                for index in range(4)
            ]
        self.assertEqual(
            [allowed for allowed, _reason in attempts],
            [True, True, True, False],
        )
        self.assertIn("3회", attempts[-1][1])

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


if __name__ == "__main__":
    unittest.main()
