"""
Coupang 검색 PoC v3 — 페이지네이션 클릭 방식
================================================
v2 실측: 검색창 경로로 ~87개 로드 후 스크롤로는 더 증가 안 함.
         하단에 JS 페이지네이션(move next) 존재.
본 스크립트: 검색창 Enter → 바닥 스크롤 → '다음 페이지' 클릭 반복.
             각 단계의 상품 수/요청/URL 관찰.
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


def scroll_to_bottom(page, rounds: int = 4) -> None:
    for _ in range(rounds):
        page.keyboard.press("End")
        page.mouse.wheel(0, random.randint(1000, 2000))
        time.sleep(random.uniform(1.2, 2.2))


def main() -> int:
    keyword = sys.argv[1] if len(sys.argv) > 1 else "에어프라이어"
    max_pages = int(sys.argv[2]) if len(sys.argv) > 2 else 4

    log(f"Coupang 검색 PoC v3 — keyword='{keyword}', max_pages={max_pages}")

    captured = []
    from camoufox.sync_api import Camoufox
    with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
        page = browser.new_page()

        def on_request(req):
            url = req.url
            if "coupang" in url and "n-api" in url:
                captured.append({"method": req.method, "url": url[:250]})
        page.on("request", on_request)

        # 홈 웜업
        log("웜업...")
        page.goto(COUPANG_HOME, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        end = time.monotonic() + 20
        while time.monotonic() < end:
            page.mouse.move(random.randint(100, 900), random.randint(100, 600))
            page.mouse.wheel(0, random.randint(50, 200))
            time.sleep(random.uniform(0.5, 1.5))

        # 검색창 입력
        log("검색창 타이핑 → Enter...")
        si = None
        used_sel = ""
        for sel in ('input[name="q"]', '.headerSearchKeyword', '.coupang-search'):
            for el in page.query_selector_all(sel):
                try:
                    if el.is_visible():
                        si = el
                        used_sel = sel
                        break
                except Exception:
                    continue
            if si:
                break
        if not si:
            log("✗ 검색창 없음")
            return 2
        log(f"검색창 셀렉터: {used_sel}")
        si.click()
        time.sleep(0.6)
        si.type(keyword, delay=random.randint(90, 220))
        time.sleep(random.uniform(0.6, 1.2))
        page.keyboard.press("Enter")
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        time.sleep(random.uniform(3.0, 5.0))

        # 결과 로드 폴링 — 3KB 셸/중간 리다이렉트 페이지 대응
        all_ids = set()
        for attempt in range(10):
            all_ids = product_ids(page)
            if all_ids:
                break
            time.sleep(1.5)
            if attempt == 4:
                try:
                    page.wait_for_load_state("load", timeout=8000)
                except Exception:
                    pass
        all_ids = product_ids(page)
        log(f"초기: {len(all_ids)}개 | url={page.url} | title={page.title()!r}")
        if not all_ids:
            html = page.content()
            (OUTPUT_DIR / f"search_poc3_{ts}_init.html").write_text(html, encoding="utf-8")
            try:
                page.screenshot(path=str(OUTPUT_DIR / f"search_poc3_{ts}_init.png"), full_page=False)
            except Exception:
                pass
            log(f"  초기 0건 — HTML({len(html)//1024}KB)·스크린샷 저장")
        steps = [{"step": 0, "unique": len(all_ids), "url": page.url}]
        blocked = False

        for pno in range(1, max_pages + 1):
            captured.clear()
            scroll_to_bottom(page)
            time.sleep(random.uniform(1.0, 2.0))

            # 다음 페이지 링크 탐색 → 클릭
            clicked = False
            for sel in ['a.move.next', 'a[aria-label="다음 페이지 보기"]',
                        '.pagination a.next', 'a:has-text("다음")']:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.click()
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                log(f"✗ 페이지 {pno+1}: 다음 페이지 버튼 없음 — 종료")
                break

            # SPA 전환 or 전체 리로드 — 둘 다 대응
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            time.sleep(random.uniform(4.0, 6.0))
            scroll_to_bottom(page, rounds=2)

            new_ids = product_ids(page)
            fresh = new_ids - all_ids
            all_ids |= new_ids
            api_hits = [r["url"] for r in captured if "search" in r["url"].lower()][:5]
            log(f"페이지 {pno+1}: 현재 {len(new_ids)}개 (신규 {len(fresh)}), "
                f"누적 고유 {len(all_ids)}, url=...{page.url[-60:]}")
            for u in api_hits:
                log(f"    API: {u[:150]}")
            steps.append({
                "step": pno, "page_items": len(new_ids), "fresh": len(fresh),
                "unique": len(all_ids), "url": page.url, "api": api_hits,
            })

            if len(new_ids) == 0:
                html = page.content()
                (OUTPUT_DIR / f"search_poc3_{ts}_p{pno+1}.html").write_text(html, encoding="utf-8")
                log(f"  상품 0개 — HTML 저장 ({len(html)//1024}KB)")
                # 차단 여부 확인
                low = html.lower()
                if any(k in low for k in ("access denied", "captcha", "접근이 제한", "비정상", "사용권한")):
                    log("✗ 차단 감지 — 즉시 중단")
                    blocked = True
                break

            time.sleep(random.uniform(8.0, 15.0))  # 인간처럼 읽는 시간

    report = {"keyword": keyword, "steps": steps, "total_unique": len(all_ids), "blocked": blocked}
    out = OUTPUT_DIR / f"search_poc3_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log("=" * 60)
    log(f"결과: 고유 상품 총 {len(all_ids)}개, 차단={'있음' if blocked else '없음'}")
    log(f"리포트: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
