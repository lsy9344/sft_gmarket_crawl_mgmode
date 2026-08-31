"""쿠팡 로그인 세션 — 영속 프로필에 1회 수동 로그인을 수행·검증한다 (Qt 비의존).

설계 근거 (2026-08-31 리뷰 결론):
- ID/비밀번호를 자동입력하지 않는다. 사람이 직접 로그인해야 캡차·보안 알림을
  통과할 수 있고, 실제 타이핑 텔레메트리가 Akamai 센서(_abck)에 기록되어
  세션 신뢰가 최상으로 형성된다.
- 로그인 상태 판별은 www.coupang.com 헤더의 로그인 링크(login.coupang.com)
  유무로 한다 — 로그인하면 헤더의 로그인 링크가 사용자 메뉴로 교체된다.
- 반드시 영속 프로필(persistent_context)로만 기동한다. 폴백 세션에 로그인하면
  쿠키가 폐기되므로, 프로필 락 등 기동 실패는 폴백 없이 오류로 끝낸다.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from app.core.base import Control
from app.core.config import DEFAULT_COUPANG_PROFILE_DIR

LOGIN_URL = "https://login.coupang.com/log-in"
# 사람이 로그인을 완료할 시간 — 캡차·2FA 포함 여유분.
DEFAULT_LOGIN_TIMEOUT_SECONDS = 300.0
POLL_INTERVAL_SECONDS = 2.0

# 로그인 상태 판별 JS — www.coupang.com 페이지에서 실행.
# 로그아웃 상태면 헤더에 login.coupang.com 링크가 남는다.
LOGIN_STATE_JS = """
() => !document.querySelector('a[href*="login.coupang.com"]')
"""

WWW_COUPANG_PREFIX = "https://www.coupang.com"


class LoginSessionError(RuntimeError):
    """로그인 세션 기동/실행 실패 (프로필 락, 패키지 미설치 등)."""


def evaluate_login_state(page) -> bool | None:
    """현재 페이지에서 로그인 여부를 판별한다.

    반환: True=로그인, False=비로그인, None=판별 불가(www.coupang.com 밖
    페이지거나 평가 실패). 호출자는 None 을 "대기"로 처리한다.
    """
    url = str(getattr(page, "url", "") or "")
    if not url.startswith(WWW_COUPANG_PREFIX):
        return None
    try:
        return bool(page.evaluate(LOGIN_STATE_JS))
    except Exception:  # noqa: BLE001 - 브라우저 경계(탭 닫힘·이동 중)는 대기로 처리
        return None


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
            page = browser.new_page()
            self._log(
                "쿠팡 로그인 페이지를 열었습니다 — 브라우저에서 직접 로그인하세요."
            )
            self._log("(캡차·보안 알림이 나타나면 직접 통과하면 됩니다)")
            self._log(f"완료를 기다립니다 (최대 {self._timeout_seconds:.0f}초)")
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
            deadline = time.monotonic() + self._timeout_seconds
            while time.monotonic() < deadline:
                self._checkpoint()
                state = evaluate_login_state(page)
                if state is True:
                    self._log("로그인 세션을 확인했습니다 — 영속 프로필에 저장됩니다.")
                    return True
                self._sleep(self._poll_seconds)
            self._log("로그인 대기 시간을 초과했습니다 — 로그인 없이 세션을 닫습니다.")
            return False
        finally:
            self._cleanup(browser, cm)

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


def run_login_session(**kwargs) -> bool:
    """LoginSession 의 편의 래퍼 — 로그인 완료 여부를 반환."""
    return LoginSession(**kwargs).run()
