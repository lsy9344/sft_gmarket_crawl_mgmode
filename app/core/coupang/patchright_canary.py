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
MIN_LIVE_INTERVAL_SECONDS = 30 * 60
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
    root: Path | None = None, *, now: float | None = None
) -> tuple[bool, str]:
    """실접속 1회를 예약한다. 차단 기록·30분 간격을 어기면 거절한다."""
    current = time.time() if now is None else now
    try:
        guard = _read_guard(root)
    except CanaryGuardError as error:
        return False, str(error)
    if guard.get("blocked"):
        return False, "이전 시험에서 차단되어 Patchright 실접속이 잠겼습니다."
    last_attempt = guard.get("last_attempt_ts")
    history = guard.get("block_history")
    if guard and (
        not isinstance(guard.get("blocked"), bool)
        or not isinstance(last_attempt, (int, float))
        or (history is not None and not isinstance(history, list))
    ):
        return False, "안전 기록 형식이 잘못되어 실접속을 중단합니다."
    if isinstance(last_attempt, (int, float)):
        remaining = MIN_LIVE_INTERVAL_SECONDS - (current - last_attempt)
        if remaining > 0:
            minutes = int(remaining // 60) + 1
            return False, f"다음 Patchright 시험까지 {minutes}분 남았습니다."
    try:
        next_guard = {
            "last_attempt_at": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(current)
            ),
            "last_attempt_ts": current,
            "blocked": False,
        }
        if history is not None:
            next_guard["block_history"] = history
        _write_guard(next_guard, root)
    except CanaryGuardError as error:
        return False, str(error)
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
        allowed, reason = claim_live_attempt(state_root)
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
