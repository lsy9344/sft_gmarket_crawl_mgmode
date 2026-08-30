"""
Coupang 검색 PoC v9 — 페이지네이션 확정 검증 (rev.7 대응)
============================================================
목적: "검색 결과 1~10 페이지" 존재 여부를 셀렉터 편향 없이 확정한다.

방법:
  1. 홈 웜업 → 검색창 타이핑 → Enter
  2. 바닥까지 스크롤 (페이지네이션 렌더 유도)
  3. 모든 클릭 가능 요소(a/button) 전수 덤프 — 텍스트·aria·href·class
  4. 페이지 번호/다음 류 후보 발견 시 클릭 → 상품 수 변화 측정
  5. 전체 HTML + 스크린샷 저장 (일반 사용자 화면과 비교용)

Usage:
    xvfb-run -a python coupang_search_poc9.py [키워드]
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
    keyword = sys.argv[1] if len(sys.argv) > 1 else "뷰티"
    log(f"PoC v9 — '{keyword}' 페이지네이션 확정 검증")

    from camoufox.sync_api import Camoufox
    with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
        page = browser.new_page()

        log("Phase 1: 홈 웜업 20초...")
        page.goto(COUPANG_HOME, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        end = time.monotonic() + 20
        while time.monotonic() < end:
            page.mouse.move(random.randint(100, 900), random.randint(100, 600))
            page.mouse.wheel(0, random.randint(50, 250))
            time.sleep(random.uniform(0.5, 1.5))

        log("Phase 2: 검색창 타이핑 → Enter...")
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
        log(f"  초기 결과: {len(base_ids)}개")
        if not base_ids:
            log("✗ 검색 결과 없음 — 중단")
            return 2

        log("Phase 3: 바닥까지 스크롤 (페이지네이션 렌더 유도)...")
        for _ in range(15):
            page.keyboard.press("End")
            page.mouse.wheel(0, random.randint(1500, 3000))
            time.sleep(random.uniform(0.8, 1.5))
        time.sleep(2)
        log(f"  바닥 도달 후: {len(product_ids(page))}개")

        # 저장: 전체 HTML + 스크린샷
        html = page.content()
        (OUTPUT_DIR / f"poc9_{ts}_full.html").write_text(html, encoding="utf-8")
        try:
            page.screenshot(path=str(OUTPUT_DIR / f"poc9_{ts}_bottom.png"), full_page=False)
        except Exception as e:
            log(f"  스크린샷 실패: {e}")

        log("Phase 4: 클릭 가능 요소 전수 덤프...")
        elements = []
        for el in page.query_selector_all("a, button"):
            try:
                if not el.is_visible():
                    continue
                txt = re.sub(r"\s+", " ", (el.inner_text() or "").strip())[:50]
                aria = (el.get_attribute("aria-label") or "")[:50]
                cls = (el.get_attribute("class") or "")[:100]
                href = (el.get_attribute("href") or "")[:150]
                elements.append({"tag": el.evaluate("e => e.tagName"),
                                 "text": txt, "aria": aria,
                                 "class": cls, "href": href})
            except Exception:
                continue
        log(f"  보이는 클릭 요소 총 {len(elements)}개")

        # 페이지네이션 후보 필터링: 숫자 텍스트 / 다음 / page= 링크 / pagination 클래스
        candidates = []
        for el in elements:
            blob = f"{el['text']}|{el['aria']}|{el['class']}|{el['href']}"
            if (re.fullmatch(r"[2-9]|10", el["text"].strip())
                    or "다음" in blob or "next" in blob.lower()
                    or "pagination" in blob.lower() or "page=" in el["href"]):
                candidates.append(el)
        log(f"  페이지네이션 후보 {len(candidates)}개:")
        for c in candidates[:25]:
            log(f"    {c['tag']} | {c['text'] or c['aria']} | cls={c['class'][:40]} | href={c['href'][:60]}")

        log("Phase 5: 후보 클릭 테스트...")
        click_results = []
        tested = set()
        for c in candidates:
            key = (c["text"], c["aria"], c["class"])
            if key in tested or len(click_results) >= 3:
                continue
            tested.add(key)
            # 상품 링크는 클릭 대상에서 제외 (결과 영역 이탈 방지)
            if "/vp/products/" in c["href"]:
                continue
            log(f"  클릭 시도: {c['tag']} '{c['text'] or c['aria']}'")
            try:
                sel = None
                if c["aria"]:
                    sel = f'{c["tag"].lower()}[aria-label="{c["aria"]}"]'
                    el = page.query_selector(sel)
                if el is None and c["text"] and len(c["text"]) < 20:
                    el = page.query_selector(f'{c["tag"].lower()}:has-text("{c["text"]}")')
                if el is None or not el.is_visible():
                    log("    요소 재발견 실패 — 건너뜀")
                    continue
                before = len(product_ids(page))
                el.click()
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                time.sleep(random.uniform(3.0, 5.0))
                after_ids = set()
                for _ in range(8):
                    after_ids = product_ids(page)
                    if after_ids:
                        break
                    time.sleep(1.5)
                fresh = after_ids - base_ids
                log(f"    클릭 후: {len(after_ids)}개 (신규 {len(fresh)}), url={page.url[:100]}")
                click_results.append({
                    "element": c, "before": before, "after": len(after_ids),
                    "fresh": len(fresh), "url": page.url[:200],
                })
                if after_ids and fresh:
                    log("    ✓ 페이지 전환으로 상품 로드 확인!")
                    break
            except Exception as e:
                log(f"    클릭 오류: {type(e).__name__}")
                continue

        report = {
            "keyword": keyword,
            "base_items": len(base_ids),
            "total_clickable": len(elements),
            "pagination_candidates": candidates,
            "click_results": click_results,
            "pagination_confirmed": any(r["fresh"] > 0 for r in click_results),
        }
        out = OUTPUT_DIR / f"poc9_{ts}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        log("=" * 60)
        log(f"결과: 페이지네이션 {'확인' if report['pagination_confirmed'] else '미확인'}")
        log(f"리포트: {out}")
        log(f"전체 HTML: poc9_{ts}_full.html / 스크린샷: poc9_{ts}_bottom.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
