"""
Coupang 검색 PoC v6 — 필터 사이드바 조사 + 하위 분할 수집 검증
================================================================
rev.2 판정: 신형 검색은 페이지네이션 없음, 키워드당 ~72개에서 종료.
확장 전략: 필터(카테고리/가격대) 하위 분할로 결과 셋 분할.
본 스크립트: 필터 사이드바의 실제 링크를 수집하고, 카테고리 필터 2개를
클릭해 각 하위 셋의 상품 수·URL을 확인한다.
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


def main() -> int:
    keyword = sys.argv[1] if len(sys.argv) > 1 else "에어프라이어"
    log(f"PoC v6 — keyword='{keyword}'")

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

        si = None
        for sel in ('input[name="q"]', '.headerSearchKeyword'):
            for el in page.query_selector_all(sel):
                try:
                    if el.is_visible():
                        si = el
                        break
                except Exception:
                    continue
            if si:
                break
        if not si:
            log("✗ 검색창 없음")
            return 2
        si.click(); time.sleep(0.6)
        si.type(keyword, delay=random.randint(90, 220))
        time.sleep(0.7)
        page.keyboard.press("Enter")
        page.wait_for_load_state("domcontentloaded", timeout=30000)

        base_ids = set()
        for _ in range(12):
            base_ids = product_ids(page)
            if base_ids:
                break
            time.sleep(1.5)
        log(f"기본 결과: {len(base_ids)}개")
        if not base_ids:
            return 2

        # 필터 사이드바 조사 — 상단에서 천천히 내려오며 렌더 유도
        page.keyboard.press("Home")
        time.sleep(1)
        filter_links = []
        for _ in range(6):
            page.mouse.wheel(0, random.randint(150, 300))
            time.sleep(random.uniform(0.4, 0.8))
        for a in page.query_selector_all('a'):
            try:
                if not a.is_visible():
                    continue
                href = a.get_attribute("href") or ""
                txt = re.sub(r'\s+', ' ', (a.inner_text() or "").strip())[:80]
                if not txt:
                    continue
                # 필터/카테고리 관련 링크만 (상품 링크 제외)
                if "/vp/products/" in href:
                    continue
                if any(k in href for k in ("search", "categories", "component", "filter")) \
                        or any(k in txt for k in ("카테고리", "브랜드", "가격")):
                    filter_links.append({"text": txt, "href": href[:200]})
            except Exception:
                continue
        log(f"필터/카테고리 링크 {len(filter_links)}개:")
        seen = set()
        for fl in filter_links:
            key = fl["text"]
            if key in seen:
                continue
            seen.add(key)
            log(f"  [{fl['text']}] → {fl['href'][:120]}")

        # 사이드바 전체 HTML 스냅샷 (필터 영역)
        for sel in ('[class*="filter"]', 'nav', 'aside'):
            try:
                el = page.query_selector(sel)
                if el:
                    (OUTPUT_DIR / f"poc6_{ts}_sidebar.html").write_text(
                        el.inner_html(), encoding="utf-8")
                    break
            except Exception:
                continue

        # 카테고리 필터 클릭 테스트 (처음 발견되는 카테고리류 링크)
        click_target = None
        for fl in filter_links:
            if ("categories" in fl["href"] or "component" in fl["href"]) and len(fl["text"]) < 30:
                click_target = fl
                break
        results = []
        if click_target:
            log(f"필터 클릭 테스트: {click_target['text']}")
            try:
                el = page.query_selector(f'a[href="{click_target["href"]}"]') or \
                     page.query_selector(f'a:has-text("{click_target["text"][:20]}")')
                if el and el.is_visible():
                    el.click()
                    page.wait_for_load_state("domcontentloaded", timeout=20000)
                    time.sleep(random.uniform(3.0, 5.0))
                    sub_ids = set()
                    for _ in range(10):
                        sub_ids = product_ids(page)
                        if sub_ids:
                            break
                        time.sleep(1.5)
                    fresh = sub_ids - base_ids
                    log(f"  → 필터 결과: {len(sub_ids)}개 (기본 셋과 겹치지 않는 신규 {len(fresh)})")
                    log(f"  → url: {page.url[:150]}")
                    results.append({"filter": click_target["text"], "items": len(sub_ids),
                                    "fresh": len(fresh), "url": page.url[:250]})
            except Exception as e:
                log(f"  필터 클릭 실패: {type(e).__name__}: {e}")
        else:
            log("클릭할 카테고리 필터 링크 미발견")

        report = {"keyword": keyword, "base_items": len(base_ids),
                  "filter_links": filter_links[:60], "filter_test": results}
        out = OUTPUT_DIR / f"poc6_{ts}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"완료 — 리포트 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
