"""
Coupang 검색 PoC v11 — 사용자가 제공한 실제 page2 URL 재현
================================================================
사용자 브라우저에서 클릭해 만들어진 URL 을 우리 세션에서 그대로 접근:
  q=뷰티&traceId=msv43dlg&channel=user&page=2

목적: 'URL 파라미터가 열쇠인가, 세션이 열쇠인가' 확정.
  - 이 URL 로 상품이 로드되면 → 파라미터(traceId) 문제 → 재현 경로 존재
  - 여전히 빈 셸이면 → 세션 속성(로그인/A-B 버킷) 문제 → URL 재현 불가 확정

순서: 홈 웜업 → 검색창 검색(세션 확립) → 사용자 URL 그대로 접근
"""
import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

COUPANG_HOME = "https://www.coupang.com/"
ts = datetime.now().strftime("%Y%m%d_%H%M%S")

# 사용자가 제공한 실제 page2 URL (traceId 포함 그대로)
USER_PAGE2_URL = "https://www.coupang.com/np/search?q=%EB%B7%B0%ED%8B%B0&traceId=msv43dlg&channel=user&page=2"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def product_ids(page) -> set[str]:
    try:
        ids = set()
        for a in page.query_selector_all('a[href*="/vp/products/"]'):
            m = re.search(r"/vp/products/(\d+)", a.get_attribute("href") or "")
            if m:
                ids.add(m.group(1))
        return ids
    except Exception:
        return set()


def count_cards(html: str) -> int:
    return len(re.findall(r'<li class="ProductUnit_productUnit', html))


def main() -> int:
    keyword = "뷰티"
    log(f"PoC v11 — 사용자 page2 URL 재현")

    report = {"steps": []}

    from camoufox.sync_api import Camoufox
    with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
        page = browser.new_page()

        log("Step 1: 홈 웜업 20초...")
        page.goto(COUPANG_HOME, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        end = time.monotonic() + 20
        while time.monotonic() < end:
            page.mouse.move(random.randint(100, 900), random.randint(100, 600))
            page.mouse.wheel(0, random.randint(50, 250))
            time.sleep(random.uniform(0.5, 1.5))

        log("Step 2: 검색창 타이핑 → Enter (세션 확립)...")
        si = None
        for sel in ('input[name="q"]', '.headerSearchKeyword'):
            for el in page.query_selector_all(sel):
                try:
                    if el.is_visible():
                        si = el
                        break
                except Exception:
                    continue
            if si:
                break
        if not si:
            log("✗ 검색창 없음")
            return 2
        si.click(); time.sleep(0.6)
        si.type(keyword, delay=random.randint(90, 220))
        time.sleep(0.7)
        page.keyboard.press("Enter")
        page.wait_for_load_state("domcontentloaded", timeout=30000)

        base_ids = set()
        for _ in range(12):
            base_ids = product_ids(page)
            if base_ids:
                break
            time.sleep(1.5)
        log(f"  page1: {len(base_ids)}개")
        report["steps"].append({"step": "search_page1", "items": len(base_ids)})
        if not base_ids:
            return 2

        for _ in range(3):
            page.mouse.wheel(0, random.randint(200, 500))
            time.sleep(random.uniform(1.0, 2.0))
        time.sleep(random.uniform(12, 18))

        log(f"Step 3: 사용자 URL 그대로 접근 → {USER_PAGE2_URL}")
        try:
            page.goto(USER_PAGE2_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            log(f"  로드 실패: {type(e).__name__}: {e}")
            report["steps"].append({"step": "user_page2", "error": str(e)})
        else:
            time.sleep(random.uniform(4.0, 6.0))
            ids = set()
            for _ in range(10):
                ids = product_ids(page)
                if ids:
                    break
                time.sleep(1.5)
            html = page.content()
            cards = count_cards(html)
            fresh = ids - base_ids
            blocked = "사용권한" in html or "Access Denied" in html
            log(f"  결과: href {len(ids)}개 (신규 {len(fresh)}), "
                f"메인 카드 {cards}개, HTML {len(html)//1024}KB, 차단={blocked}")
            (OUTPUT_DIR / f"poc11_{ts}_user_page2.html").write_text(html, encoding="utf-8")
            report["steps"].append({
                "step": "user_page2", "items": len(ids), "fresh": len(fresh),
                "cards": cards, "html_kb": len(html)//1024, "blocked": blocked,
                "final_url": page.url[:160],
            })
            if cards > 0 and fresh:
                log("  ✓ page2 상품 로드 성공 — URL 재현 경로 존재!")
            else:
                log("  ✗ 빈 셸 — 세션 속성 문제 확정 (URL 재현 불가)")

    out = OUTPUT_DIR / f"poc11_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"완료 — 리포트 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
