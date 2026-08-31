"""쿠팡 로그인 세션(LoginSession)·상태 판별 테스트 — 네트워크·브라우저 없음."""

import tempfile
import threading
import unittest
from pathlib import Path

from app.core.base import CancelledError, Control
from app.core.coupang.login import (
    LOGIN_URL,
    LoginSession,
    LoginSessionError,
    evaluate_login_state,
)

WWW_HOME = "https://www.coupang.com/"


class FakeLoginPage:
    """로그인 세션용 fake page — evaluate 순서대로 로그인 상태를 반환."""

    def __init__(self, states, redirect_on_goto=False):
        # states: evaluate 호출마다 순서대로 소모, 마지막 값은 반복
        self._states = list(states)
        self._redirect_on_goto = redirect_on_goto
        self.url = "about:blank"
        self.goto_urls = []
        self.closed = False

    def goto(self, url, **kwargs):
        self.url = str(url)
        self.goto_urls.append(str(url))
        # 이미 로그인된 세션: 쿠팡이 로그인 페이지 대신 홈으로 리다이렉트한다
        if self._redirect_on_goto and "login.coupang.com" in self.url:
            self.url = WWW_HOME

    def evaluate(self, script, *args):
        if "login.coupang.com" in script:
            if len(self._states) > 1:
                return self._states.pop(0)
            return self._states[0]
        return None

    def close(self):
        self.closed = True


class FakeLoginBrowser:
    def __init__(self, page):
        self._page = page
        self.closed = False

    def new_page(self):
        return self._page

    def close(self):
        self.closed = True


def _session(page, **kwargs) -> tuple[LoginSession, FakeLoginBrowser]:
    browser = FakeLoginBrowser(page)
    kwargs.setdefault("browser_factory", lambda: browser)
    kwargs.setdefault("timeout_seconds", 1.0)
    kwargs.setdefault("poll_seconds", 0.01)
    kwargs.setdefault("on_log", lambda msg: None)
    return LoginSession(**kwargs), browser


class EvaluateLoginStateTest(unittest.TestCase):
    class _Page:
        def __init__(self, url, result=None, error=False):
            self.url = url
            self._result = result
            self._error = error

        def evaluate(self, script, *args):
            if self._error:
                raise RuntimeError("boom")
            return self._result

    def test_none_off_coupang_pages(self):
        self.assertIsNone(evaluate_login_state(self._Page("about:blank")))
        self.assertIsNone(
            evaluate_login_state(self._Page("https://login.coupang.com/log-in"))
        )

    def test_true_false_and_error_on_www(self):
        www = "https://www.coupang.com/"
        self.assertIs(evaluate_login_state(self._Page(www, True)), True)
        self.assertIs(evaluate_login_state(self._Page(www, False)), False)
        self.assertIsNone(evaluate_login_state(self._Page(www, error=True)))


class LoginSessionRunTest(unittest.TestCase):
    def test_detects_completed_login(self):
        """로그인 페이지 진입 후 사용자가 완료(홈 리다이렉트)하면 True."""
        page = FakeLoginPage([True])
        session, browser = _session(page)
        # 0.05초 뒤 로그인 완료 → 홈 리다이렉트 시뮬레이션
        threading.Timer(0.05, lambda: setattr(page, "url", WWW_HOME)).start()
        self.assertIs(session.run(), True)
        self.assertIn(LOGIN_URL, page.goto_urls)
        self.assertTrue(browser.closed)

    def test_already_logged_in_returns_immediately(self):
        page = FakeLoginPage([True], redirect_on_goto=True)
        session, _ = _session(page)
        self.assertIs(session.run(), True)

    def test_timeout_returns_false(self):
        page = FakeLoginPage([False])
        session, browser = _session(page)
        self.assertIs(session.run(), False)
        self.assertTrue(browser.closed)  # 타임아웃에도 세션 정리

    def test_cancel_raises_and_cleans_up(self):
        page = FakeLoginPage([False])
        control = Control()
        session, browser = _session(page, control=control)
        threading.Timer(0.05, control.request_cancel).start()
        with self.assertRaises(CancelledError):
            session.run()
        self.assertTrue(browser.closed)

    def test_launch_failure_raises_session_error(self):
        def broken_factory():
            raise RuntimeError("profile locked")

        session = LoginSession(browser_factory=broken_factory)
        with self.assertRaises(LoginSessionError):
            session.run()

    def test_profile_dir_created(self):
        page = FakeLoginPage([True])
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "sub" / "profile"
            session, _ = _session(page, profile_dir=profile)
            session.run()
            self.assertTrue(profile.exists())


if __name__ == "__main__":
    unittest.main()
