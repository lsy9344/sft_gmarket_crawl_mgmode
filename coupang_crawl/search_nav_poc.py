"""검색 페이지네이션 규명 POC 5 — Next.js 클라이언트 내비게이션 에뮬레이션

가설: 새 SRP는 Next App Router 기반이라 페이지 이동이 앵커 클릭 → RSC fetch로
일어난다. ?page=2 를 직접 로드하면 "결과 없음"이지만, 페이지 '내부'에서
링크 클릭 내비게이션을 하면 2페이지가 렌더될 수 있다.

방법: 검색 페이지 로드 후 <a href="/np/search?q=KW&page=2"> 를 DOM에 넣고
실제 클릭 → Next가 가로채서 RSC 요청을 보냄. 그 요청의 URL/헤더/응답을
캡처하고 렌더된 상품 수를 측정. 요청 최소화(세션 1개).
"""
import json
import random
import time
from datetime import datetime
from pathlib import Path

from camoufox.sync_api import Camoufox

KEYWORD = "에어프라이어"
OUTPUT_DIR = Path(__file__).resolve().parent / "output"

COUNT_JS = "() => document.querySelectorAll('#product-list > li').length"

CLICK_NAV_JS = """
(pageNum) => {
  const a = document.createElement('a');
  a.href = '/np/search?q=""" + KEYWORD + """&page=' + pageNum;
  a.id = 'poc-nav-' + pageNum;
  document.body.appendChild(a);
  a.click();
  return 'clicked';
}
"""


def main() -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log: dict = {"started_at": ts, "navigations": [], "rsc_requests": []}

    def on_request(req):
        if "_rsc" in req.url or "np/search" in req.url:
            entry = {
                "t": round(time.time(), 2),
                "method": req.method,
                "url": req.url,
                "headers": {k: v for k, v in req.headers.items()
                            if k.lower().startswith(("rsc", "next-", "accept", "referer"))},
            }
            log["rsc_requests"].append(entry)

    def on_response(res):
        for r in reversed(log["rsc_requests"]):
            if res.url == r["url"] and "status" not in r:
                r["status"] = res.status
                break

    with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
        page = browser.new_page()
        page.on("request", on_request)
        page.on("response", on_response)

        page.goto("https://www.coupang.com", timeout=60000)
        for _ in range(4):
            page.mouse.wheel(0, random.randint(300, 700))
            time.sleep(random.uniform(1.5, 3.0))

        page.goto(f"https://www.coupang.com/np/search?q={KEYWORD}", timeout=60000)
        page.wait_for_timeout(4000)
        log["page1_items"] = page.evaluate(COUNT_JS)
        print("page1 items:", log["page1_items"])

        for pn in (2, 3):
            time.sleep(random.uniform(3.0, 5.0))
            before = page.evaluate(COUNT_JS)
            page.evaluate(CLICK_NAV_JS, pn)
            try:
                page.wait_for_timeout(6000)
            except Exception:  # noqa: BLE001
                pass
            after = page.evaluate(COUNT_JS)
            url_now = page.url
            no_result = page.evaluate(
                "() => document.body.innerText.includes('검색결과가 없습니다')")
            log["navigations"].append({
                "page": pn, "items_before": before, "items_after": after,
                "url": url_now, "no_result_msg": no_result,
            })
            print(f"[nav page={pn}] items {before} -> {after} url={url_now} no_result={no_result}")

    out = OUTPUT_DIR / f"search_nav_{ts}.json"
    out.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved:", out)


if __name__ == "__main__":
    main()
