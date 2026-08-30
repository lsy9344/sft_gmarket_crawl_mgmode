"""
Coupang 검색 페이지 크롤링 PoC (타당성 실측)
================================================
목적: /np/search?q={키워드}&page={N} 을 기존 Camoufox 파이프라인으로
      접근했을 때 차단 없이 몇 페이지까지 수집 가능한지 실측한다.

원칙:
  - 차단 감지 시 즉시 중단 (밀어붙이지 않는다)
  - 페이지 간 랜덤 딜레이 (기본 15~30초)
  - 각 페이지에서 자연스러운 스크롤/마우스 행동
  - 결과를 JSON + 실측 리포트로 저장

Usage:
    xvfb-run -a python coupang_search_poc.py \
        --keyword 에어프라이어 --max-pages 3 \
        --delay-min 15 --delay-max 30
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
SEARCH_URL = "https://www.coupang.com/np/search?q={q}&page={page}"

BLOCK_KEYWORDS = [
    "자동화된 테스트 소프트웨어",
    "접근이 제한",
    "비정상적인 접근",
    "보안 절차",
    "확인 절차",
    "Access Denied",
    "captcha",
    "사용권한",
]

PRODUCT_LINK_RE = re.compile(r"/vp/products/(\d+)")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def natural_interaction(page, duration: float) -> None:
    """웜업/페이지 열람 중 자연스러운 행동 시뮬레이션 (v3 엔진 패턴 재사용)"""
    end_time = time.monotonic() + duration
    while time.monotonic() < end_time:
        action = random.choice(["move", "scroll", "pause", "move", "scroll"])
        if action == "move":
            page.mouse.move(random.randint(100, 900), random.randint(100, 600))
            time.sleep(random.uniform(0.1, 0.4))
        elif action == "scroll":
            page.mouse.wheel(0, random.randint(80, 350))
            time.sleep(random.uniform(0.3, 0.9))
        else:
            time.sleep(random.uniform(0.8, 2.0))


def detect_block(page, html: str) -> tuple[bool, str]:
    """차단 여부 감지 — 상태 키워드 + 소프트 블록(빈 응답)"""
    for kw in BLOCK_KEYWORDS:
        if kw.lower() in html.lower():
            return True, f"차단 키워드: {kw}"
    if len(html.encode("utf-8", errors="ignore")) < 1500:
        return True, f"소프트 블록 (응답 {len(html)}자)"
    return False, ""


def parse_products(page, html: str) -> list[dict]:
    """검색 결과 파싱 — /vp/products/ 링크 기준, 상품명 셀렉터 매칭"""
    items: dict[str, dict] = {}

    try:
        links = page.query_selector_all('a[href*="/vp/products/"]')
        for a in links:
            href = a.get_attribute("href") or ""
            m = PRODUCT_LINK_RE.search(href)
            if not m:
                continue
            pid = m.group(1)
            if pid in items:
                continue
            # 신형 SRP: class 이름이 해시되어 있으므로 부분 매칭
            name_el = a.query_selector('[class*="productName"]')
            title = ""
            if name_el:
                title = name_el.inner_text().strip()
            else:
                txt = a.inner_text().strip()
                if 5 < len(txt) < 200:
                    title = txt
            items[pid] = {"product_id": pid, "title": title[:120], "url": href}
    except Exception:
        pass

    # 폴백 — HTML 전체에서 상품 링크 추출
    if not items:
        for m in PRODUCT_LINK_RE.finditer(html):
            pid = m.group(1)
            items.setdefault(pid, {"product_id": pid, "title": "", "url": ""})

    return list(items.values())


def main() -> int:
    parser = argparse.ArgumentParser(description="Coupang search PoC")
    parser.add_argument("--keyword", default="에어프라이어")
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--delay-min", type=float, default=15.0)
    parser.add_argument("--delay-max", type=float, default=30.0)
    parser.add_argument("--warmup-time", type=float, default=20.0)
    args = parser.parse_args()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = OUTPUT_DIR / f"search_poc_{ts}.json"

    log("=" * 60)
    log(f"Coupang 검색 PoC — keyword='{args.keyword}' pages={args.max_pages} "
        f"delay={args.delay_min}~{args.delay_max}s")
    log("=" * 60)

    report = {
        "started_at": ts,
        "keyword": args.keyword,
        "max_pages": args.max_pages,
        "delay_range": [args.delay_min, args.delay_max],
        "pages": [],
        "blocked": False,
        "block_reason": None,
        "finished_at": None,
    }

    try:
        from camoufox.sync_api import Camoufox
    except ImportError:
        log("Camoufox 미설치 — SellerCollector.exe --setup-runtime 필요")
        return 1

    with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
        page = browser.new_page()

        # Phase 1: 홈 웜업
        log("Phase 1: 홈 웜업...")
        page.goto(COUPANG_HOME, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        natural_interaction(page, args.warmup_time)
        home_html = page.content()
        blocked, reason = detect_block(page, home_html)
        if blocked:
            log(f"✗ 웜업 단계에서 차단: {reason}")
            report["blocked"], report["block_reason"] = True, f"warmup: {reason}"
            _save(report, report_path)
            return 2
        log("  웜업 통과 (Akamai 검증 OK)")

        # Phase 2: 검색 페이지네이션
        #   1페이지: URL 직접 로드, 2페이지부터: '다음 페이지' 링크 클릭 우선
        #   (SPA 렌더링 대응 + 실제 사용자 행동에 가장 가까움)
        for page_no in range(1, args.max_pages + 1):
            t0 = time.monotonic()
            try:
                if page_no == 1:
                    url = SEARCH_URL.format(q=args.keyword, page=page_no)
                    log(f"Phase 2.{page_no}: 검색 페이지 1 로드 → {url}")
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(random.uniform(3.0, 5.0))
                else:
                    log(f"Phase 2.{page_no}: '다음 페이지' 링크 클릭...")
                    clicked = _click_next_page(page)
                    if not clicked:
                        url = SEARCH_URL.format(q=args.keyword, page=page_no)
                        log(f"  다음 링크 없음 — URL 직접 로드: {url}")
                        page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(random.uniform(4.0, 6.0))

                natural_interaction(page, random.uniform(6.0, 9.0))
                html = page.content()
            except Exception as e:
                log(f"✗ 페이지 로드 실패: {type(e).__name__}: {e}")
                report["pages"].append({"page": page_no, "status": "load_error", "error": str(e)})
                break

            blocked, reason = detect_block(page, html)
            if blocked:
                log(f"✗ 페이지 {page_no}에서 차단 감지: {reason} — 즉시 중단")
                report["blocked"], report["block_reason"] = True, f"page{page_no}: {reason}"
                report["pages"].append({"page": page_no, "status": "blocked", "reason": reason})
                break

            items = parse_products(page, html)
            elapsed = time.monotonic() - t0
            final_url = page.url
            log(f"  ✓ 페이지 {page_no}: 상품 {len(items)}개, {elapsed:.1f}초, "
                f"HTML {len(html)//1024}KB, url={final_url[:80]}")
            report["pages"].append({
                "page": page_no,
                "status": "ok",
                "items": len(items),
                "elapsed_sec": round(elapsed, 1),
                "html_kb": len(html) // 1024,
                "final_url": final_url,
            })

            # 모든 페이지 HTML 저장 (진단용)
            debug_path = OUTPUT_DIR / f"search_poc_{ts}_page{page_no}.html"
            debug_path.write_text(html, encoding="utf-8")
            if page_no == 1:
                sample_path = OUTPUT_DIR / f"search_poc_{ts}_page1_sample.json"
                sample_path.write_text(
                    json.dumps(items[:20], ensure_ascii=False, indent=2), encoding="utf-8")
                log(f"  샘플 저장: {sample_path.name} ({len(items[:20])}건)")

            if page_no < args.max_pages:
                delay = random.uniform(args.delay_min, args.delay_max)
                log(f"  딜레이 {delay:.0f}초...")
                time.sleep(delay)

    report["finished_at"] = datetime.now().strftime("%Y%m%d_%H%M%S")
    ok_pages = [p for p in report["pages"] if p["status"] == "ok"]
    report["summary"] = {
        "pages_ok": len(ok_pages),
        "total_items": sum(p.get("items", 0) for p in ok_pages),
    }
    _save(report, report_path)

    log("=" * 60)
    log(f"결과: {len(ok_pages)}/{args.max_pages} 페이지 성공, "
        f"총 {report['summary']['total_items']}건, 차단={'있음' if report['blocked'] else '없음'}")
    log(f"리포트: {report_path}")
    return 0 if ok_pages else 2


def _click_next_page(page) -> bool:
    """'다음 페이지 보기' 페이지네이션 링크 클릭 성공 여부"""
    selectors = [
        'a[aria-label="다음 페이지 보기"]',
        'a.move.next',
        'a:has-text("다음 페이지")',
    ]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                return True
        except Exception:
            continue
    return False


def _save(report: dict, path: Path) -> None:
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
