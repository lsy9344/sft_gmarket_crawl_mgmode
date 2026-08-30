"""
Coupang 검색 PoC v7 — '뷰티' 종합 검증
========================================
검증 항목:
  1. 뷰티 키워드 검색 결과 형태 (상품 목록 vs 카테고리 허브)
  2. listSize 파라미터가 SSR 개수에 영향 주는지 (36 → 100/120)
  3. 정렬(sorter) 변경 시 서로 다른 상품 셋이 나오는지 (73개 상한 확장 검증)
  4. href 에 itemId/vendorItemId 포함 여부 (기존 3-API 파이프라인 호환)
  5. 로켓배송 배지 식별 가능 여부 (제외 처리용)
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
KEYWORD = "뷰티"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def item_hrefs(page) -> dict[str, str]:
    """pid -> full href (중복 제거, 첫 발견 우선)"""
    out = {}
    for a in page.query_selector_all('a[href*="/vp/products/"]'):
        href = a.get_attribute("href") or ""
        m = re.search(r"/vp/products/(\d+)", href)
        if m and m.group(1) not in out:
            out[m.group(1)] = href
    return out


def wait_items(page, rounds=12) -> dict[str, str]:
    items = {}
    for _ in range(rounds):
        items = item_hrefs(page)
        if items:
            break
        time.sleep(1.5)
    return items


def main() -> int:
    report = {"keyword": KEYWORD, "tests": {}}

    from camoufox.sync_api import Camoufox
    with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
        page = browser.new_page()

        log("웜업...")
        page.goto(COUPANG_HOME, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        end = time.monotonic() + 18
        while time.monotonic() < end:
            page.mouse.move(random.randint(100, 900), random.randint(100, 600))
            page.mouse.wheel(0, random.randint(50, 250))
            time.sleep(random.uniform(0.5, 1.5))

        q = quote(KEYWORD)

        # ── Test 1: 기본 검색 (직접 URL, SSR) ─────────────────────────────
        url1 = f"https://www.coupang.com/np/search?q={q}"
        log(f"Test 1: 기본 검색 → {url1}")
        page.goto(url1, wait_until="domcontentloaded", timeout=30000)
        items1 = wait_items(page)
        html1 = page.content()
        (OUTPUT_DIR / f"poc7_{ts}_t1.html").write_text(html1, encoding="utf-8")
        log(f"  결과: {len(items1)}개, HTML {len(html1)//1024}KB, title={page.title()[:40]!r}")
        report["tests"]["t1_default"] = {"items": len(items1), "html_kb": len(html1)//1024,
                                         "title": page.title()[:80], "url": page.url[:150]}

        if not items1:
            log("✗ 상품 없음 — 카테고리 허브/빈 결과 가능성. 저장 HTML 확인 필요")
            out = OUTPUT_DIR / f"poc7_{ts}.json"
            out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            return 2

        # Test 4: href 스키마 호환 확인
        sample_href = next(iter(items1.values()))
        has_viid = "vendorItemId=" in sample_href
        has_iid = "itemId=" in sample_href
        log(f"  href 스키마: itemId={'✓' if has_iid else '✗'} vendorItemId={'✓' if has_viid else '✗'}")
        report["tests"]["t4_href_schema"] = {"itemId": has_iid, "vendorItemId": has_viid,
                                             "sample": sample_href[:200]}

        # Test 5: 로켓 배지 식별 — 상품 카드에서 '로켓' 텍스트/alt 탐색
        rocket_hits = html1.count("로켓배송") + html1.count("rocket")
        rocket_badge_selectors = [
            'img[alt*="로켓"]', '[class*="rocket"]', 'svg[aria-label*="로켓"]',
        ]
        rocket_count = 0
        for sel in rocket_badge_selectors:
            try:
                n = len(page.query_selector_all(sel))
                if n:
                    log(f"  로켓 식별 '{sel}': {n}개")
                    rocket_count = max(rocket_count, n)
            except Exception:
                pass
        report["tests"]["t5_rocket"] = {"text_hits": rocket_hits, "badge_count": rocket_count}

        time.sleep(random.uniform(12, 18))

        # ── Test 2: listSize 증량 ───────────────────────────────────────────
        url2 = f"https://www.coupang.com/np/search?q={q}&listSize=120"
        log(f"Test 2: listSize=120 → {url2}")
        page.goto(url2, wait_until="domcontentloaded", timeout=30000)
        items2 = wait_items(page)
        log(f"  결과: {len(items2)}개 (기본 대비 {'증가' if len(items2) > len(items1) else '변화 없음'})")
        report["tests"]["t2_listsize120"] = {"items": len(items2)}
        time.sleep(random.uniform(12, 18))

        # ── Test 3: 정렬 변경 (판매량순) ────────────────────────────────────
        url3 = f"https://www.coupang.com/np/search?q={q}&sorter=saleCountDesc"
        log(f"Test 3: 판매량순 → {url3}")
        page.goto(url3, wait_until="domcontentloaded", timeout=30000)
        items3 = wait_items(page)
        fresh3 = set(items3) - set(items1) - set(items2)
        log(f"  결과: {len(items3)}개, 기존과 겹치지 않는 신규 {len(fresh3)}개")
        report["tests"]["t3_sort_salecount"] = {"items": len(items3), "fresh": len(fresh3)}
        time.sleep(random.uniform(12, 18))

        # 정렬 2종 더 (낮은가격순, 최신순) — 확장량 측정
        for name, sorter in (("t3b_sort_lowprice", "salePriceAsc"), ("t3c_sort_latest", "latestAsc")):
            url = f"https://www.coupang.com/np/search?q={q}&sorter={sorter}"
            log(f"Test {name}: {sorter}...")
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            it = wait_items(page)
            fresh = set(it) - set(items1) - set(items2) - set(items3)
            log(f"  결과: {len(it)}개, 신규 {len(fresh)}개")
            report["tests"][name] = {"items": len(it), "fresh": len(fresh)}
            time.sleep(random.uniform(12, 18))

        # 연관 검색어 추출 (사이드바)
        related = []
        for a in page.query_selector_all('a[href*="/np/search?q="]'):
            try:
                txt = (a.inner_text() or "").strip()
                href = a.get_attribute("href") or ""
                if txt and len(txt) < 40 and "channel=" in href:
                    related.append(txt)
            except Exception:
                continue
        related = list(dict.fromkeys(related))
        log(f"연관 검색어 {len(related)}개: {related[:12]}")
        report["related_keywords"] = related

    out = OUTPUT_DIR / f"poc7_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"완료 — 리포트 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
