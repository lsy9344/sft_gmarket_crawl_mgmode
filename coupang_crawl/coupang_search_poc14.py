"""
Coupang 검색 PoC v14 — PLP 상한 이분 탐색 + 하위 카테고리 검증
==================================================================
1. 카테고리 176522(뷰티) page=16 탐색 → 상한 이분 탐색
   (16 통과 시 18 추가 탐색, 미통과 시 14 탐색)
2. 하위 카테고리 176530(스킨케어) page=3 로드 — 서브 PLP 페이지네이션 확인
   + 서브-서브 카테고리 링크 추출 (HTML 저장)
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
CATEGORY_URL = "https://www.coupang.com/np/categories/{cid}?page={page}"
ts = datetime.now().strftime("%Y%m%d_%H%M%S")


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
    log("PoC v14 — PLP 상한 이분 탐색 + 하위 카테고리 검증")
    report = {"steps": []}

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

        def probe(cid: str, pno: int, save: bool = False, name: str = "") -> dict:
            url = CATEGORY_URL.format(cid=cid, page=pno)
            label = name or f"cat{cid}_p{pno}"
            log(f"[{label}] → {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                log(f"  로드 실패: {type(e).__name__}")
                return {"name": label, "error": str(e)}
            time.sleep(random.uniform(3.0, 5.0))
            ids = set()
            for _ in range(8):
                ids = product_ids(page)
                if ids:
                    break
                time.sleep(1.5)
            html = page.content()
            cards = count_cards(html)
            blocked = "사용권한" in html or "Access Denied" in html
            log(f"  카드 {cards}개 / 상품 {len(ids)}개 / 차단={blocked}")
            if save:
                (OUTPUT_DIR / f"poc14_{ts}_{label}.html").write_text(html, encoding="utf-8")
            result = {"name": label, "url": url, "cards": cards,
                      "items": len(ids), "blocked": blocked}
            report["steps"].append(result)
            time.sleep(random.uniform(13, 17))
            return result

        # 1. 상한 이분 탐색
        r16 = probe("176522", 16)
        if r16.get("cards", 0) > 0:
            probe("176522", 18)
        else:
            probe("176522", 14)

        # 2. 하위 카테고리(스킨케어) page=3 + HTML 저장
        probe("176530", 3, save=True, name="skincare_p3")

    out = OUTPUT_DIR / f"poc14_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"완료 — 리포트 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
