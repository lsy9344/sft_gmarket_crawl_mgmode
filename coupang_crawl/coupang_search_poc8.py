"""
Coupang 검색 PoC v8 — 연관 검색어 추출 (최소 세션)
====================================================
목적: 키워드 검색 결과 사이드바의 연관 검색어 링크를 수집해
확장 후보 키워드 목록을 확보한다. (클릭 없음 — 검색 1회만 수행)
"""
import json
import random
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


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    keyword = sys.argv[1] if len(sys.argv) > 1 else "뷰티"
    log(f"PoC v8 — '{keyword}' 연관 검색어 추출 (최소 세션)")

    from camoufox.sync_api import Camoufox
    with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
        page = browser.new_page()

        log("웜업 15초...")
        page.goto(COUPANG_HOME, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        end = time.monotonic() + 15
        while time.monotonic() < end:
            page.mouse.move(random.randint(100, 900), random.randint(100, 600))
            page.mouse.wheel(0, random.randint(50, 250))
            time.sleep(random.uniform(0.5, 1.5))

        log("검색창 경로로 검색...")
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
        time.sleep(random.uniform(4.0, 6.0))

        # 사이드바 천천히 훑기 (렌더 유도)
        page.keyboard.press("Home")
        time.sleep(1)
        for _ in range(5):
            page.mouse.wheel(0, random.randint(150, 300))
            time.sleep(random.uniform(0.4, 0.8))

        related = []
        for a in page.query_selector_all('a[href*="/np/search?q="]'):
            try:
                txt = (a.inner_text() or "").strip()
                href = a.get_attribute("href") or ""
                if txt and len(txt) < 40 and "channel=" in href:
                    related.append({"keyword": txt, "href": href[:160]})
            except Exception:
                continue
        seen = set()
        deduped = []
        for r in related:
            if r["keyword"] not in seen:
                seen.add(r["keyword"])
                deduped.append(r)

        log(f"연관 검색어 {len(deduped)}개:")
        for r in deduped:
            log(f"  - {r['keyword']}")

    out = OUTPUT_DIR / f"poc8_{ts}.json"
    out.write_text(json.dumps({"keyword": keyword, "related": deduped},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"저장: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
