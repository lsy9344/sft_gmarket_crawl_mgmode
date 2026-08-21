"""
Coupang 검색 PoC v17 — SRP page≥2 돌파 종합 실험 (영속 프로필 + 미시도 경로)
============================================================================
rev.19 확정 이후 남은 경로 + 신규 가설을 1세션 규율 내(≤8 내비게이션)에서 검증:

  A) 영속 프로필(user_data_dir) — 쿠키/신뢰 축적 시작 (poc16 미실행분)
  B) AB 옵션 판독 — /n-api/abtest/options 전체 응답
  K) 카테고리+키워드 조합 — /np/search?component={cat}&q={kw}&page=2 (미시도 조합)
  L) 레거시 프론트엔드 — JS 비활성 컨텍스트로 /np/search 로드
     (엣지가 무JS 클라이언트에게 레거시 SSR을 주는 가설)
  M) 남은 JS 청크 확보 — disableFixedPagination 소비자 모듈 오프라인 분석용
  C) 모바일 도메인 — m.coupang.com (poc16 미실행분)

규율: 내비게이션 간 15~20초, 403/차단 키워드 감지 시 즉시 중단.
"""
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

KEYWORD = "에어프라이어"
CATEGORY = "176522"  # 뷰티 (히스토리 실측 카테고리)

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
PROFILE_DIR = BASE_DIR / "runtime_profile"
ts = datetime.now().strftime("%Y%m%d_%H%M%S")

DISABLE_RE = re.compile(r'disableFixedPagination\\?":(true|false)')
report: dict = {"started_at": ts, "keyword": KEYWORD, "loads": []}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def analyze(html: str) -> dict:
    cards = len(re.findall(r'<li class="ProductUnit_productUnit', html))
    hrefs = set(re.findall(r'/vp/products/(\d+)', html))
    flag = DISABLE_RE.search(html)
    blocked = ("사용권한" in html) or ("access denied" in html.lower())
    no_result = "검색결과가 없습니다" in html
    return {
        "cards": cards, "unique_products": len(hrefs),
        "kb": len(html) // 1024,
        "flag": flag.group(1) if flag else "?",
        "blocked": blocked, "no_result": no_result,
    }


def main() -> int:
    import sys
    sys.path.insert(0, str(BASE_DIR.parent))
    from camoufox.sync_api import Camoufox

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    log(f"poc17 시작 — 영속 프로필: {PROFILE_DIR}")

    with Camoufox(persistent_context=True, user_data_dir=str(PROFILE_DIR),
                  headless=False, geoip=True, locale="ko-KR",
                  humanize=True) as context:
        page = context.pages[0] if context.pages else context.new_page()

        def nav(url: str, name: str, wait: tuple = (15.0, 20.0)) -> dict | None:
            if report["loads"]:
                time.sleep(random.uniform(*wait))
            log(f"[{name}] → {url[:150]}")
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=40000)
            except Exception as e:  # noqa: BLE001
                log(f"  로드 실패: {e}")
                report["loads"].append({"name": name, "error": str(e)[:150]})
                return None
            time.sleep(random.uniform(3.5, 5.5))
            html = page.content()
            info = analyze(html)
            info.update({"name": name, "http": resp.status if resp else None,
                         "url": page.url})
            report["loads"].append(info)
            safe = re.sub(r"[^a-z0-9]", "_", name)[:40]
            (OUTPUT_DIR / f"poc17_{ts}_{safe}.html").write_text(html, encoding="utf-8")
            log(f"  http={info['http']} 카드={info['cards']} 상품={info['unique_products']} "
                f"flag={info['flag']} no_result={info['no_result']} blocked={info['blocked']}")
            if info["blocked"] or info["http"] == 403:
                log("  !! 차단 감지 — 즉시 중단")
                report["aborted_at"] = name
                return None
            return info

        # ---- 웜업 ----
        page.goto("https://www.coupang.com/", wait_until="domcontentloaded", timeout=40000)
        end = time.monotonic() + 20
        while time.monotonic() < end:
            page.mouse.move(random.randint(100, 900), random.randint(100, 600))
            page.mouse.wheel(0, random.randint(50, 250))
            time.sleep(random.uniform(0.5, 1.5))
        log("웜업 완료")

        # ---- 기준: SRP page1 + 플래그 + 쿠키 + AB ----
        r = nav(f"https://www.coupang.com/np/search?q={quote(KEYWORD)}", "srp_page1")
        if not r or r["unique_products"] == 0:
            log("기본 검색 실패 — 중단")
            return 2

        try:
            cookies = context.cookies()
            report["cookies"] = [{"name": c["name"], "domain": c["domain"],
                                  "len": len(c["value"])} for c in cookies]
            log(f"쿠키 {len(cookies)}개: {sorted(c['name'] for c in cookies)}")
        except Exception as e:  # noqa: BLE001
            report["cookies_error"] = str(e)

        try:
            ab = page.evaluate(
                "async () => { const r = await fetch('/n-api/abtest/options', "
                "{method:'GET', credentials:'include'}); "
                "return {status: r.status, text: await r.text()}; }")
            report["ab_options_get"] = {k: (v[:3000] if isinstance(v, str) else v)
                                        for k, v in ab.items()}
            log(f"AB options GET: status={ab['status']} len={len(ab['text'])}")
        except Exception as e:  # noqa: BLE001
            report["ab_options_get"] = {"error": str(e)}

        # ---- M) 남은 JS 청크 확보 (페이지 컨텍스트에서 CDN fetch) ----
        try:
            html = page.content()
            chunk_urls = sorted(set(re.findall(
                r'(https://assets\.coupangcdn\.com/[^"\\]+?/static/chunks/[^"\\]+?\.js)',
                html)))
            got = []
            for u in chunk_urls:
                name = u.rsplit("/", 1)[-1]
                if (OUTPUT_DIR / "srp_chunks" / name).exists():
                    continue
                try:
                    resp = page.evaluate(
                        "async (u) => { const r = await fetch(u); "
                        "return {status: r.status, text: await r.text()}; }", u)
                    if resp["status"] == 200 and len(resp["text"]) > 1000:
                        (OUTPUT_DIR / "srp_chunks").mkdir(exist_ok=True)
                        (OUTPUT_DIR / "srp_chunks" / name).write_text(
                            resp["text"], encoding="utf-8")
                        got.append(f"{name}({len(resp['text'])//1024}KB)")
                        time.sleep(random.uniform(0.4, 0.9))
                except Exception:  # noqa: BLE001
                    pass
            report["chunks_downloaded"] = got
            log(f"청크 확보: {len(got)}개 {got}")
        except Exception as e:  # noqa: BLE001
            report["chunks_error"] = str(e)

        # ---- K) 카테고리+키워드 조합 (미시도) ----
        nav(f"https://www.coupang.com/np/search?component={CATEGORY}"
            f"&q={quote(KEYWORD)}", "srp_component_q")
        nav(f"https://www.coupang.com/np/search?component={CATEGORY}"
            f"&q={quote(KEYWORD)}&page=2", "srp_component_q_page2")

        # ---- L) JS 비활성 컨텍스트는 별도 브라우저로 (아래 Part 2) ----

        # ---- C) 모바일 도메인 ----
        for nm, u in (("mobile_home", "https://m.coupang.com/"),
                      ("mobile_search", f"https://m.coupang.com/nm/search?q={quote(KEYWORD)}")):
            r = nav(u, nm, wait=(15.0, 20.0))
            if r is None:
                break

    # ---- Part 2: JS 비활성 브라우저 — 레거시 SSR 가설 ----
    log("Part 2 — JS 비활성 브라우저로 레거시 SSR 탐색")
    with Camoufox(headless=False, geoip=True, locale="ko-KR",
                  humanize=True) as browser:
        try:
            nojs = browser.new_context(java_script_enabled=False)
            np_ = nojs.pages[0] if nojs.pages else nojs.new_page()
            for nm, u in (("nojs_p2", f"https://www.coupang.com/np/search?q={quote(KEYWORD)}&page=2"),
                          ("nojs_p1", f"https://www.coupang.com/np/search?q={quote(KEYWORD)}")):
                time.sleep(random.uniform(15.0, 20.0))
                try:
                    resp = np_.goto(u, wait_until="domcontentloaded", timeout=40000)
                    time.sleep(3.0)
                    html = np_.content()
                    info = analyze(html)
                    info.update({"name": nm, "http": resp.status if resp else None,
                                 "final_url": np_.url,
                                 "has_legacy_paging": ("search-pagination" in html)
                                                      or ("product-list-paging" in html),
                                 "has_productList": 'id="productList"' in html})
                    report["loads"].append(info)
                    (OUTPUT_DIR / f"poc17_{ts}_{nm}.html").write_text(html, encoding="utf-8")
                    log(f"  [{nm}] http={info['http']} kb={info['kb']} 상품={info['unique_products']} "
                        f"legacy_paging={info['has_legacy_paging']} final={np_.url[:100]}")
                    if info["http"] == 403 or info["blocked"]:
                        log("  !! 차단 감지 — nojs 중단")
                        break
                except Exception as e:  # noqa: BLE001
                    report["loads"].append({"name": nm, "error": str(e)[:150]})
                    log(f"  [{nm}] 실패: {e}")
            nojs.close()
        except Exception as e:  # noqa: BLE001
            report["nojs_error"] = str(e)
            log(f"nojs 컨텍스트 불가: {e}")

    out = OUTPUT_DIR / f"poc17_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    log("=" * 62)
    for p in report["loads"]:
        log(f"  {p.get('name','?'):22} http={p.get('http')} 상품={p.get('unique_products',0):4} "
            f"flag={p.get('flag','?')} blocked={p.get('blocked')}")
    log(f"리포트: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
