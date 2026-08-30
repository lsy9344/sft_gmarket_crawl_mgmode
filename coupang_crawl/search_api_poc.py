"""검색 페이지네이션 규명 POC 4 — 페이지 내부 API/RSC 탐색

모든 요청을 로드된 검색 페이지 '내부'에서 fetch (쿠키·세션 포함, Akamai 통과 상태).
요청 수 최소화(세션 1개, 소량 호출).

탐색 대상:
  A) /n-api/web-adapter/search?keyword=...  (실측 로그에 등장)
  B) /n-api/web-adapter/proxy               (홈/SRP에서 호출되던 것)
  C) RSC flight 요청으로 ?page=2 (헤더 RSC:1)
  D) listSize=72 전체 페이지 로드 (상품 수 변화)
"""
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path

from camoufox.sync_api import Camoufox

KEYWORD = "에어프라이어"
OUTPUT_DIR = Path(__file__).resolve().parent / "output"

COUNT_JS = "() => document.querySelectorAll('#product-list > li').length"


def main() -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log: dict = {"started_at": ts, "probes": []}

    def probe(name: str, page, js: str, arg=None) -> None:
        try:
            r = page.evaluate(js, arg) if arg is not None else page.evaluate(js)
            log["probes"].append({"name": name, **r})
            print(f"[{name}] status={r.get('status')} len={r.get('len')} head={str(r.get('head'))[:200]}")
        except Exception as e:  # noqa: BLE001
            log["probes"].append({"name": name, "error": str(e)[:200]})
            print(f"[{name}] ERR {e}")

    FETCH_JSON = """async (url) => {
        const r = await fetch(url, {credentials: 'include'});
        const t = await r.text();
        return {status: r.status, len: t.length, ctype: r.headers.get('content-type'),
                head: t.slice(0, 600)};
    }"""

    RSC_FETCH = """async (url) => {
        const r = await fetch(url, {credentials: 'include', headers: {
            'RSC': '1', 'Next-Router-Prefetch': '1', 'Next-Url': '/np/search'}});
        const t = await r.text();
        const ids = (t.match(/legacyProductId/g) || []).length;
        return {status: r.status, len: t.length, ctype: r.headers.get('content-type'),
                productRefs: ids, head: t.slice(0, 400)};
    }"""

    with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
        page = browser.new_page()
        page.goto("https://www.coupang.com", timeout=60000)
        for _ in range(4):
            page.mouse.wheel(0, random.randint(300, 700))
            time.sleep(random.uniform(1.5, 3.0))

        page.goto(f"https://www.coupang.com/np/search?q={KEYWORD}", timeout=60000)
        page.wait_for_timeout(4000)
        log["page1_items"] = page.evaluate(COUNT_JS)
        print("page1 items:", log["page1_items"])

        probe("A_webadapter_search", page, FETCH_JSON,
              f"/n-api/web-adapter/search?keyword={KEYWORD}&_={int(time.time()*1000)}")
        time.sleep(random.uniform(1.0, 2.0))
        probe("B_webadapter_proxy", page, FETCH_JSON, "/n-api/web-adapter/proxy")
        time.sleep(random.uniform(1.0, 2.0))
        probe("C_rsc_page2", page, RSC_FETCH,
              f"/np/search?q={KEYWORD}&page=2")
        time.sleep(random.uniform(1.0, 2.0))
        probe("C_rsc_page1", page, RSC_FETCH,
              f"/np/search?q={KEYWORD}&page=1")

        # D: listSize=72 full load
        time.sleep(random.uniform(3.0, 5.0))
        page.goto(f"https://www.coupang.com/np/search?q={KEYWORD}&listSize=72", timeout=60000)
        page.wait_for_timeout(4000)
        log["listSize72_items"] = page.evaluate(COUNT_JS)
        print("listSize72 items:", log["listSize72_items"])

    out = OUTPUT_DIR / f"search_api_{ts}.json"
    out.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved:", out)


if __name__ == "__main__":
    main()
