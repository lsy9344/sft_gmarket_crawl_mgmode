"""
Coupang 검색 PoC v18 — 쿨다운 후 SRP page≥2 돌파 검증
=====================================================
rev.23 차단 이벤트(2026-08-22 01:22) 이후 쿨다운 전용 실행.

안전장치:
  - BLOCK_MARKER 파일 기준 12시간 미만이면 실행 거부(--force 로만Override)
  - 스모크 게이트: 홈 로드 403/차단 마커면 즉시 종료(추가 요청 0)
  - 내비게이션 간 18~25초(차단 이전보다 강화), 총 로드 ≤7

검증 큐(우선순위):
  P0  홈 스모크 + 쿠키 인벤토리
  P1  가설 L — 무JS 컨텍스트: /np/search page1 → page2 (레거시 SSR 가설)
  P2  가설 K — component={cat}&q={kw}&page=2 (카테고리+키워드 조합)
  P3  SRP page1 플래그 재관측 + abtest/options 덤프
  P4  미확보 JS 청크 CDN fetch (disableFixedPagination 소비자 모듈 탐색)
"""
import json
import random
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

KEYWORD = "에어프라이어"
CATEGORY = "176522"
COOLDOWN_HOURS = 12.0

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
PROFILE_DIR = BASE_DIR / "runtime_profile"
BLOCK_MARKER = OUTPUT_DIR / "last_block_ts.txt"
ts = datetime.now().strftime("%Y%m%d_%H%M%S")

DISABLE_RE = re.compile(r'disableFixedPagination\\?":(true|false)')
# 주의: "bazadebezolkohpepadr" 는 차단 마커가 아님 — 모든 정상 페이지에
# 포함되는 Akamai 센서 부트스트랩 (rev.23 스모크 오탐 교훈).
BLOCK_MARKERS = ("사용권한이 없습니다", "Access Denied", "access denied",
                 'id="error403"')
report: dict = {"started_at": ts, "keyword": KEYWORD, "loads": []}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def is_blocked_html(html: str) -> bool:
    low = html.lower()
    return any(m in html or m in low for m in BLOCK_MARKERS)


def analyze(html: str) -> dict:
    hrefs = set(re.findall(r'/vp/products/(\d+)', html))
    flag = DISABLE_RE.search(html)
    return {
        "cards": len(re.findall(r'<li class="ProductUnit_productUnit', html)),
        "unique_products": len(hrefs),
        "kb": len(html) // 1024,
        "flag": flag.group(1) if flag else "?",
        "blocked": is_blocked_html(html),
        "no_result": "검색결과가 없습니다" in html,
        "legacy_paging": ("search-pagination" in html) or ("product-list-paging" in html),
        "legacy_productList": 'id="productList"' in html,
    }


def check_cooldown(force: bool) -> bool:
    if not BLOCK_MARKER.exists():
        return True
    try:
        last = datetime.fromisoformat(BLOCK_MARKER.read_text().strip())
    except ValueError:
        return True
    elapsed = datetime.now() - last
    need = timedelta(hours=COOLDOWN_HOURS)
    if elapsed < need and not force:
        log(f"쿨다운 미충족: 차단 {elapsed} 전, 필요 {need}. --force 로 무시 가능.")
        return False
    log(f"쿨다운 충족 ({elapsed} 경과)")
    return True


def main() -> int:
    if not check_cooldown("--force" in sys.argv):
        return 3

    from camoufox.sync_api import Camoufox

    # ============ Part 1: 무JS 컨텍스트 (가설 L) — 별도 브라우저 ============
    log("Part 1 — 무JS 브라우저로 레거시 SSR 탐색 (가설 L)")
    with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
        try:
            nojs = browser.new_context(java_script_enabled=False)
        except Exception as e:  # noqa: BLE001
            log(f"무JS 컨텍스트 생성 실패: {e}")
            report["nojs_error"] = str(e)
            nojs = None

        def nojs_load(url: str, name: str, first: bool = False) -> dict | None:
            if not nojs:
                return None
            if not first:
                time.sleep(random.uniform(18.0, 25.0))
            pg = nojs.pages[0] if nojs.pages else nojs.new_page()
            log(f"[{name}] → {url[:130]}")
            try:
                resp = pg.goto(url, wait_until="domcontentloaded", timeout=40000)
                time.sleep(3.0)
                html = pg.content()
            except Exception as e:  # noqa: BLE001
                report["loads"].append({"name": name, "error": str(e)[:150]})
                log(f"  실패: {e}")
                return None
            info = analyze(html)
            info.update({"name": name, "http": resp.status if resp else None,
                         "final_url": pg.url})
            report["loads"].append(info)
            (OUTPUT_DIR / f"poc18_{ts}_{name}.html").write_text(html, encoding="utf-8")
            log(f"  http={info['http']} kb={info['kb']} 상품={info['unique_products']} "
                f"legacy_paging={info['legacy_paging']} no_result={info['no_result']}")
            if info["blocked"] or info["http"] == 403:
                log("  !! 차단 — 전체 중단")
                BLOCK_MARKER.write_text(datetime.now().isoformat())
                report["aborted_at"] = name
                return None
            return info

        # 스모크 게이트: 무JS 홈 로드
        r = nojs_load("https://www.coupang.com/", "smoke_home_nojs", first=True)
        if r is None:
            log("스모크 실패 — 종료")
            return _finish(2)
        time.sleep(random.uniform(18.0, 25.0))
        r = nojs_load(f"https://www.coupang.com/np/search?q={quote(KEYWORD)}",
                      "nojs_srp_p1")
        if r and r["unique_products"] > 0:
            nojs_load(f"https://www.coupang.com/np/search?q={quote(KEYWORD)}&page=2",
                      "nojs_srp_p2")
        if nojs:
            nojs.close()

    # ============ Part 2: 영속 프로필 일반 세션 — K/AB/플래그/청크 ============
    log("Part 2 — 영속 프로필 세션 (가설 K + AB + 플래그)")
    with Camoufox(persistent_context=True, user_data_dir=str(PROFILE_DIR),
                  headless=False, geoip=True, locale="ko-KR",
                  humanize=True) as context:
        page = context.pages[0] if context.pages else context.new_page()

        def nav(url: str, name: str) -> dict | None:
            time.sleep(random.uniform(18.0, 25.0))
            log(f"[{name}] → {url[:130]}")
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=40000)
            except Exception as e:  # noqa: BLE001
                report["loads"].append({"name": name, "error": str(e)[:150]})
                return None
            time.sleep(random.uniform(3.5, 5.5))
            html = page.content()
            info = analyze(html)
            info.update({"name": name, "http": resp.status if resp else None,
                         "url": page.url})
            report["loads"].append(info)
            (OUTPUT_DIR / f"poc18_{ts}_{name}.html").write_text(html, encoding="utf-8")
            log(f"  http={info['http']} 상품={info['unique_products']} flag={info['flag']} "
                f"no_result={info['no_result']}")
            if info["blocked"] or info["http"] == 403:
                log("  !! 차단 — 전체 중단")
                BLOCK_MARKER.write_text(datetime.now().isoformat())
                report["aborted_at"] = name
                return None
            return info

        # 홈 웜업 + 쿠키 인벤토리
        page.goto("https://www.coupang.com/", wait_until="domcontentloaded", timeout=40000)
        if is_blocked_html(page.content()):
            log("홈에서 차단 — 중단")
            BLOCK_MARKER.write_text(datetime.now().isoformat())
            return _finish(2)
        end = time.monotonic() + 20
        while time.monotonic() < end:
            page.mouse.move(random.randint(100, 900), random.randint(100, 600))
            page.mouse.wheel(0, random.randint(50, 250))
            time.sleep(random.uniform(0.5, 1.5))
        try:
            cookies = context.cookies()
            report["cookies"] = [{"name": c["name"], "domain": c["domain"],
                                  "vlen": len(c["value"])} for c in cookies]
            log(f"쿠키 {len(cookies)}개: {sorted(c['name'] for c in cookies)}")
        except Exception as e:  # noqa: BLE001
            report["cookies_error"] = str(e)

        # P3: SRP page1 플래그 (영속 프로필 2회차)
        r = nav(f"https://www.coupang.com/np/search?q={quote(KEYWORD)}", "srp_page1")
        if r is None:
            return _finish(2)

        try:
            ab = page.evaluate(
                "async () => { const r = await fetch('/n-api/abtest/options', "
                "{method:'GET', credentials:'include'}); "
                "return {status: r.status, text: await r.text()}; }")
            report["ab_options_get"] = {"status": ab["status"], "text": ab["text"][:4000]}
            log(f"AB options: status={ab['status']} len={len(ab['text'])}")
        except Exception as e:  # noqa: BLE001
            report["ab_options_get"] = {"error": str(e)}

        # P2: 가설 K — component + q + page
        nav(f"https://www.coupang.com/np/search?component={CATEGORY}"
            f"&q={quote(KEYWORD)}", "srp_component_q_p1")
        nav(f"https://www.coupang.com/np/search?component={CATEGORY}"
            f"&q={quote(KEYWORD)}&page=2", "srp_component_q_p2")

        # P4: 미확보 청크
        try:
            html = page.content()
            urls = sorted(set(re.findall(
                r'(https://assets\.coupangcdn\.com/[^"\\]+?/static/chunks/[^"\\]+?\.js)',
                html)))
            (OUTPUT_DIR / "srp_chunks").mkdir(exist_ok=True)
            got = []
            for u in urls:
                name = u.rsplit("/", 1)[-1]
                if (OUTPUT_DIR / "srp_chunks" / name).exists():
                    continue
                try:
                    r2 = page.evaluate(
                        "async (u) => { const r = await fetch(u); "
                        "return {status: r.status, text: await r.text()}; }", u)
                    if r2["status"] == 200 and len(r2["text"]) > 1000:
                        (OUTPUT_DIR / "srp_chunks" / name).write_text(r2["text"],
                                                                      encoding="utf-8")
                        got.append(name)
                        time.sleep(random.uniform(0.4, 0.9))
                except Exception:  # noqa: BLE001
                    pass
            report["chunks_downloaded"] = got
            log(f"청크 확보: {len(got)}개")
        except Exception as e:  # noqa: BLE001
            report["chunks_error"] = str(e)

    return _finish(0)


def _finish(code: int) -> int:
    out = OUTPUT_DIR / f"poc18_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    log("=" * 62)
    for p in report["loads"]:
        log(f"  {p.get('name','?'):22} http={p.get('http')} 상품={p.get('unique_products',0):4} "
            f"flag={p.get('flag','?')} legacy_paging={p.get('legacy_paging')}")
    log(f"리포트: {out}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
