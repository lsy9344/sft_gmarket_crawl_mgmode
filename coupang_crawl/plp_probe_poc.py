"""페이지네이션 재검증 POC 6 — /np/search/filters(PLP) 라우트 검증

Wayback 2026-06~08 스냅샷에서 발견된 현재 라우트:
  /np/search/filters?channel=&component={categoryId}&...&listSize=60&page={N}&filterMode=PLP...

가설: 이 라우트는 명시적 page 파라미터를 지원한다.
검증: page=1 → page=2 (→ 가능하면 3) 로드 후 상품 수 측정.
차단 주의: 내비게이션 간 15~25초 휴지, 200 아니면 즉시 중단.
"""
import json
import random
import time
from datetime import datetime
from pathlib import Path

from camoufox.sync_api import Camoufox

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
CATEGORY_ID = "177229"  # Wayback 2026-08-10 스냅샷에서 200 확인된 카테고리

FILTERS_URL = (
    "https://www.coupang.com/np/search/filters?channel=&component={cid}"
    "&priceRange=&filterType=&minPrice=&maxPrice=&sorter=&listSize=60&page={page}"
    "&filter=&brand=&rating=0&isPriceRange=false&isEmptyByRocket=&filterMode=PLP"
    "&fromComponent=N&filterKey=&selectedPlpKeepFilter="
)

COUNT_JS = """
() => {
  const sel = ['#product-list > li', '#productList > li', 'li[data-product-id]',
               '[class*="ProductUnit_productUnit"]', '.search-product-wrap', '.baby-product'];
  const out = {};
  for (const s of sel) out[s] = document.querySelectorAll(s).length;
  return out;
}
"""


def main() -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log: dict = {"started_at": ts, "category": CATEGORY_ID, "loads": []}

    statuses: list[int] = []

    def on_response(res):
        if res.url.startswith("https://www.coupang.com/np/") and "filters" in res.url or \
           res.url.startswith("https://www.coupang.com/np/categories"):
            statuses.append(res.status)

    with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
        page = browser.new_page()
        page.on("response", on_response)

        page.goto("https://www.coupang.com", timeout=60000)
        for _ in range(5):
            page.mouse.wheel(0, random.randint(300, 700))
            time.sleep(random.uniform(2.0, 3.5))

        targets = [
            ("filters_p1", FILTERS_URL.format(cid=CATEGORY_ID, page=1)),
            ("filters_p2", FILTERS_URL.format(cid=CATEGORY_ID, page=2)),
            ("filters_p3", FILTERS_URL.format(cid=CATEGORY_ID, page=3)),
        ]
        for name, url in targets:
            time.sleep(random.uniform(15.0, 25.0))
            statuses.clear()
            try:
                resp = page.goto(url, timeout=60000)
                code = resp.status if resp else None
            except Exception as e:  # noqa: BLE001
                log["loads"].append({"name": name, "error": str(e)[:150]})
                print(f"[{name}] ERR {e}")
                break
            page.wait_for_timeout(5000)
            counts = page.evaluate(COUNT_JS)
            title = page.title()
            no_result = page.evaluate(
                "() => document.body.innerText.includes('검색결과가 없습니다') || "
                "document.body.innerText.includes('상품이 없습니다')")
            entry = {"name": name, "http": code, "counts": counts,
                     "title": title, "no_result": no_result, "url": page.url}
            log["loads"].append(entry)
            print(f"[{name}] http={code} title={title[:40]!r} counts={counts} no_result={no_result}")
            if code != 200:
                print("non-200 — stopping")
                break
            if name == "filters_p2" and max(counts.values()) == 0:
                print("p2 empty — skipping p3")
                break
            # save html for p1/p2
            p = OUTPUT_DIR / f"plp_{name}_{ts}.html"
            p.write_text(page.content(), encoding="utf-8")

    out = OUTPUT_DIR / f"plp_probe_{ts}.json"
    out.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved:", out)


if __name__ == "__main__":
    main()
