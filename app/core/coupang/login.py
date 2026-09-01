"""쿠팡 로그인 세션 — 영속 프로필에 1회 수동 로그인을 수행·검증한다 (Qt 비의존).

설계 근거 (2026-08-31 리뷰 결론):
- ID/비밀번호를 자동입력하지 않는다. 사람이 직접 로그인해야 캡차·보안 알림을
  통과할 수 있고, 실제 타이핑 텔레메트리가 Akamai 센서(_abck)에 기록되어
  세션 신뢰가 최상으로 형성된다.
- 홈의 로그인/로그아웃 표시는 후보 신호로만 사용한다. 최종 성공은 로그인한
  사용자만 열 수 있는 마이쿠팡 주문목록이 실제로 열리는지 별도 탭에서 확인한다.
- 반드시 영속 프로필(persistent_context)로만 기동한다. 폴백 세션에 로그인하면
  쿠키가 폐기되므로, 프로필 락 등 기동 실패는 폴백 없이 오류로 끝낸다.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from app.core.base import Control
from app.core.config import DEFAULT_COUPANG_PROFILE_DIR

# 직접 로그인 주소는 쿠팡에서 폐기될 수 있으므로 정상 홈에서 시작한다.
# 사용자는 같은 탭의 헤더에서 로그인하고, 완료 후 홈으로 돌아온다.
LOGIN_URL = "https://www.coupang.com/"
AUTH_CHECK_URL = "https://mc.coupang.com/ssr/desktop/order/list"
# 사람이 로그인을 완료할 시간 — 캡차·2FA 포함 여유분.
DEFAULT_LOGIN_TIMEOUT_SECONDS = 300.0
POLL_INTERVAL_SECONDS = 2.0

# www.coupang.com의 실제 상단 로그인 링크는 semantic header 밖에 있을 수 있다.
# 모든 링크 중 화면에 보이는 로그인/로그아웃 후보만 Python으로 전달한다.
LOGIN_STATE_JS = """
() => {
    if (document.readyState !== 'complete') return null;
    return [...document.querySelectorAll('a')].map((link) => {
        const text = (link.textContent || '').replace(/\\s+/g, '');
        const title = (link.getAttribute('title') || '').replace(/\\s+/g, '');
        const href = link.href || '';
        if (!/(로그인|로그아웃|login|logout)/i.test(`${text} ${title} ${href}`)) {
            return null;
        }
        const style = window.getComputedStyle(link);
        return {
            text,
            title,
            href,
            visible: link.getClientRects().length > 0
                && style.display !== 'none'
                && style.visibility !== 'hidden',
        };
    }).filter(Boolean);
}
"""

WWW_COUPANG_HOST = "www.coupang.com"
LOGIN_COUPANG_HOST = "login.coupang.com"
MY_COUPANG_HOST = "mc.coupang.com"
AUTH_CHECK_PATH_PREFIX = "/ssr/"
BLOCK_MARKERS = (
    "access denied",
    "permission to access",
    "errors.edgesuite.net",
    "비정상적인 접근",
)


class LoginSessionError(RuntimeError):
    """로그인 세션 기동/실행 실패 (프로필 락, 패키지 미설치 등)."""


def _compact(value: object) -> str:
    return "".join(str(value or "").split())


def classify_login_markers(markers: object) -> bool | None:
    """홈 링크 후보를 True=로그인, False=비로그인, None=판별 불가로 분류한다."""
    if not isinstance(markers, list):
        return None
    login_found = False
    logout_found = False
    for marker in markers:
        if not isinstance(marker, dict) or marker.get("visible") is not True:
            continue
        text = _compact(marker.get("text"))
        title = _compact(marker.get("title"))
        href = str(marker.get("href") or "")
        try:
            parsed = urlsplit(href)
        except ValueError:
            continue
        path = parsed.path.lower()
        if (
            parsed.hostname == LOGIN_COUPANG_HOST
            and path in ("/login/login.pang", "/login/sso/login.pang")
            and (text == "로그인" or title == "로그인")
        ):
            login_found = True
        if (
            parsed.hostname in (LOGIN_COUPANG_HOST, WWW_COUPANG_HOST)
            and "logout" in path
            and (text == "로그아웃" or title == "로그아웃")
        ):
            logout_found = True
    if login_found and logout_found:
        return None
    if logout_found:
        return True
    if login_found:
        return False
    return None


def is_authenticated_destination(url: str, status: int | None, html: str) -> bool:
    """마이쿠팡 보호 화면이 로그인 리다이렉트나 차단 없이 열린 경우만 True."""
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return False
    if (
        status is None
        or not 200 <= status < 400
        or parsed.scheme != "https"
        or parsed.hostname != MY_COUPANG_HOST
        or not parsed.path.startswith(AUTH_CHECK_PATH_PREFIX)
    ):
        return False
    lowered = str(html or "").lower()
    return not any(marker in lowered for marker in BLOCK_MARKERS)


def evaluate_login_state(page) -> bool | None:
    """현재 페이지에서 로그인 여부를 판별한다.

    반환: True=로그인, False=비로그인, None=판별 불가(www.coupang.com 밖
    페이지거나 평가 실패). 호출자는 None 을 "대기"로 처리한다.
    """
    url = str(getattr(page, "url", "") or "")
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname != WWW_COUPANG_HOST
        or parsed.path not in ("", "/")
    ):
        return None
    try:
        return classify_login_markers(page.evaluate(LOGIN_STATE_JS))
    except Exception:  # noqa: BLE001 - 브라우저 경계(탭 닫힘·이동 중)는 대기로 처리
        return None


def _is_login_page(page) -> bool:
    try:
        parsed = urlsplit(str(getattr(page, "url", "") or ""))
    except ValueError:
        return False
    return parsed.hostname == LOGIN_COUPANG_HOST and parsed.path.startswith("/login/")


class LoginSession:
    """영속 프로필 브라우저를 띄워 사용자의 수동 로그인을 완료까지 대기한다.

    규율:
    - 로그인은 사용자 몫 — 프로그램은 페이지를 열고 완료를 감지만 한다.
    - 영속 프로필 전용 — 폴백(임시 세션)으로는 로그인을 수행하지 않는다.
    """

    def __init__(
        self,
        control: Control | None = None,
        profile_dir: Path | None = None,
        on_log: Callable[[str], None] | None = None,
        timeout_seconds: float = DEFAULT_LOGIN_TIMEOUT_SECONDS,
        poll_seconds: float = POLL_INTERVAL_SECONDS,
        browser_factory: Callable | None = None,
    ) -> None:
        self._control = control
        self._profile_dir = profile_dir or DEFAULT_COUPANG_PROFILE_DIR
        self._on_log = on_log
        self._timeout_seconds = timeout_seconds
        self._poll_seconds = poll_seconds
        self._browser_factory = browser_factory

    def _log(self, msg: str) -> None:
        if self._on_log is None:
            return
        try:
            self._on_log(msg)
        except Exception:  # noqa: BLE001, S110 - 로그 경계 격리
            pass

    def _sleep(self, seconds: float) -> None:
        if self._control is not None:
            self._control.sleep(seconds)
        else:
            time.sleep(seconds)

    def _checkpoint(self) -> None:
        if self._control is not None:
            self._control.checkpoint()

    def run(self) -> bool:
        """로그인 완료 감지 시 True, 타임아웃/취소 시 False. CancelledError 전파."""
        self._ensure_profile_dir()
        browser, cm = self._create_browser()
        try:
            try:
                initial_pages = list(getattr(browser, "pages", ()) or ())
            except Exception:  # noqa: BLE001 - 브라우저 기동 직후 경계
                initial_pages = []
            # Camoufox는 기동할 때 about:newtab을 하나 만든다. 다시 new_page()를
            # 호출하면 사용자에게 창이 두 개 보여 혼란스럽고 완료 탭도 놓치기 쉽다.
            page = initial_pages[0] if initial_pages else browser.new_page()
            self._log(
                "쿠팡 홈을 열었습니다 — 같은 탭의 상단 로그인에서 직접 로그인하세요."
            )
            self._log("(캡차·보안 알림이 나타나면 직접 통과하면 됩니다)")
            self._log(f"완료를 기다립니다 (최대 {self._timeout_seconds:.0f}초)")
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
            deadline = time.monotonic() + self._timeout_seconds
            saw_login_page = False
            checked_positive_marker = False
            while time.monotonic() < deadline:
                self._checkpoint()
                # 쿠팡 홈 버튼이나 로그인 흐름이 새 탭을 만들 수 있다. 처음 만든
                # page 하나만 보면 정상 로그인도 영원히 대기하므로 모든 탭을 본다.
                try:
                    pages = list(getattr(browser, "pages", ()) or ())
                except Exception:  # noqa: BLE001 - 브라우저가 닫히는 경계
                    pages = []
                if page not in pages:
                    pages.insert(0, page)
                login_page_open = any(_is_login_page(candidate) for candidate in pages)
                if login_page_open:
                    saw_login_page = True
                positive_marker = False
                for candidate in reversed(pages):
                    if evaluate_login_state(candidate) is True:
                        positive_marker = True
                login_flow_finished = saw_login_page and not login_page_open
                should_verify = login_flow_finished or (
                    positive_marker and not checked_positive_marker
                )
                if login_flow_finished:
                    saw_login_page = False
                if positive_marker:
                    checked_positive_marker = True
                else:
                    checked_positive_marker = False
                if should_verify:
                    self._log("마이쿠팡 보호 화면에서 로그인 상태를 최종 확인합니다.")
                    verified = self._verify_authenticated(browser)
                    if verified is True:
                        self._log(
                            "로그인 세션을 확인했습니다 — 영속 프로필에 저장됩니다."
                        )
                        return True
                    if verified is False:
                        self._log(
                            "아직 로그인되지 않았습니다 — 쿠팡 홈에서 다시 로그인하세요."
                        )
                    else:
                        self._log(
                            "로그인 확인 화면을 읽지 못했습니다 — 다시 로그인해주세요."
                        )
                self._sleep(self._poll_seconds)
            self._log("로그인 대기 시간을 초과했습니다 — 로그인 없이 세션을 닫습니다.")
            return False
        finally:
            self._cleanup(browser, cm)

    def _verify_authenticated(self, browser) -> bool | None:
        """새 탭에서 보호 화면을 확인하고 즉시 닫는다."""
        verification_page = None
        try:
            verification_page = browser.new_page()
            response = verification_page.goto(
                AUTH_CHECK_URL, wait_until="domcontentloaded", timeout=45000
            )
            verification_page.wait_for_timeout(1000)
            status = getattr(response, "status", None)
            html = verification_page.content()
            return is_authenticated_destination(verification_page.url, status, html)
        except Exception:  # noqa: BLE001 - 검증 탭 네트워크/종료 경계
            return None
        finally:
            if verification_page is not None:
                try:
                    verification_page.close()
                except Exception:  # noqa: BLE001, S110 - 정리 경계
                    pass

    def _ensure_profile_dir(self) -> None:
        """영속 프로필 디렉터리를 준비한다 — 실패 시 세션을 시작하지 않는다."""
        try:
            self._profile_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise LoginSessionError(f"영속 프로필 디렉터리 생성 실패: {e}") from e

    def _create_browser(self):
        """영속 프로필 브라우저를 반환한다. (browser, cm) — 실패 시 예외."""
        if self._browser_factory is not None:
            try:
                return self._browser_factory(), None
            except Exception as e:
                raise LoginSessionError(f"브라우저 기동 실패: {e}") from e
        try:
            from camoufox.sync_api import Camoufox
        except ImportError as e:
            raise LoginSessionError(
                "Camoufox 패키지가 설치되지 않았습니다. "
                "SellerCollector --setup-runtime 을 먼저 실행하세요."
            ) from e
        try:
            cm = Camoufox(
                headless=False,
                geoip=True,
                locale="ko-KR",
                humanize=True,
                persistent_context=True,
                user_data_dir=str(self._profile_dir),
            )
            browser = cm.__enter__()
        except Exception as e:
            raise LoginSessionError(
                f"영속 프로필 기동 실패: {e}\n"
                "수집 실행 중이면 먼저 종료한 뒤 다시 시도하세요."
            ) from e
        return browser, cm

    def _cleanup(self, browser, cm) -> None:
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
                return
            except Exception as e:  # noqa: BLE001 - cleanup 경계
                self._log(f"브라우저 종료 실패, 재시도: {e}")
        if browser is not None:
            try:
                browser.close()
            except Exception as e:  # noqa: BLE001 - cleanup 경계
                self._log(f"브라우저 종료 실패: {e}")
