"""
Coupang 검색 PoC v10 — 동일 세션 내 ?page=2 접근 검증
=========================================================
rev.8 기준 미시도 조합: 검색창으로 검색을 확립한 직후, 동일한 세션
(쿠키/_abck 유지)에서 ?page=2 직접 접근이 상품 데이터를 돌려주는지 확인.

어제(rev.1~2)의 ?page=2 실패는 검색 확립 전 fresh 접근이었음 — 조건이 다름.

순서:
  1. 홈 웜업
  2. 검색창 타이핑 → Enter (검색 세션 확립)
  3. 상품 수 확인
  4. 동일 세션에서 ?page=2 접근 → 상품 수/HTML 저장
  5. (4 실패 시) channel=user 등 파라미터 변형 1회 시도
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
    log(f"PoC v10 — '{keyword}' 동일 세션 ?page=2 검증")

    report = {"keyword": keyword, "steps": []}

    from camoufox.sync_api import Camoufox
    with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
        page = browser.new_page()

        log("Step 1: 홈 웜업 20초...")
        page.goto(COUPANG_HOME, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        end = time.monotonic() + 20
        while time.monotonic() < end:
            page.mouse.move(random.randint(100, 900), random.randint(100, 600))
            page.mouse.wheel(0, random.randint(50, 250))
            time.sleep(random.uniform(0.5, 1.5))

        log("Step 2: 검색창 타이핑 → Enter (세션 확립)...")
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
        log(f"  page1: {len(base_ids)}개, url=...{page.url[-70:]}")
        report["steps"].append({"step": "search_page1", "items": len(base_ids),
                                "url": page.url})
        if not base_ids:
            return 2

        # 검색 결과 화면에서 잠시 체류 (자연스러운 맥락)
        for _ in range(3):
            page.mouse.wheel(0, random.randint(200, 500))
            time.sleep(random.uniform(1.0, 2.0))

        time.sleep(random.uniform(12, 18))

        # Step 3: 동일 세션에서 ?page=2 직접 접근
        variants = [
            f"https://www.coupang.com/np/search?q={q}&page=2",
            f"https://www.coupang.com/np/search?q={q}&page=2&channel=user",
        ]
        for i, url in enumerate(variants):
            log(f"Step 3.{i+1}: 동일 세션 직접 접근 → {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                log(f"  로드 실패: {type(e).__name__}: {e}")
                report["steps"].append({"step": f"page2_v{i+1}", "error": str(e)})
                break
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
            log(f"  결과: href {len(ids)}개 (신규 {len(fresh)}), "
                f"메인 카드 {cards}개, HTML {len(html)//1024}KB, 차단={blocked}")
            (OUTPUT_DIR / f"poc10_{ts}_v{i+1}.html").write_text(html, encoding="utf-8")
            report["steps"].append({
                "step": f"page2_v{i+1}", "url_tried": url, "items": len(ids),
                "fresh": len(fresh), "cards": cards,
                "html_kb": len(html)//1024, "blocked": blocked,
                "final_url": page.url[:150],
            })
            if cards > 0 and fresh:
                log("  ✓ page2 상품 로드 성공!")
                break
            time.sleep(random.uniform(12, 18))

    out = OUTPUT_DIR / f"poc10_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"완료 — 리포트 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
