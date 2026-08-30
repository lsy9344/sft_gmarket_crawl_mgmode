"""최종 검증 POC — 카테고리 PLP 페이지네이션 (히스토리 rev.11~13 재확인)

히스토리 문서(coupang-search 브랜치) 결론:
  - /np/search(SRP) 는 page≥2 서빙 거부 (disableFixedPagination, 세션 속성 종속)
  - /np/categories/{id}?page=N 은 비로그인에서도 SSR — page 17까지 실측됨

본 스크립트는 위 결론이 '오늘도' 성립하는지 최소 요청으로 재확인한다:
  웜업 → 카테고리 page1 → (20초 휴지) → page2 → 상품 수/신규 수 측정.
차단 주의: 200 아니면 즉시 중단.
"""
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path

from camoufox.sync_api import Camoufox

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
CATEGORY_ID = "176522"  # 뷰티 — 히스토리 실측 카테고리
BASE = f"https://www.coupang.com/np/categories/{CATEGORY_ID}"

PRODUCT_HREF_JS = """
() => {
  const ids = new Set();
  for (const a of document.querySelectorAll('a[href*="/vp/products/"]')) {
    const m = a.href.match(/\\/vp\\/products\\/(\\d+)/);
    if (m) ids.add(m[1]);
  }
  return Array.from(ids);
}
"""


def main() -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log: dict = {"started_at": ts, "category": CATEGORY_ID, "loads": []}
    seen: set[str] = set()

    with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
        page = browser.new_page()

        page.goto("https://www.coupang.com", timeout=60000)
        for _ in range(5):
            page.mouse.wheel(0, random.randint(300, 700))
            time.sleep(random.uniform(2.0, 3.5))

        for pname, url in (("plp_p1", BASE), ("plp_p2", BASE + "?page=2")):
            if pname != "plp_p1":
                time.sleep(random.uniform(18.0, 26.0))
            try:
                resp = page.goto(url, timeout=60000)
                code = resp.status if resp else None
            except Exception as e:  # noqa: BLE001
                log["loads"].append({"name": pname, "error": str(e)[:150]})
                print(f"[{pname}] ERR {e}")
                break
            page.wait_for_timeout(5000)
            ids = page.evaluate(PRODUCT_HREF_JS)
            new = [i for i in ids if i not in seen]
            seen.update(ids)
            no_result = page.evaluate(
                "() => document.body.innerText.includes('검색결과가 없습니다') || "
                "document.body.innerText.includes('상품이 없습니다')")
            log["loads"].append({
                "name": pname, "http": code, "products": len(ids),
                "new": len(new), "no_result": no_result, "title": page.title(),
            })
            print(f"[{pname}] http={code} products={len(ids)} new={len(new)} title={page.title()[:40]!r}")
            p = OUTPUT_DIR / f"cat_{pname}_{ts}.html"
            p.write_text(page.content(), encoding="utf-8")
            if code != 200:
                print("non-200 — stopping")
                break

        log["unique_total"] = len(seen)

    out = OUTPUT_DIR / f"cat_probe_{ts}.json"
    out.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved:", out)


if __name__ == "__main__":
    main()
