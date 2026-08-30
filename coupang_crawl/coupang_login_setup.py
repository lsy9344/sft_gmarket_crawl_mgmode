"""
Coupang 로그인 세션 준비 도구 (수동 로그인 — 자동 자격증명 처리 없음)
====================================================================
목적: 영속 프로필 브라우저 창을 열어 **사용자가 직접** 쿠팡에 로그인하면
세션을 프로필에 보관한다. 이후 poc20(로그인 검색 검증)이 동일 프로필을
재사용한다.

안전 설계:
  - 이 스크립트는 로그인 정보를 입력·저장·전송하지 않는다. 사용자가 창에서
    직접 로그인한다 (비밀번호는 스크립트를 거치지 않음).
  - 로그인 여부 판별만 수행 (URL/쿠키/닉네임 요소 관찰).
  - 일회용(서브) 계정 사용을 권고 — 자동화 세션과 연결된 계정 제재 리스크.

사용법:
  xvfb 없이 실행해야 창이 보임 (터미널 환경이면 xvfb 제외):
    python coupang_login_setup.py
"""
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "runtime_profile"
LOGIN_URL = "https://login.coupang.com/login?returnUrl=https%3A%2F%2Fwww.coupang.com%2F"
TIMEOUT_SEC = 600


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    from camoufox.sync_api import Camoufox

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    log(f"영속 프로필: {PROFILE_DIR}")
    log("브라우저 창이 열립니다. 창에서 직접 쿠팡 로그인을 완료해 주세요.")
    log("(권고: 일회용 계정. 로그인 완료 후 자동으로 감지됩니다.)")

    with Camoufox(persistent_context=True, user_data_dir=str(PROFILE_DIR),
                  headless=False, geoip=True, locale="ko-KR",
                  humanize=True) as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)

        start = time.monotonic()
        logged = False
        while time.monotonic() - start < TIMEOUT_SEC:
            time.sleep(10)
            try:
                url = page.url
                cookies = {c["name"] for c in context.cookies()}
                # 로그인 판별: 멤버 관련 쿠키 또는 홈에서 닉네임 요소
                cookie_hit = any(("token" in n.lower()) or n in ("wc", "WCK")
                                 for n in cookies)
                dom_hit = page.evaluate(
                    "() => !!document.querySelector("
                    "'a[href*=\"/np/members\"], .gnb-user, [class*=\"userName\"]')")
                off_login = "login.coupang.com" not in url
                if cookie_hit or dom_hit:
                    logged = True
                    log(f"로그인 감지! (cookie_hit={cookie_hit}, dom_hit={dom_hit})")
                    break
                remain = int(TIMEOUT_SEC - (time.monotonic() - start))
                log(f"로그인 대기 중... (남은 시간 {remain}초, url={url[:70]})")
            except Exception as e:  # noqa: BLE001
                log(f"관측 오류(무시): {e}")

        if logged:
            # 로그인 상태 확정: 홈 방문으로 세션 정착
            try:
                page.goto("https://www.coupang.com/", wait_until="domcontentloaded",
                          timeout=60000)
                time.sleep(5)
                log("홈 방문 완료 — 세션이 프로필에 보관됐습니다.")
                log("다음 단계: coupang_search_poc20.py 실행 (로그인 검색 검증)")
                return 0
            except Exception as e:  # noqa: BLE001
                log(f"홈 방문 실패: {e}")
                return 1
        log("시간 초과 — 로그인 미감지. 다시 실행해 주세요.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
