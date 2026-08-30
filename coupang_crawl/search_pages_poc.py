"""Coupang 검색 페이지네이션 재검증 POC (읽기 전용·최소 요청)

기존 POC(2026-08-13)에서 ?page=2+ 직접 로드가 "검색결과 없음"을 반환해
수집 불가로 결론 났다. 이 스크립트는 다음을 판별한다:

  1) 검색 결과가 실제로 무한 스크롤로만 로드되는지 (스크롤 시 증가 여부)
  2) 스크롤 시 페이지가 스스로 호출하는 추가 로드 API가 무엇인지 (캡처)
  3) 캡처된 API를 page=2 형태로 리플레이하면 2페이지 데이터가 나오는지

차단 리스크 최소화: Camoufox headed + geoip + humanize (기존 OMP 크롤러와
동일 설정), 홈페이지 웜업, 사람 속도 스크롤, 요청 수 소량 제한.
"""
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path

from camoufox.sync_api import Camoufox

KEYWORD = "에어프라이어"
SEARCH_URL = f"https://www.coupang.com/np/search?q={KEYWORD}"
OUTPUT_DIR = Path(__file__).resolve().parent / "output"
MAX_SCROLLS = 6          # 스크롤 횟수 상한 (요청 최소화)
STABLE_STOP = 2          # 연속 N회 신규 0건이면 스크롤 중단

COUNT_JS = """
() => document.querySelectorAll('#product-list > li, #product-list .search-product-wrap, ul#product-list li').length
"""


def main() -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log: dict = {
        "started_at": ts,
        "keyword": KEYWORD,
        "warmup": "home 15s natural interaction",
        "phases": [],
        "requests": [],
        "scrolls": [],
    }

    def on_request(req):
        if any(k in req.url for k in ("n-api", "/api/", "search", "reco.")):
            log["requests"].append({
                "t": round(time.time(), 2),
                "phase": log.get("_phase", "?"),
                "method": req.method,
                "url": req.url[:400],
                "post": (req.post_data or "")[:500] or None,
            })

    def on_response(res):
        for r in reversed(log["requests"]):
            if res.url.startswith(r["url"][:200]) and "status" not in r:
                r["status"] = res.status
                break

    with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
        page = browser.new_page()
        page.on("request", on_request)
        page.on("response", on_response)

        # ---- Phase 0: warmup on home (Akamai _abck) ----
        log["_phase"] = "warmup"
        page.goto("https://www.coupang.com", timeout=60000)
        for _ in range(4):
            page.mouse.wheel(0, random.randint(300, 700))
            time.sleep(random.uniform(1.5, 3.0))
        page.wait_for_timeout(3000)

        # ---- Phase 1: search page load ----
        log["_phase"] = "search_load"
        t0 = time.time()
        page.goto(SEARCH_URL, timeout=60000)
        page.wait_for_timeout(4000)
        cnt0 = page.evaluate(COUNT_JS)
        log["phases"].append({
            "phase": "search_load",
            "items": cnt0,
            "elapsed": round(time.time() - t0, 1),
            "final_url": page.url,
        })
        print(f"[load] items={cnt0} url={page.url}")

        # ---- Phase 2: scroll & watch network ----
        log["_phase"] = "scroll"
        stable = 0
        prev = cnt0
        for i in range(1, MAX_SCROLLS + 1):
            time.sleep(random.uniform(2.5, 4.5))
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(random.randint(2500, 4000))
            cnt = page.evaluate(COUNT_JS)
            new_reqs = [r for r in log["requests"] if r["phase"] == "scroll" and "status" in r][-6:]
            log["scrolls"].append({
                "scroll": i,
                "items": cnt,
                "delta": cnt - prev,
                "recent_api": [f"{r['method']} {r['status']} {r['url'][:120]}" for r in new_reqs],
            })
            print(f"[scroll {i}] items={cnt} (+{cnt - prev})")
            if cnt == prev:
                stable += 1
                if stable >= STABLE_STOP:
                    print("stable -> stop scrolling")
                    break
            else:
                stable = 0
            prev = cnt

        # ---- Phase 3: capture scroll-triggered API template (if any) ----
        # 스크롤 중 발견된 n-api 검색계 엔드포인트를 모아 저장 (리플레이는 다음 단계 판단 후)
        api_candidates = [
            r for r in log["requests"]
            if "n-api" in r["url"] and ("search" in r["url"].lower() or r.get("post"))
        ]
        log["api_candidates"] = api_candidates[-20:]

        # final state dump
        log["final_items"] = page.evaluate(COUNT_JS)
        html = page.content()
        html_path = OUTPUT_DIR / f"search_pages_poc_{ts}_final.html"
        html_path.write_text(html, encoding="utf-8")
        log["final_html_kb"] = round(len(html) / 1024)

    del log["_phase"]
    out = OUTPUT_DIR / f"search_pages_poc_{ts}.json"
    out.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"saved: {out}")
    print(f"final items: {log['final_items']}")


if __name__ == "__main__":
    main()
