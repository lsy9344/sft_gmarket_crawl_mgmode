"""
Coupang 검색 PoC v12 — 비로그인 페이지 돌파 경로 검증
========================================================
rev.10 확정: 번호 페이지네이션은 로그인 세션 전용.
본 스크립트는 비로그인으로 60개 상한을 넘는 3가지 경로를 검증:

  경로 1: 가격대 필터 — 쿠팡이 페이지에 내장한 공식 가격 밴드
          (뷰티 실측: 0-6000 / 6000-12000 / 12000-18000 / 18000-24000 / 24000+)
          각 밴드가 독립 결과 셋이면 키워드당 60×6 확보
  경로 2: 카테고리 PLP — /np/categories/176522 (뷰티) 의 ?page=2 동작 여부
  경로 3: 필터 + page 조합 — 필터 적용 시 page≥2 가 열리는지

URL 문법 후보:
  A) &isPriceRange=true&minPrice=N&maxPrice=N  (query 객체 반영)
  B) &filterType=0-6000                        (밴드 id 직접)
"""
import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

COUPANG_HOME = "https://www.coupang.com/"
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
    keyword = sys.argv[1] if len(sys.argv) > 1 else "뷰티"
    q = quote(keyword)
    log(f"PoC v12 — '{keyword}' 비로그인 돌파 경로 검증")

    report = {"keyword": keyword, "paths": []}
    base_ids: set[str] = set()

    from camoufox.sync_api import Camoufox
    with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
        page = browser.new_page()

        log("웜업 20초...")
        page.goto(COUPANG_HOME, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        end = time.monotonic() + 20
        while time.monotonic() < end:
            page.mouse.move(random.randint(100, 900), random.randint(100, 600))
            page.mouse.wheel(0, random.randint(50, 250))
            time.sleep(random.uniform(0.5, 1.5))

        def load_and_measure(url: str, name: str, save: bool = True) -> dict:
            nonlocal base_ids
            log(f"[{name}] → {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                log(f"  로드 실패: {type(e).__name__}")
                return {"name": name, "error": str(e)}
            time.sleep(random.uniform(3.0, 5.0))
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
            log(f"  카드 {cards}개 / href {len(ids)}개 (기본 대비 신규 {len(fresh)}) "
                f"/ {len(html)//1024}KB / 차단={blocked}")
            if save:
                safe = re.sub(r"[^a-z0-9]", "_", name)[:30]
                (OUTPUT_DIR / f"poc12_{ts}_{safe}.html").write_text(html, encoding="utf-8")
            result = {"name": name, "url": url[:200], "cards": cards,
                      "items": len(ids), "fresh_vs_base": len(fresh),
                      "blocked": blocked}
            report["paths"].append(result)
            time.sleep(random.uniform(14, 19))
            return result

        # 기준: 기본 검색 page1
        r = load_and_measure(f"https://www.coupang.com/np/search?q={q}", "base_page1")
        if r.get("cards", 0) > 0:
            # base_ids 갱신용 재수집
            base_ids = product_ids(page) or base_ids
        else:
            log("✗ 기본 검색 실패 — 중단")
            return 2

        # 경로 1A: 가격대 필터 (문법 A)
        load_and_measure(
            f"https://www.coupang.com/np/search?q={q}&isPriceRange=true&minPrice=0&maxPrice=6000",
            "price_band_A_0-6000")

        # 경로 1B: 가격대 필터 (문법 B — A 실패 시 의미 있는 비교용)
        load_and_measure(
            f"https://www.coupang.com/np/search?q={q}&filterType=0-6000",
            "price_band_B_0-6000")

        # 경로 2: 카테고리 PLP page1 + page2
        load_and_measure("https://www.coupang.com/np/categories/176522", "category_plp_p1")
        load_and_measure("https://www.coupang.com/np/categories/176522?page=2", "category_plp_p2")

        # 경로 3: 필터 + page 조합
        load_and_measure(
            f"https://www.coupang.com/np/search?q={q}&isPriceRange=true&minPrice=0&maxPrice=6000&page=2",
            "price_band_A_p2")

    out = OUTPUT_DIR / f"poc12_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log("=" * 60)
    log("요약:")
    for p in report["paths"]:
        if "error" in p:
            log(f"  {p['name']:24} 오류")
        else:
            log(f"  {p['name']:24} 카드 {p['cards']:3d}개, 신규 {p['fresh_vs_base']:3d}")
    log(f"리포트: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
