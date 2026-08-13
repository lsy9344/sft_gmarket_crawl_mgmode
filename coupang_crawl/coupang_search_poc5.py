"""
Coupang 검색 PoC v5 — 결과 하단 UI 조사 + '이어 검색' 상호작용 검증
=====================================================================
rev.2 실측(poc4): page2 '다음 페이지' 버튼이 실제로는 존재하지 않음.
(어제 RSC에서 본 move.next 는 '최근 본 상품' 모듈의 것)
본 스크립트: 검색 결과 바닥의 실제 버튼/링크 목록을 수집하고,
'이어서 검색하기'/'더보기' 류 클릭 시 상품이 더 로드되는지 검증.
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
    log(f"PoC v5 — keyword='{keyword}'")

    captured = []
    from camoufox.sync_api import Camoufox
    with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
        page = browser.new_page()

        def on_request(req):
            u = req.url
            if "coupang" in u and not any(x in u for x in (".png", ".jpg", ".webp", ".css", ".js", ".woff", ".svg")):
                captured.append({"method": req.method, "url": u[:300]})
        page.on("request", on_request)

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
        time.sleep(random.uniform(0.6, 1.2))
        page.keyboard.press("Enter")
        page.wait_for_load_state("domcontentloaded", timeout=30000)

        all_ids = set()
        for _ in range(12):
            all_ids = product_ids(page)
            if all_ids:
                break
            time.sleep(1.5)
        log(f"초기 로드: {len(all_ids)}개")
        if not all_ids:
            return 2

        # 바닥까지 스크롤 (결과 끝 도달)
        for _ in range(12):
            page.keyboard.press("End")
            page.mouse.wheel(0, random.randint(1500, 3000))
            time.sleep(random.uniform(0.8, 1.6))
        time.sleep(2)
        log(f"바닥 도달 후: {len(product_ids(page))}개")

        try:
            page.screenshot(path=str(OUTPUT_DIR / f"poc5_{ts}_bottom.png"))
        except Exception as e:
            log(f"스크린샷 실패: {e}")
        html = page.content()
        (OUTPUT_DIR / f"poc5_{ts}_bottom.html").write_text(html, encoding="utf-8")

        # 바닥 영역의 보이는 클릭 가능 요소 목록 수집
        inventory = []
        for el in page.query_selector_all('a, button'):
            try:
                if not el.is_visible():
                    continue
                txt = re.sub(r'\s+', ' ', (el.inner_text() or "").strip())[:60]
                aria = el.get_attribute("aria-label") or ""
                cls = (el.get_attribute("class") or "")[:80]
                href = (el.get_attribute("href") or "")[:100]
                if txt or aria:
                    inventory.append({"tag": el.evaluate("e => e.tagName"), "text": txt,
                                      "aria": aria, "class": cls, "href": href})
            except Exception:
                continue
        log(f"보이는 클릭 요소 {len(inventory)}개 — 바닥 관련:")
        for it in inventory:
            blob = it["text"] + it["aria"]
            if any(k in blob for k in ("이어", "더보기", "더 보기", "검색", "다음", "페이지", "결과")):
                log(f"  {it['tag']} | {it['text'] or it['aria']} | cls={it['class'][:50]} | href={it['href'][:60]}")

        # 후보 클릭: 이어서 검색 / 더보기 류
        candidates = [
            'a:has-text("이어서 검색하기")', 'button:has-text("이어서 검색하기")',
            'a:has-text("이어 검색")', 'button:has-text("이어 검색")',
            'a:has-text("더보기")', 'button:has-text("더보기")',
            'a:has-text("검색결과 더보기")', '[class*="searchGuide"] a', '[class*="guide"] a',
        ]
        before = len(all_ids)
        clicked_any = False
        for sel in candidates:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    captured.clear()
                    log(f"클릭 시도: {sel}")
                    el.click()
                    clicked_any = True
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=15000)
                    except Exception:
                        pass
                    time.sleep(random.uniform(4.0, 6.0))
                    new_ids = set()
                    for _ in range(10):
                        new_ids = product_ids(page)
                        if new_ids:
                            break
                        time.sleep(1.5)
                    fresh = new_ids - all_ids
                    all_ids |= new_ids
                    log(f"  → 클릭 후 상품 {len(new_ids)}개 (신규 {len(fresh)}), url={page.url[:100]}")
                    api = [r for r in captured if "n-api" in r["url"] or "reco" in r["url"]][:10]
                    for r in api:
                        log(f"    {r['method']} {r['url'][:140]}")
                    break
            except Exception as e:
                log(f"  ({sel} 클릭 오류: {type(e).__name__})")
        if not clicked_any:
            log("클릭 가능한 이어보기 후보 없음")

        (OUTPUT_DIR / f"poc5_{ts}_after.html").write_text(page.content(), encoding="utf-8")

    report = {"keyword": keyword, "initial": before if clicked_any else len(all_ids),
              "final": len(all_ids), "inventory": inventory,
              "captured_tail": captured[-40:]}
    out = OUTPUT_DIR / f"poc5_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"완료: 최종 {len(all_ids)}개 — 리포트 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
