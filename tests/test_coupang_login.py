"""쿠팡 로그인 세션(LoginSession)·상태 판별 테스트 — 네트워크·브라우저 없음."""

import tempfile
import threading
import unittest
from pathlib import Path

from app.core.base import CancelledError, Control
from app.core.coupang.login import (
    AUTH_CHECK_URL,
    LOGIN_URL,
    LoginSession,
    LoginSessionError,
    classify_login_markers,
    evaluate_login_state,
    is_authenticated_destination,
)

WWW_HOME = "https://www.coupang.com/"
LOGIN_PAGE = "https://login.coupang.com/login/login.pang?rtnUrl=home"
LOGIN_MARKERS = [
    {
        "text": "로그인",
        "title": "로그인",
        "href": LOGIN_PAGE,
        "visible": True,
    }
]
LOGOUT_MARKERS = [
    {
        "text": "로그아웃",
        "title": "로그아웃",
        "href": "https://login.coupang.com/login/logout.pang",
        "visible": True,
    }
]


def _marker_result(state):
    if state is True:
        return LOGOUT_MARKERS
    if state is False:
        return LOGIN_MARKERS
    return state


class LoginMarkerClassificationTest(unittest.TestCase):
    def test_public_home_login_link_is_logged_out(self):
        """2026-09-01 일반 Chrome에서 확인한 공개 홈 로그인 링크."""
        markers = [
            {
                "text": "로그인",
                "title": "로그인",
                "href": (
                    "https://login.coupang.com/login/login.pang"
                    "?rtnUrl=https%3A%2F%2Fwww.coupang.com%2F"
                ),
                "visible": True,
            }
        ]
        self.assertIs(classify_login_markers(markers), False)

    def test_missing_login_and_logout_markers_is_unknown(self):
        self.assertIsNone(classify_login_markers([]))

    def test_hidden_promotional_login_link_is_not_header_state(self):
        markers = [
            {
                "text": "",
                "title": "",
                "href": "https://login.coupang.com/login/login.pang?rtnUrl=x",
                "visible": False,
            }
        ]
        self.assertIsNone(classify_login_markers(markers))

    def test_visible_logout_marker_is_logged_in_candidate(self):
        markers = [
            {
                "text": "로그아웃",
                "title": "로그아웃",
                "href": "https://login.coupang.com/login/logout.pang",
                "visible": True,
            }
        ]
        self.assertIs(classify_login_markers(markers), True)


class AuthenticatedDestinationTest(unittest.TestCase):
    def test_protected_order_page_is_authenticated(self):
        self.assertTrue(
            is_authenticated_destination(AUTH_CHECK_URL, 200, "<html>주문목록</html>")
        )

    def test_logged_out_redirect_to_login_is_not_authenticated(self):
        self.assertFalse(
            is_authenticated_destination(
                "https://login.coupang.com/login/login.pang?rtnUrl=orders",
                200,
                "<html>로그인</html>",
            )
        )

    def test_access_denied_is_not_authenticated(self):
        self.assertFalse(
            is_authenticated_destination(
                AUTH_CHECK_URL,
                403,
                "<html>Access Denied</html>",
            )
        )


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
        if "getClientRects" in script:
            if len(self._states) > 1:
                return _marker_result(self._states.pop(0))
            return _marker_result(self._states[0])
        return None

    def close(self):
        self.closed = True


class FakeResponse:
    def __init__(self, status=200):
        self.status = status


class FakeVerificationPage:
    def __init__(self, final_url=AUTH_CHECK_URL, status=200, html="<html>주문목록</html>"):
        self._final_url = final_url
        self._status = status
        self._html = html
        self.url = "about:blank"
        self.goto_urls = []
        self.closed = False

    def goto(self, url, **kwargs):
        self.goto_urls.append(str(url))
        self.url = self._final_url
        return FakeResponse(self._status)

    def wait_for_timeout(self, milliseconds):
        return None

    def content(self):
        return self._html

    def close(self):
        self.closed = True


class FakeLoginBrowser:
    def __init__(self, page, verification_url=AUTH_CHECK_URL):
        self._page = page
        self.pages = [page]
        self.verification_page = FakeVerificationPage(final_url=verification_url)
        self.new_page_calls = 0
        self.closed = False

    def new_page(self):
        self.new_page_calls += 1
        return self.verification_page

    def close(self):
        self.closed = True


def _session(
    page, *, verification_url=AUTH_CHECK_URL, **kwargs
) -> tuple[LoginSession, FakeLoginBrowser]:
    browser = FakeLoginBrowser(page, verification_url=verification_url)
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
            return _marker_result(self._result)

    def test_none_off_coupang_pages(self):
        self.assertIsNone(evaluate_login_state(self._Page("about:blank")))
        self.assertIsNone(
            evaluate_login_state(self._Page("https://login.coupang.com/log-in"))
        )

    def test_true_false_and_error_on_www(self):
        www = "https://www.coupang.com/"
        self.assertIs(evaluate_login_state(self._Page(www, True)), True)
        self.assertIs(evaluate_login_state(self._Page(www, False)), False)
        self.assertIsNone(evaluate_login_state(self._Page(www, None)))
        self.assertIsNone(evaluate_login_state(self._Page(www, error=True)))

    def test_none_outside_exact_home(self):
        self.assertIsNone(
            evaluate_login_state(
                self._Page("https://www.coupang.com/np/categories/123", True)
            )
        )
        self.assertIsNone(
            evaluate_login_state(self._Page("https://www.coupang.com.evil/", True))
        )


class LoginSessionRunTest(unittest.TestCase):
    def test_starts_from_coupang_home(self):
        """폐기된 직접 로그인 주소 대신 정상 홈에서 로그인을 시작한다."""
        self.assertEqual(LOGIN_URL, WWW_HOME)

    def test_reuses_browser_initial_page(self):
        """Camoufox 기본 newtab은 홈에 쓰고 새 탭은 최종 검증에만 쓴다."""
        page = FakeLoginPage([True])
        session, browser = _session(page)
        self.assertIs(session.run(), True)
        self.assertEqual(browser.new_page_calls, 1)
        self.assertEqual(browser.verification_page.goto_urls, [AUTH_CHECK_URL])

    def test_detects_completed_login(self):
        """로그인 페이지 진입 후 사용자가 완료(홈 리다이렉트)하면 True."""
        page = FakeLoginPage([False])
        session, browser = _session(page)
        threading.Timer(0.02, lambda: setattr(page, "url", LOGIN_PAGE)).start()
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

    def test_protected_page_redirect_rejects_false_positive(self):
        page = FakeLoginPage([True])
        session, browser = _session(
            page,
            verification_url=LOGIN_PAGE,
            timeout_seconds=0.05,
            poll_seconds=0.01,
        )
        self.assertIs(session.run(), False)
        self.assertTrue(browser.verification_page.closed)

    def test_non_home_redirect_does_not_complete_login(self):
        page = FakeLoginPage([False, True])
        session, browser = _session(page, timeout_seconds=0.1, poll_seconds=0.05)
        threading.Timer(
            0.001,
            lambda: setattr(page, "url", "https://www.coupang.com/np/categories/123"),
        ).start()
        self.assertIs(session.run(), False)
        self.assertTrue(browser.closed)

    def test_detects_login_completed_in_new_tab(self):
        """홈 버튼이 새 탭을 열어도 로그인 완료를 놓치지 않는다."""
        original = FakeLoginPage([False])
        session, browser = _session(original)
        authenticated = FakeLoginPage([True])
        authenticated.url = WWW_HOME
        threading.Timer(0.05, lambda: browser.pages.append(authenticated)).start()
        self.assertIs(session.run(), True)
        self.assertTrue(browser.closed)

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
