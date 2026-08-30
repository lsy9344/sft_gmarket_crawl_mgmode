"""
Coupang 검색 PoC v2 — 무한 스크롤 + API 캡처 방식
====================================================
PoC v1 실측 결과: /np/search?q=...&page=2+ 직접 URL 접근은 빈 껍데기(128KB)만
반환. 페이지 1만 서버 렌더링됨.
가설: 신형 검색은 (a) 검색창 입력 → Enter 경로만 인정하고,
      (b) 추가 상품은 무한 스크롤/백그라운드 API 로 로드.

본 스크립트:
  1. 홈 웜업
  2. 검색창에 키워드 타이핑 → Enter (실사용자 경로)
  3. 결과 로드 후 상품 수 카운트
  4. 바닥까지 반복 스크롤하며 상품 수 증가 관찰 + 네트워크 요청 캡처
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
PRODUCT_LINK_RE = re.compile(r"/vp/products/(\d+)")
ts = datetime.now().strftime("%Y%m%d_%H%M%S")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def count_products(page) -> int:
    try:
        return len({
            (a.get_attribute("href") or "").split("?")[0]
            for a in page.query_selector_all('a[href*="/vp/products/"]')
        })
    except Exception:
        return 0


def main() -> int:
    keyword = sys.argv[1] if len(sys.argv) > 1 else "에어프라이어"
    max_scrolls = int(sys.argv[2]) if len(sys.argv) > 2 else 15

    log(f"Coupang 검색 PoC v2 — keyword='{keyword}', max_scrolls={max_scrolls}")

    captured_requests = []

    from camoufox.sync_api import Camoufox
    with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
        page = browser.new_page()

        def on_request(req):
            url = req.url
            if any(k in url.lower() for k in ("search", "api", "recommend")) \
                    and "coupang" in url \
                    and not any(x in url for x in (".png", ".jpg", ".webp", ".css", ".js", ".woff")):
                captured_requests.append({
                    "method": req.method,
                    "url": url[:200],
                    "post_data": (req.post_data or "")[:300] if req.method == "POST" else None,
                })
        page.on("request", on_request)

        # Phase 1: 홈 웜업
        log("Phase 1: 홈 웜업...")
        page.goto(COUPANG_HOME, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        end = time.monotonic() + 20
        while time.monotonic() < end:
            page.mouse.move(random.randint(100, 900), random.randint(100, 600))
            page.mouse.wheel(0, random.randint(50, 200))
            time.sleep(random.uniform(0.5, 1.5))

        # Phase 2: 검색창 타이핑 → Enter
        log("Phase 2: 검색창에 키워드 타이핑 후 Enter...")
        search_input = None
        for sel in ['input[name="q"]', '.headerSearchKeyword', '#headerSearchKeyword']:
            search_input = page.query_selector(sel)
            if search_input:
                break
        if not search_input:
            log("✗ 검색창을 찾지 못함")
            return 2
        search_input.click()
        time.sleep(random.uniform(0.5, 1.0))
        search_input.type(keyword, delay=random.randint(90, 220))
        time.sleep(random.uniform(0.5, 1.2))
        page.keyboard.press("Enter")
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        time.sleep(random.uniform(3.0, 5.0))

        initial = count_products(page)
        log(f"  검색 결과 초기 로드: {initial}개")
        if initial == 0:
            html = page.content()
            (OUTPUT_DIR / f"search_poc2_{ts}_empty.html").write_text(html, encoding="utf-8")
            log(f"✗ 상품 0개 — HTML 저장 ({len(html)//1024}KB): search_poc2_{ts}_empty.html")
            return 2

        # Phase 3: 스크롤하며 추가 로드 관찰
        log("Phase 3: 바닥 스크롤로 추가 로드 관찰...")
        counts = [initial]
        stale_rounds = 0
        for i in range(1, max_scrolls + 1):
            page.keyboard.press("End")
            time.sleep(random.uniform(0.3, 0.6))
            page.mouse.wheel(0, random.randint(800, 1500))
            time.sleep(random.uniform(2.5, 4.5))
            n = count_products(page)
            counts.append(n)
            log(f"  스크롤 {i:2d}: 상품 {n}개 (누적 요청 {len(captured_requests)}개)")
            if n == counts[-2]:
                stale_rounds += 1
            else:
                stale_rounds = 0
            if stale_rounds >= 4:
                log("  4회 연속 증가 없음 — 종료")
                break
            time.sleep(random.uniform(1.0, 3.0))

        html = page.content()
        (OUTPUT_DIR / f"search_poc2_{ts}_final.html").write_text(html, encoding="utf-8")

    # 저장
    report = {
        "keyword": keyword,
        "initial_items": initial,
        "final_items": counts[-1] if counts else 0,
        "counts_per_scroll": counts,
        "captured_requests": captured_requests[-60:],
        "total_requests_captured": len(captured_requests),
    }
    out = OUTPUT_DIR / f"search_poc2_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    log("=" * 60)
    log(f"결과: 초기 {initial}개 → 최종 {counts[-1] if counts else 0}개 "
        f"(스크롤 {len(counts)-1}회)")
    api_reqs = [r for r in captured_requests if "api" in r["url"].lower()]
    log(f"캡처된 요청 {len(captured_requests)}개 중 API성 {len(api_reqs)}개")
    for r in api_reqs[-10:]:
        log(f"  {r['method']} {r['url']}")
    log(f"리포트: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
