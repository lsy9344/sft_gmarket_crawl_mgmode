"""
Coupang 검색 PoC v13 — 카테고리 PLP 페이지네이션 깊이 측정
================================================================
rev.11(poc12)에서 /np/categories/{id}?page=2 가 비로그인에서 SSR 되는 것 확인.
본 스크립트: page=3,5,8,12 순차 탐색으로 깊이의 상한을 측정한다.

규율: 세션 1회, 페이지 간 12~15초, 차단 감지 시 즉시 중단.
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
    cid = sys.argv[1] if len(sys.argv) > 1 else "176522"  # 뷰티
    pages = [int(p) for p in (sys.argv[2].split(",") if len(sys.argv) > 2 else ["3", "5", "8", "12"])]
    log(f"PoC v13 — 카테고리 {cid} PLP 깊이 측정: {pages}")

    report = {"category_id": cid, "pages": []}
    all_ids: set[str] = set()

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

        for pno in pages:
            url = CATEGORY_URL.format(cid=cid, page=pno)
            log(f"page={pno} → {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                log(f"  로드 실패: {type(e).__name__}")
                report["pages"].append({"page": pno, "error": str(e)})
                break
            time.sleep(random.uniform(3.0, 5.0))
            ids = set()
            for _ in range(8):
                ids = product_ids(page)
                if ids:
                    break
                time.sleep(1.5)
            html = page.content()
            cards = count_cards(html)
            fresh = ids - all_ids
            all_ids |= ids
            blocked = "사용권한" in html or "Access Denied" in html
            log(f"  카드 {cards}개 / 상품 {len(ids)}개 (신규 {len(fresh)}) / 차단={blocked}")
            report["pages"].append({
                "page": pno, "cards": cards, "items": len(ids),
                "fresh": len(fresh), "blocked": blocked,
            })
            if blocked:
                log("✗ 차단 감지 — 즉시 중단")
                break
            if cards == 0:
                log(f"  page={pno} 빈 결과 — 깊이 상한 확인")
                break
            time.sleep(random.uniform(12, 15))

    report["total_unique"] = len(all_ids)
    out = OUTPUT_DIR / f"poc13_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log("=" * 60)
    log(f"누적 고유 상품: {len(all_ids)}개 — 리포트 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
