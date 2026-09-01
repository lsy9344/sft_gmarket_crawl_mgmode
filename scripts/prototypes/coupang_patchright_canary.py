"""PROTOTYPE: Patchright + real Chrome로 쿠팡의 첫 진입만 작게 확인한다."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path

from patchright.sync_api import sync_playwright


HOME_URL = "https://www.coupang.com/"
CATEGORY_URL = "https://www.coupang.com/np/categories/{category_id}?page=1"
MAX_ITEMS = 10
MIN_LIVE_INTERVAL_SECONDS = 30 * 60
BLOCK_STATUSES = {403, 418, 429}
BLOCK_MARKERS = (
    "access denied",
    "permission to access",
    "errors.edgesuite.net",
    "비정상적인 접근",
    "captcha",
)
REFERENCE_RE = re.compile(r"Reference\s*#([\w.-]+)", re.IGNORECASE)

EXTRACT_PRODUCTS_JS = r"""
(limit) => [...document.querySelectorAll('a[href*="/vp/products/"]')]
  .slice(0, limit)
  .map((a) => ({
    href: a.getAttribute('href') || '',
    title: (a.innerText || a.getAttribute('aria-label') || '')
      .replace(/\s+/g, ' ').trim().slice(0, 120),
  }))
"""

OFFLINE_HTML = """
<!doctype html><html lang="ko"><head><title>Patchright offline canary</title></head>
<body><main>
  <a href="/vp/products/1001?itemId=2001&vendorItemId=3001">시험 상품 하나</a>
  <a href="/vp/products/1002?itemId=2002&vendorItemId=3002">시험 상품 둘</a>
</main></body></html>
"""


def _state_dir() -> Path:
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "SellerCollectorPatchrightCanary"
    return Path(__file__).resolve().parents[2] / "runtime_profile" / "patchright_canary"


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _emit(state: dict, event: str, **changes: object) -> None:
    state.update(changes, event=event)
    print(json.dumps(state, sort_keys=True), flush=True)


def _blocked_result(page, response) -> tuple[bool, int | None, str]:
    status = response.status if response else None
    html = page.content()
    lowered = f"{page.url}\n{html}".lower()
    blocked = status in BLOCK_STATUSES or any(text in lowered for text in BLOCK_MARKERS)
    match = REFERENCE_RE.search(html)
    reference = f"Reference #{match.group(1)}" if match else ""
    return blocked, status, reference


def _run(live_category: str | None, limit: int) -> int:
    live = live_category is not None
    state = {
        "mode": "live" if live else "offline",
        "browser": "Patchright + installed Google Chrome",
        "category_id": live_category or "",
        "limit": limit,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    guard_path = _state_dir() / "canary_guard.json"
    guard = _read_json(guard_path)

    if live:
        if guard.get("blocked"):
            _emit(state, "guard_refused", reason="이전에 차단되어 실접속을 잠갔습니다.")
            return 21
        last_attempt = guard.get("last_attempt_ts")
        if isinstance(last_attempt, (int, float)):
            remaining = MIN_LIVE_INTERVAL_SECONDS - (time.time() - last_attempt)
            if remaining > 0:
                minutes = int(remaining // 60) + 1
                _emit(state, "guard_refused", reason=f"다음 실행까지 {minutes}분 남음")
                return 21
        guard = {
            "last_attempt_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "last_attempt_ts": time.time(),
            "blocked": False,
        }
        _write_json(guard_path, guard)

    if live:
        profile_dir = _state_dir() / "chrome_profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_scope = nullcontext(str(profile_dir))
    else:
        profile_scope = tempfile.TemporaryDirectory(prefix="patchright-canary-")

    try:
        with profile_scope as profile, sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=profile,
                channel="chrome",
                headless=not live,
                no_viewport=True,
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                _emit(state, "browser_started")

                if not live:
                    context.set_offline(True)
                    page.set_content(OFFLINE_HTML)
                    products = page.evaluate(EXTRACT_PRODUCTS_JS, limit)
                    _emit(state, "offline_completed", products=products)
                    return 0

                checks = (
                    ("home", HOME_URL, 1_500),
                    ("category", CATEGORY_URL.format(category_id=live_category), 2_000),
                )
                for name, url, settle_ms in checks:
                    response = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                    page.wait_for_timeout(settle_ms)
                    blocked, status, reference = _blocked_result(page, response)
                    _emit(state, f"{name}_loaded", **{f"{name}_status": status})
                    if blocked:
                        guard.update(blocked=True, reference=reference)
                        _write_json(guard_path, guard)
                        _emit(state, "blocked", blocked_at=name, reference=reference)
                        return 20

                products = page.evaluate(EXTRACT_PRODUCTS_JS, limit)
                _emit(state, "live_completed", products=products)
                return 0
            finally:
                context.close()
    except Exception as error:  # prototype boundary: one clear failure record
        _emit(state, "failed", error=f"{type(error).__name__}: {error}")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Patchright 실제 Chrome 소량 시험판")
    parser.add_argument(
        "--live-category",
        metavar="CATEGORY_ID",
        help="명시할 때만 쿠팡 홈 1회와 카테고리 1페이지를 엽니다.",
    )
    parser.add_argument("--limit", type=int, default=5, help="읽을 링크 수(1~10)")
    args = parser.parse_args()
    if args.live_category is not None and not args.live_category.isdigit():
        parser.error("CATEGORY_ID는 숫자여야 합니다.")
    if not 1 <= args.limit <= MAX_ITEMS:
        parser.error(f"--limit는 1~{MAX_ITEMS}여야 합니다.")
    return _run(args.live_category, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
