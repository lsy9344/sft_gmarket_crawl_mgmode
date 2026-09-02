"""PROTOTYPE: Patchright 실제 Chrome의 쿠팡 2페이지 진입 시험."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from pathlib import Path

from app.core.base import CancelledError, Control
from app.core.config import PROJECT_ROOT

HOME_URL = "https://www.coupang.com/"
CATEGORY_URL = "https://www.coupang.com/np/categories/{category_id}?page=1"
MAX_ITEMS = 10
MIN_LIVE_INTERVAL_SECONDS = 2 * 60 * 60
MAX_LIVE_SESSIONS_PER_DAY = 8
MAX_DAILY_ITEMS = 2_460
MAX_SESSION_ITEMS = 600
ROLLING_WINDOW_SECONDS = 24 * 60 * 60
MAX_ROLLING_PAGES = 46
MAX_ROLLING_ITEMS = 2_460
RECOVERY_RAMP_LIMITS = (60, 180, 600)
BLOCK_RECOVERY_INTERVAL_SECONDS = 60 * 60
BLOCK_STATUSES = {403, 418, 429}
BLOCK_MARKERS = (
    "access denied",
    "permission to access",
    "errors.edgesuite.net",
    "접근이 제한",
    "비정상적인 접근",
    "사용권한",
    "captcha",
)
REFERENCE_RE = re.compile(r"Reference\s*#([\w.-]+)", re.IGNORECASE)

EXTRACT_PRODUCTS_JS = r"""
(limit) => {
  const products = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/vp/products/"]')) {
    const href = a.getAttribute('href') || '';
    let key = href;
    try {
      key = new URL(href, location.origin).searchParams.get('vendorItemId') || href;
    } catch (_error) {
      // 잘못된 링크는 href 자체로 중복을 판단한다.
    }
    if (!href || seen.has(key)) continue;
    seen.add(key);
    const text = (a.innerText || a.getAttribute('aria-label') || '')
      .replace(/\s+/g, ' ').trim().slice(0, 120);
    products.push({
      href,
      title: text,
      priceText: text,
    });
    if (products.length >= limit) break;
  }
  return products;
}
"""

OFFLINE_HTML = """
<!doctype html><html lang="ko"><head><title>Patchright offline canary</title></head>
<body><main>
  <a href="/vp/products/1001?itemId=2001&vendorItemId=3001">시험 상품 하나</a>
  <a href="/vp/products/1002?itemId=2002&vendorItemId=3002">시험 상품 둘</a>
</main></body></html>
"""


class CanaryGuardError(RuntimeError):
    """안전 기록을 믿을 수 없어 실접속을 중단해야 한다."""


def state_dir() -> Path:
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "SellerCollectorPatchrightCanary"
    return PROJECT_ROOT / "runtime_profile" / "patchright_canary"


def profile_dir(root: Path | None = None) -> Path:
    return (root or state_dir()) / "chrome_profile"


def _guard_path(root: Path | None = None) -> Path:
    return (root or state_dir()) / "canary_guard.json"


def _read_guard(root: Path | None = None) -> dict:
    path = _guard_path(root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise CanaryGuardError(f"안전 기록을 읽을 수 없습니다: {error}") from error
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise CanaryGuardError("안전 기록이 손상되어 실접속을 중단합니다.") from error
    if not isinstance(value, dict):
        raise CanaryGuardError("안전 기록 형식이 잘못되어 실접속을 중단합니다.")
    return value


def _write_guard(value: dict, root: Path | None = None) -> None:
    path = _guard_path(root)
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise CanaryGuardError(f"안전 기록을 저장할 수 없습니다: {error}") from error


def claim_live_attempt(
    root: Path | None = None,
    *,
    now: float | None = None,
    planned_items: int = 0,
    planned_pages: int = 0,
) -> tuple[bool, str]:
    """실접속 1회를 예약한다. 차단·간격·수량 상한을 어기면 거절한다."""
    if (
        not isinstance(planned_items, int)
        or not 0 <= planned_items <= MAX_SESSION_ITEMS
    ):
        return False, f"세션 예정 상품 수는 0~{MAX_SESSION_ITEMS}개여야 합니다."
    if not isinstance(planned_pages, int) or not 0 <= planned_pages <= 10:
        return False, "세션 예정 페이지 수는 0~10쪽이어야 합니다."
    current = time.time() if now is None else now
    try:
        guard = _read_guard(root)
    except CanaryGuardError as error:
        return False, str(error)
    if guard.get("recovery_hold"):
        return False, "차단 뒤 소량 확인을 마쳐 추가 실접속을 잠갔습니다."
    if guard.get("blocked"):
        return False, "이전 시험에서 차단되어 Patchright 실접속이 잠겼습니다."
    last_attempt = guard.get("last_attempt_ts")
    history = guard.get("block_history")
    daily_date = guard.get("daily_date")
    daily_sessions = guard.get("daily_sessions", 0)
    daily_items_reserved = guard.get("daily_items_reserved", 0)
    attempt_history = guard.get("attempt_history")
    recovery_ramp_limit = guard.get("recovery_ramp_limit")
    if guard and (
        not isinstance(guard.get("blocked"), bool)
        or not isinstance(last_attempt, (int, float))
        or (history is not None and not isinstance(history, list))
        or (
            "recovery_hold" in guard
            and not isinstance(guard.get("recovery_hold"), bool)
        )
        or (daily_date is not None and not isinstance(daily_date, str))
        or not isinstance(daily_sessions, int)
        or not isinstance(daily_items_reserved, int)
        or daily_sessions < 0
        or daily_items_reserved < 0
        or (
            attempt_history is not None
            and (
                not isinstance(attempt_history, list)
                or any(
                    not isinstance(attempt, dict)
                    or not isinstance(attempt.get("attempt_ts"), (int, float))
                    or not isinstance(attempt.get("items"), int)
                    or not 0 <= attempt["items"] <= MAX_ROLLING_ITEMS
                    or not isinstance(attempt.get("pages"), int)
                    or not 0 <= attempt["pages"] <= MAX_ROLLING_PAGES
                    or not isinstance(attempt.get("settled"), bool)
                    for attempt in attempt_history
                )
            )
        )
        or (
            recovery_ramp_limit is not None
            and recovery_ramp_limit not in RECOVERY_RAMP_LIMITS
        )
    ):
        return False, "안전 기록 형식이 잘못되어 실접속을 중단합니다."
    if (
        isinstance(recovery_ramp_limit, int)
        and planned_items > recovery_ramp_limit
    ):
        return (
            False,
            f"복구 확대 단계는 최대 {recovery_ramp_limit}개까지만 허용합니다.",
        )
    if isinstance(last_attempt, (int, float)):
        remaining = MIN_LIVE_INTERVAL_SECONDS - (current - last_attempt)
        if remaining > 0:
            minutes = int(remaining // 60) + 1
            return False, f"다음 Patchright 시험까지 {minutes}분 남았습니다."
    if attempt_history is None:
        attempt_history = []
        if isinstance(last_attempt, (int, float)):
            legacy_items = daily_items_reserved
            legacy_pages = max(
                1,
                min(MAX_ROLLING_PAGES, (legacy_items + 59) // 60),
            )
            attempt_history.append(
                {
                    "attempt_ts": last_attempt,
                    "items": legacy_items,
                    "pages": legacy_pages,
                    "settled": True,
                }
            )
    cutoff = current - ROLLING_WINDOW_SECONDS
    recent_attempts = [
        dict(attempt)
        for attempt in attempt_history
        if attempt["attempt_ts"] > cutoff
    ]
    rolling_items = sum(attempt["items"] for attempt in recent_attempts)
    rolling_pages = sum(attempt["pages"] for attempt in recent_attempts)
    if rolling_items + planned_items > MAX_ROLLING_ITEMS:
        return False, f"최근 24시간 상품 상한 {MAX_ROLLING_ITEMS:,}개를 넘습니다."
    if rolling_pages + planned_pages > MAX_ROLLING_PAGES:
        return False, f"최근 24시간 페이지 상한 {MAX_ROLLING_PAGES}쪽을 넘습니다."
    current_date = time.strftime("%Y-%m-%d", time.localtime(current))
    if daily_date != current_date:
        daily_sessions = 0
        daily_items_reserved = 0
    if daily_sessions >= MAX_LIVE_SESSIONS_PER_DAY:
        return (
            False,
            f"오늘 Patchright 세션 상한 {MAX_LIVE_SESSIONS_PER_DAY}회에 도달했습니다.",
        )
    if daily_items_reserved + planned_items > MAX_DAILY_ITEMS:
        return False, f"오늘 예약 상품 상한 {MAX_DAILY_ITEMS:,}개를 넘습니다."
    try:
        recent_attempts.append(
            {
                "attempt_ts": current,
                "items": planned_items,
                "pages": planned_pages,
                "settled": False,
            }
        )
        next_guard = {
            "last_attempt_at": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(current)
            ),
            "last_attempt_ts": current,
            "blocked": False,
            "daily_date": current_date,
            "daily_sessions": daily_sessions + 1,
            "daily_items_reserved": daily_items_reserved + planned_items,
            "attempt_history": recent_attempts,
        }
        if history is not None:
            next_guard["block_history"] = history
        if recovery_ramp_limit is not None:
            next_guard["recovery_ramp_limit"] = recovery_ramp_limit
        _write_guard(next_guard, root)
    except CanaryGuardError as error:
        return False, str(error)
    return True, ""


def settle_live_attempt(
    planned_items: int,
    actual_items: int,
    root: Path | None = None,
    *,
    planned_pages: int = 0,
    actual_pages: int = 0,
) -> tuple[bool, str]:
    """정상 종료한 세션의 미사용 예약량만 수량 상한에 돌려놓는다."""
    if (
        not isinstance(planned_items, int)
        or not isinstance(actual_items, int)
        or not 0 <= actual_items <= planned_items <= MAX_SESSION_ITEMS
    ):
        return False, "세션 예약량과 실제 처리량이 올바르지 않습니다."
    if (
        not isinstance(planned_pages, int)
        or not isinstance(actual_pages, int)
        or not 0 <= actual_pages <= planned_pages <= 10
    ):
        return False, "세션 예약 페이지와 실제 처리 페이지가 올바르지 않습니다."
    try:
        guard = _read_guard(root)
    except CanaryGuardError as error:
        return False, str(error)
    reserved = guard.get("daily_items_reserved")
    if not isinstance(reserved, int) or reserved < planned_items:
        return False, "일일 예약 상품 기록이 실제 세션과 맞지 않습니다."
    attempt_history = guard.get("attempt_history")
    if not isinstance(attempt_history, list) or not attempt_history:
        return False, "최근 24시간 예약 기록이 실제 세션과 맞지 않습니다."
    attempt = attempt_history[-1]
    if (
        not isinstance(attempt, dict)
        or attempt.get("settled") is not False
        or attempt.get("items") != planned_items
        or attempt.get("pages") != planned_pages
    ):
        return False, "최근 24시간 예약 기록이 실제 세션과 맞지 않습니다."
    guard["daily_items_reserved"] = reserved - (planned_items - actual_items)
    attempt.update(items=actual_items, pages=actual_pages, settled=True)
    _write_guard(guard, root)
    return True, ""


def inspect_block(page, response=None) -> tuple[bool, int | None, str]:
    status = response.status if response else None
    html = page.content()
    text = f"{page.url}\n{html}".lower()
    blocked = status in BLOCK_STATUSES or any(marker in text for marker in BLOCK_MARKERS)
    match = REFERENCE_RE.search(html)
    reference = f"Reference #{match.group(1)}" if match else ""
    return blocked, status, reference


def record_block(
    root: Path | None = None,
    *,
    reference: str = "",
    now: float | None = None,
) -> None:
    current = time.time() if now is None else now
    try:
        guard = _read_guard(root)
    except CanaryGuardError:
        guard = {}
    guard.update(
        blocked=True,
        reference=reference,
        blocked_at=time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(current)
        ),
        blocked_ts=current,
    )
    _write_guard(guard, root)


def authorize_block_recovery(
    root: Path | None = None,
    *,
    now: float | None = None,
) -> tuple[bool, str]:
    """차단 뒤 1시간이 지난 소량 확인 1회만 위해 잠금을 연다."""
    current = time.time() if now is None else now
    try:
        guard = _read_guard(root)
    except CanaryGuardError as error:
        return False, str(error)
    if not guard.get("blocked"):
        return False, "차단된 안전 기록이 없어 복구 확인을 실행하지 않습니다."
    blocked_ts = guard.get("blocked_ts")
    history = guard.get("block_history", [])
    if not isinstance(blocked_ts, (int, float)) or not isinstance(history, list):
        return False, "차단 시각 또는 이력 형식이 잘못되어 잠금을 유지합니다."
    remaining = BLOCK_RECOVERY_INTERVAL_SECONDS - (current - blocked_ts)
    if remaining > 0:
        minutes = int(remaining // 60) + 1
        return False, f"차단 뒤 소량 확인까지 {minutes}분 남았습니다."
    history.append(
        {
            "blocked_at": guard.get("blocked_at", ""),
            "blocked_ts": blocked_ts,
            "reference": guard.get("reference", ""),
            "recovery_authorized_at": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(current)
            ),
            "recovery_authorized_ts": current,
        }
    )
    guard.update(blocked=False, block_history=history)
    _write_guard(guard, root)
    return True, ""


def record_recovery_hold(
    root: Path | None = None,
    *,
    now: float | None = None,
) -> None:
    """차단 뒤 소량 확인을 마친 뒤 추가 실접속을 다시 잠근다."""
    current = time.time() if now is None else now
    guard = _read_guard(root)
    guard.update(
        recovery_hold=True,
        recovery_hold_at=time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(current)
        ),
        recovery_hold_ts=current,
    )
    _write_guard(guard, root)


def authorize_recovery_resume(
    root: Path | None = None,
    *,
    now: float | None = None,
) -> tuple[bool, str]:
    """복구 확인 다음 날부터 1→3→10페이지 확대 검증을 시작한다."""
    current = time.time() if now is None else now
    try:
        guard = _read_guard(root)
    except CanaryGuardError as error:
        return False, str(error)
    if guard.get("blocked"):
        return False, "차단 기록이 남아 있어 복구 확대를 시작하지 않습니다."
    if not guard.get("recovery_hold"):
        return False, "복구 확인 뒤 대기 기록이 없어 확대를 시작하지 않습니다."
    hold_ts = guard.get("recovery_hold_ts")
    last_attempt_ts = guard.get("last_attempt_ts")
    if not isinstance(hold_ts, (int, float)) or not isinstance(
        last_attempt_ts, (int, float)
    ):
        return False, "복구 대기 시각 형식이 잘못되어 잠금을 유지합니다."
    hold_date = time.strftime("%Y-%m-%d", time.localtime(hold_ts))
    current_date = time.strftime("%Y-%m-%d", time.localtime(current))
    if current_date <= hold_date:
        return False, "복구 확인 다음 날이 되기 전에는 확대하지 않습니다."
    remaining = MIN_LIVE_INTERVAL_SECONDS - (current - last_attempt_ts)
    if remaining > 0:
        minutes = int(remaining // 60) + 1
        return False, f"복구 확대 시작까지 {minutes}분 남았습니다."
    guard.update(
        recovery_hold=False,
        recovery_ramp_limit=RECOVERY_RAMP_LIMITS[0],
        recovery_resume_at=time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(current)
        ),
        recovery_resume_ts=current,
    )
    _write_guard(guard, root)
    return True, ""


def advance_recovery_ramp(
    completed_limit: int,
    root: Path | None = None,
    *,
    now: float | None = None,
) -> tuple[bool, str]:
    """성공한 복구 검증을 기록하고 다음 허용량으로 한 단계만 올린다."""
    current = time.time() if now is None else now
    try:
        guard = _read_guard(root)
    except CanaryGuardError as error:
        return False, str(error)
    ramp_limit = guard.get("recovery_ramp_limit")
    if ramp_limit != completed_limit or ramp_limit not in RECOVERY_RAMP_LIMITS:
        return False, "현재 복구 확대 단계와 완료한 단계가 맞지 않습니다."
    index = RECOVERY_RAMP_LIMITS.index(ramp_limit)
    guard["recovery_ramp_completed_at"] = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.localtime(current)
    )
    guard["recovery_ramp_completed_limit"] = ramp_limit
    if index + 1 < len(RECOVERY_RAMP_LIMITS):
        guard["recovery_ramp_limit"] = RECOVERY_RAMP_LIMITS[index + 1]
    else:
        guard.pop("recovery_ramp_limit", None)
        guard["recovery_ramp_completed"] = True
    _write_guard(guard, root)
    return True, ""


@contextmanager
def patchright_browser(user_data_dir: Path, *, headless: bool = False):
    """설치된 Google Chrome을 Patchright 영속 컨텍스트로 연다."""
    from patchright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            channel="chrome",
            headless=headless,
            no_viewport=True,
        )
        try:
            yield context
        finally:
            context.close()


def _emit(
    state: dict,
    event: str,
    on_event: Callable[[dict], None] | None,
    **changes: object,
) -> dict:
    state.update(changes, event=event)
    snapshot = dict(state)
    if on_event is not None:
        on_event(snapshot)
    return snapshot


def run_canary(
    *,
    live_category: str | None = None,
    limit: int = 5,
    control: Control | None = None,
    on_event: Callable[[dict], None] | None = None,
    state_root: Path | None = None,
    browser_scope_factory: Callable | None = None,
) -> dict:
    """오프라인 점검 또는 홈+카테고리 2페이지 실접속을 실행한다."""
    if live_category is not None and not live_category.isdigit():
        raise ValueError("CATEGORY_ID는 숫자여야 합니다.")
    if not 1 <= limit <= MAX_ITEMS:
        raise ValueError(f"limit는 1~{MAX_ITEMS}여야 합니다.")

    live = live_category is not None
    state = {
        "mode": "live" if live else "offline",
        "browser": "Patchright + installed Google Chrome",
        "category_id": live_category or "",
        "limit": limit,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if live:
        allowed, reason = claim_live_attempt(
            state_root, planned_items=limit, planned_pages=1
        )
        if not allowed:
            return _emit(state, "guard_refused", on_event, reason=reason)

    factory = browser_scope_factory or patchright_browser
    try:
        with ExitStack() as stack:
            if live:
                user_data_dir = profile_dir(state_root)
                user_data_dir.mkdir(parents=True, exist_ok=True)
            else:
                temporary = stack.enter_context(
                    tempfile.TemporaryDirectory(prefix="patchright-canary-")
                )
                user_data_dir = Path(temporary)
            context = stack.enter_context(
                factory(user_data_dir, headless=not live)
            )
            page = context.pages[0] if context.pages else context.new_page()
            _emit(state, "browser_started", on_event)

            if not live:
                context.set_offline(True)
                page.set_content(OFFLINE_HTML)
                products = page.evaluate(EXTRACT_PRODUCTS_JS, limit)
                return _emit(state, "offline_completed", on_event, products=products)

            checks = (
                ("home", HOME_URL, 1_500),
                ("category", CATEGORY_URL.format(category_id=live_category), 2_000),
            )
            for name, url, settle_ms in checks:
                if control is not None:
                    control.checkpoint()
                response = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(settle_ms)
                if control is not None:
                    control.checkpoint()
                blocked, status, reference = inspect_block(page, response)
                if blocked:
                    record_block(state_root, reference=reference)
                    return _emit(
                        state,
                        "blocked",
                        on_event,
                        blocked_at=name,
                        reference=reference,
                        **{f"{name}_status": status, f"{name}_url": page.url},
                    )
                _emit(
                    state,
                    f"{name}_loaded",
                    on_event,
                    **{f"{name}_status": status, f"{name}_url": page.url},
                )

            if control is not None:
                control.checkpoint()
            products = page.evaluate(EXTRACT_PRODUCTS_JS, limit)
            if not products:
                return _emit(
                    state, "failed", on_event, error="목록에서 상품 링크를 찾지 못했습니다."
                )
            return _emit(state, "live_completed", on_event, products=products)
    except CancelledError:
        return _emit(state, "cancelled", on_event)
    except Exception as error:  # 브라우저 경계의 실패를 한 결과로 모은다
        return _emit(
            state, "failed", on_event, error=f"{type(error).__name__}: {error}"
        )
