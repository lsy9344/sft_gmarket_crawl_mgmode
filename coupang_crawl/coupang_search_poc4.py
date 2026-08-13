"""
Coupang 검색 PoC v4 — 2일차 실측 (쿨다운 확인 + 페이지네이션 + API 판별)
=========================================================================
실행 순서:
  Phase 0  쿨다운 확인 — /np/search page1 직접 접근 1회 (저비용)
  Phase 1  검색창 타이핑 → Enter
  Phase 2  바닥 스크롤 → '다음 페이지' 클릭 반복 (요청 캡처로 검색 API 판별)
  Phase 3  (선택 --sweep) 페이지 간 딜레이 스윕

Usage:
  xvfb-run -a python coupang_search_poc4.py --keyword 에어프라이어 \
      --pages 5 --delay 20 [--sweep]
"""
import argparse
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
SEARCH_URL = "https://www.coupang.com/np/search?q={q}&page=1"
PRODUCT_LINK_RE = re.compile(r"/vp/products/(\d+)")
BLOCK_KEYWORDS = ["자동화된 테스트 소프트웨어", "접근이 제한", "비정상적인 접근",
                  "보안 절차", "확인 절차", "Access Denied", "captcha", "사용권한"]
ts = datetime.now().strftime("%Y%m%d_%H%M%S")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def is_blocked_html(html: str) -> tuple[bool, str]:
    low = html.lower()
    for kw in BLOCK_KEYWORDS:
        if kw.lower() in low:
            return True, kw
    if len(html.encode("utf-8", errors="ignore")) < 1500:
        return True, "소프트 블록"
    return False, ""


def product_ids(page) -> set[str]:
    try:
        ids = set()
        for a in page.query_selector_all('a[href*="/vp/products/"]'):
            m = PRODUCT_LINK_RE.search(a.get_attribute("href") or "")
            if m:
                ids.add(m.group(1))
        return ids
    except Exception:
        return set()


def natural_interaction(page, duration: float) -> None:
    end = time.monotonic() + duration
    while time.monotonic() < end:
        act = random.choice(["move", "scroll", "pause"])
        if act == "move":
            page.mouse.move(random.randint(100, 900), random.randint(100, 600))
            time.sleep(random.uniform(0.1, 0.4))
        elif act == "scroll":
            page.mouse.wheel(0, random.randint(80, 350))
            time.sleep(random.uniform(0.3, 0.9))
        else:
            time.sleep(random.uniform(0.8, 2.0))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyword", default="에어프라이어")
    ap.add_argument("--pages", type=int, default=5)
    ap.add_argument("--delay", type=float, default=20.0)
    ap.add_argument("--sweep", action="store_true",
                    help="딜레이 스윕: 10/20/30초 각각 --pages 페이지 (3개 세션 필요)")
    args = ap.parse_args()

    delays = [10.0, 20.0, 30.0] if args.sweep else [args.delay]
    grand = {"keyword": args.keyword, "sessions": []}

    from camoufox.sync_api import Camoufox

    for delay in delays:
        log("=" * 60)
        log(f"세션 시작 — 딜레이 {delay:.0f}초")
        session = {"delay": delay, "phase0": None, "pages": [], "blocked": False}
        captured = []

        with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
            page = browser.new_page()

            def on_request(req, _cap=captured):
                u = req.url
                if "coupang" in u and ("search" in u.lower() or "n-api" in u) \
                        and not any(x in u for x in (".png", ".jpg", ".webp", ".css", ".js", ".woff")):
                    _cap.append({"method": req.method, "url": u[:300]})
            page.on("request", on_request)

            # Phase 0: 쿨다운 확인
            log("Phase 0: 쿨다운 확인 (page1 직접 접근)...")
            page.goto(SEARCH_URL.format(q=args.keyword), wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)
            html = page.content()
            blocked, reason = is_blocked_html(html)
            n0 = len(product_ids(page))
            session["phase0"] = {"blocked": blocked, "reason": reason, "items": n0}
            if blocked:
                log(f"✗ 아직 차단 상태: {reason} — 오늘 실측 중단")
                session["blocked"] = True
                grand["sessions"].append(session)
                break
            log(f"  ✓ 차단 해제 확인 (page1 {n0}개)")

            # 홈 웜업
            log("웜업 20초...")
            page.goto(COUPANG_HOME, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
            natural_interaction(page, 20)

            # Phase 1: 검색창 경로
            log("Phase 1: 검색창 타이핑 → Enter...")
            si = None
            for sel in ('input[name="q"]', '.headerSearchKeyword', '.coupang-search'):
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
                session["blocked"] = True
                grand["sessions"].append(session)
                break
            si.click()
            time.sleep(0.6)
            si.type(args.keyword, delay=random.randint(90, 220))
            time.sleep(random.uniform(0.6, 1.2))
            page.keyboard.press("Enter")
            page.wait_for_load_state("domcontentloaded", timeout=30000)

            all_ids = set()
            for _ in range(12):
                all_ids = product_ids(page)
                if all_ids:
                    break
                time.sleep(1.5)
            if not all_ids:
                html = page.content()
                blocked, reason = is_blocked_html(html)
                log(f"✗ 검색 결과 0건 (blocked={blocked}, {reason})")
                session["blocked"] = blocked
                session["block_reason"] = reason
                (OUTPUT_DIR / f"poc4_{ts}_d{int(delay)}_fail.html").write_text(html, encoding="utf-8")
                grand["sessions"].append(session)
                break
            log(f"  ✓ 초기 {len(all_ids)}개")
            session["pages"].append({"page": 1, "fresh": len(all_ids), "unique": len(all_ids)})

            # Phase 2: 페이지네이션 클릭
            for pno in range(2, args.pages + 1):
                time.sleep(max(2.0, random.uniform(delay * 0.8, delay * 1.2)))
                natural_interaction(page, random.uniform(3.0, 6.0))
                # 바닥까지 스크롤 (페이지네이션 노출)
                for _ in range(5):
                    page.keyboard.press("End")
                    page.mouse.wheel(0, random.randint(800, 1600))
                    time.sleep(random.uniform(0.6, 1.2))

                captured.clear()
                clicked = False
                for sel in ('a.move.next', 'a[aria-label="다음 페이지 보기"]', 'a:has-text("다음 페이지")'):
                    try:
                        el = page.query_selector(sel)
                        if el and el.is_visible():
                            el.click()
                            clicked = True
                            break
                    except Exception:
                        continue
                if not clicked:
                    log(f"✗ page{pno}: 다음 페이지 버튼 없음 — 종료")
                    session["pages"].append({"page": pno, "status": "no_next_button"})
                    break
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                time.sleep(random.uniform(3.0, 5.0))

                ids = set()
                for _ in range(8):
                    ids = product_ids(page)
                    if ids:
                        break
                    time.sleep(1.5)
                html = page.content()
                blocked, reason = is_blocked_html(html)
                if blocked:
                    log(f"✗ page{pno} 차단: {reason} — 즉시 중단")
                    session["blocked"] = True
                    session["block_reason"] = f"page{pno}: {reason}"
                    break
                fresh = ids - all_ids
                all_ids |= ids
                api_hits = [r for r in captured if "n-api" in r["url"] or "search" in r["url"].lower()][:8]
                log(f"  page{pno}: 신규 {len(fresh)}, 누적 {len(all_ids)}")
                for r in api_hits:
                    log(f"    {r['method']} {r['url'][:140]}")
                session["pages"].append({
                    "page": pno, "fresh": len(fresh), "unique": len(all_ids),
                    "api": api_hits, "url": page.url[:150],
                })
                if not ids:
                    (OUTPUT_DIR / f"poc4_{ts}_d{int(delay)}_p{pno}.html").write_text(html, encoding="utf-8")
                    log(f"  상품 0건 — HTML 저장")
                    break

        session["total_unique"] = len(all_ids)
        grand["sessions"].append(session)
        if session["blocked"]:
            log("차단 발생 — 이후 세션 취소")
            break
        if delay != delays[-1]:
            gap = random.uniform(900, 1200)
            log(f"세션 간 쿨다운 {gap/60:.0f}분...")
            time.sleep(gap)

    out = OUTPUT_DIR / f"poc4_{ts}.json"
    out.write_text(json.dumps(grand, ensure_ascii=False, indent=2), encoding="utf-8")
    log("=" * 60)
    log(f"완료 — 리포트: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
