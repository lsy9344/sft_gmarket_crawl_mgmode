"""Coupang 카테고리 Decodo 회선 교체 재시도 — 네트워크 없음."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.base import CancelledError, Control
from app.core.coupang.decodo_run import (
    category_run_dir,
    run_category_attempts,
    should_keep_attempt,
    should_retry,
)
from app.core.coupang.search_crawler import SearchRunConfig
from app.core.decodo import DecodoError, DecodoSettings, ExitIpInfo
from app.models.coupang_records import CoupangRunSummary

_KOREA = ExitIpInfo(ip="203.0.113.10", country_code="KR", country_name="South Korea")
_VIETNAM = ExitIpInfo(ip="203.0.113.80", country_code="VN", country_name="Vietnam")


def _config(tmp: str) -> SearchRunConfig:
    return SearchRunConfig(
        output_dir=Path(tmp),
        output_prefix="cat",
        keyword="",
        category_id="194829",
        category_name="수산물",
        warmup_time=0,
        page_delay_min=0,
        page_delay_max=0,
        delay_min=0,
        delay_max=0,
        max_pages=2,
    )


def _settings() -> DecodoSettings:
    return DecodoSettings(username="sp3id", password="pw")


def _summary(**kwargs) -> CoupangRunSummary:
    s = CoupangRunSummary()
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


class _FakeCrawler:
    def __init__(self, summary: CoupangRunSummary, config=None):
        self._summary = summary
        self.config = config

    def run(self):
        return self._summary


class AttemptRuleTest(unittest.TestCase):
    def test_success_needs_products(self):
        self.assertFalse(should_keep_attempt(_summary(termination_reason="success")))
        self.assertTrue(
            should_keep_attempt(_summary(termination_reason="success", products_seen=10))
        )
        self.assertTrue(
            should_keep_attempt(_summary(termination_reason="page_limit", products_seen=10))
        )

    def test_no_retry_on_cancel_or_home_ip_cooldown(self):
        self.assertFalse(should_retry(_summary(cancelled=True, termination_reason="cancelled")))
        self.assertFalse(should_retry(_summary(termination_reason="block_cooldown")))
        self.assertTrue(should_retry(_summary(termination_reason="blocked", products_seen=0)))
        self.assertTrue(should_retry(_summary(termination_reason="error")))


class RunAttemptsTest(unittest.TestCase):
    def test_succeeds_on_first_try(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok = _summary(termination_reason="success", products_seen=5, unique_vendors=2)
            seen: list = []

            def factory(cfg):
                seen.append(cfg)
                return _FakeCrawler(ok, cfg)

            result = run_category_attempts(
                _config(tmp), _settings(), Control(),
                crawler_factory=factory, retry_wait=0,
                exit_ip_fn=lambda _p: _KOREA,
            )
            self.assertEqual(len(seen), 1)
            self.assertEqual(result.products_seen, 5)
            self.assertTrue(seen[0].proxy["username"].startswith("user-sp3id-session-"))
            self.assertEqual(seen[0].subcategories, ())
            self.assertFalse(seen[0].require_login)
            expected_dir = category_run_dir(Path(tmp), "194829", "수산물")
            self.assertEqual(seen[0].output_dir, expected_dir)
            self.assertTrue(str(seen[0].profile_dir).startswith(str(expected_dir)))

    def test_retries_until_success_with_new_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            fail = _summary(termination_reason="blocked", products_seen=0)
            ok = _summary(termination_reason="success", products_seen=3)
            queue = [fail, ok]
            configs = []

            def factory(cfg):
                configs.append(cfg)
                return _FakeCrawler(queue.pop(0), cfg)

            result = run_category_attempts(
                _config(tmp), _settings(), Control(),
                crawler_factory=factory, retry_wait=0,
                exit_ip_fn=lambda _p: _KOREA,
            )
            self.assertEqual(len(configs), 2)
            self.assertEqual(result.products_seen, 3)
            self.assertNotEqual(configs[0].proxy["username"], configs[1].proxy["username"])
            self.assertNotEqual(configs[0].profile_dir, configs[1].profile_dir)

    def test_stops_after_max_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            fail = _summary(termination_reason="error", error="x", products_seen=0)
            n = {"c": 0}

            def factory(cfg):
                n["c"] += 1
                return _FakeCrawler(fail, cfg)

            result = run_category_attempts(
                _config(tmp), _settings(), Control(),
                crawler_factory=factory, max_attempts=3, retry_wait=0,
                exit_ip_fn=lambda _p: _KOREA,
            )
            self.assertEqual(n["c"], 3)
            self.assertEqual(result.termination_reason, "error")

    def test_cancel_during_retry_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            fail = _summary(termination_reason="blocked")

            def factory(cfg):
                return _FakeCrawler(fail, cfg)

            class _CancelControl(Control):
                def sleep(self, seconds, poll_interval=0.1):
                    raise CancelledError()

            result = run_category_attempts(
                _config(tmp), _settings(), _CancelControl(),
                crawler_factory=factory, retry_wait=90,
                exit_ip_fn=lambda _p: _KOREA,
            )
            self.assertTrue(result.cancelled)
            self.assertEqual(result.termination_reason, "cancelled")

    def test_cancel_before_first_attempt_has_no_placeholder_error(self):
        """첫 시도 전 취소 — '실행되지 않았습니다' 자리표시자가 남지 않는다."""
        with tempfile.TemporaryDirectory() as tmp:
            def factory(cfg):
                raise AssertionError("취소 시 크롤러가 시작되면 안 됩니다")

            class _CancelControl(Control):
                def checkpoint(self):
                    raise CancelledError()

            result = run_category_attempts(
                _config(tmp), _settings(), _CancelControl(),
                crawler_factory=factory, retry_wait=0,
                exit_ip_fn=lambda _p: _KOREA,
            )
            self.assertTrue(result.cancelled)
            self.assertEqual(result.termination_reason, "cancelled")
            self.assertIsNone(result.error)

    def test_missing_credentials_does_not_call_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            called = []

            def factory(cfg):
                called.append(cfg)
                return _FakeCrawler(_summary(termination_reason="success", products_seen=1))

            result = run_category_attempts(
                _config(tmp), DecodoSettings(), Control(),
                crawler_factory=factory, retry_wait=0,
            )
            self.assertEqual(called, [])
            self.assertEqual(result.termination_reason, "error")
            self.assertIn("Decodo", result.error or "")

    def test_rotates_until_korea_then_collects(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok = _summary(termination_reason="success", products_seen=4)
            countries = [_VIETNAM, _KOREA]
            seen = []

            def factory(cfg):
                seen.append(cfg)
                return _FakeCrawler(ok, cfg)

            result = run_category_attempts(
                _config(tmp), _settings(), Control(),
                crawler_factory=factory, retry_wait=0,
                exit_ip_fn=lambda _p: countries.pop(0),
            )
            self.assertEqual(len(seen), 1)
            self.assertEqual(result.products_seen, 4)
            self.assertEqual(countries, [])

    def test_non_korea_never_starts_crawler(self):
        with tempfile.TemporaryDirectory() as tmp:
            called = []

            def factory(cfg):
                called.append(cfg)
                return _FakeCrawler(_summary(termination_reason="success", products_seen=1))

            result = run_category_attempts(
                _config(tmp), _settings(), Control(),
                crawler_factory=factory, max_attempts=2, retry_wait=0,
                exit_ip_fn=lambda _p: _VIETNAM,
            )
            self.assertEqual(called, [])
            self.assertEqual(result.termination_reason, "error")
            self.assertIn("한국", result.error or "")

    def test_geo_check_error_rotates_without_crawling(self):
        with tempfile.TemporaryDirectory() as tmp:
            called = []

            def factory(cfg):
                called.append(cfg)
                return _FakeCrawler(_summary(termination_reason="success", products_seen=1))

            def boom(_proxy):
                raise DecodoError("timeout")

            result = run_category_attempts(
                _config(tmp), _settings(), Control(),
                crawler_factory=factory, max_attempts=1, retry_wait=0,
                exit_ip_fn=boom,
            )
            self.assertEqual(called, [])
            self.assertIn("한국", result.error or "")

    def test_category_folders_are_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            dirs = []

            def factory(cfg):
                dirs.append(cfg.output_dir)
                return _FakeCrawler(
                    _summary(termination_reason="success", products_seen=2), cfg
                )

            a = _config(tmp)
            a.category_id = "194829"
            a.category_name = "수산물/건어물"
            b = _config(tmp)
            b.category_id = "225461"
            b.category_name = "냉장냉동"

            run_category_attempts(
                a, _settings(), Control(),
                crawler_factory=factory, retry_wait=0,
                exit_ip_fn=lambda _p: _KOREA,
            )
            run_category_attempts(
                b, _settings(), Control(),
                crawler_factory=factory, retry_wait=0,
                exit_ip_fn=lambda _p: _KOREA,
            )
            self.assertEqual(len(dirs), 2)
            self.assertNotEqual(dirs[0], dirs[1])
            self.assertEqual(dirs[0].parent, Path(tmp))
            self.assertEqual(dirs[1].parent, Path(tmp))
            self.assertIn("194829", dirs[0].name)
            self.assertIn("225461", dirs[1].name)

    def test_keeps_earlier_saved_file_when_last_attempt_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = _summary(
                termination_reason="error",
                products_seen=8,
                unique_vendors=3,
                json_path="/tmp/first.json",
                csv_path="/tmp/first.csv",
            )
            saved.records = [{"vendor_id": "V1"}]
            fail = _summary(termination_reason="blocked", products_seen=0)
            queue = [saved, fail]

            def factory(cfg):
                return _FakeCrawler(queue.pop(0), cfg)

            result = run_category_attempts(
                _config(tmp), _settings(), Control(),
                crawler_factory=factory, max_attempts=2, retry_wait=0,
                exit_ip_fn=lambda _p: _KOREA,
            )
            self.assertEqual(result.termination_reason, "blocked")
            self.assertEqual(result.json_path, "/tmp/first.json")
            self.assertEqual(result.csv_path, "/tmp/first.csv")
            self.assertEqual(result.products_seen, 8)

    def test_page_limit_is_kept_but_logged_as_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            partial = _summary(
                termination_reason="page_limit", products_seen=12, unique_vendors=4,
            )
            logs: list[str] = []

            def factory(cfg):
                return _FakeCrawler(partial, cfg)

            result = run_category_attempts(
                _config(tmp), _settings(), Control(),
                crawler_factory=factory, retry_wait=0,
                on_log=logs.append,
                exit_ip_fn=lambda _p: _KOREA,
            )
            self.assertEqual(result.termination_reason, "page_limit")
            joined = "\n".join(logs)
            self.assertIn("일부 수집", joined)
            self.assertNotIn("시도 1 성공", joined)


class CategoryRunDirTest(unittest.TestCase):
    def test_strips_slash_and_keeps_id(self):
        root = Path("/tmp/out")
        path = category_run_dir(root, "194829", "수산물/건어물")
        self.assertEqual(path.parent, root)
        self.assertIn("194829", path.name)
        self.assertNotIn("/", path.name)
        self.assertNotIn("\\", path.name)
