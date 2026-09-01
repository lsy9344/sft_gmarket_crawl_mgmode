"""Patchright canary 코어 테스트 — 실제 브라우저·네트워크 없음."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from app.core.coupang.patchright_canary import (
    CATEGORY_URL,
    HOME_URL,
    record_block,
    run_canary,
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
