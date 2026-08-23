"""
Coupang 검색 PoC v20 — 로그인 세션에서 SRP page≥2 검증
=====================================================
전제: coupang_login_setup.py 로 사용자가 직접 로그인 완료 (영속 프로필).

검증:
  1) SRP page1 — disableFixedPagination 플래그·페이저 렌더 여부 (비로그인 대비)
  2) 페이저/상품 확인 시 ?page=2, ?page=3 로드 — 신규 상품 수 측정
  3) 전부 저장 (HTML 증거) — 판정 후 문서화

규율: 내비게이션 ≤4, 간격 20~30초, 차단 감지 즉시 중단.
주의: 로그인 세션은 계정과 연결됨 — 이상 징후 시 중단이 최우선.
"""
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

KEYWORD = "에어프라이어"
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
PROFILE_DIR = BASE_DIR / "runtime_profile"
BLOCK_MARKER = OUTPUT_DIR / "last_block_ts.txt"
ts = datetime.now().strftime("%Y%m%d_%H%M%S")

DISABLE_RE = re.compile(r'disableFixedPagination\\?":(true|false)')
report: dict = {"started_at": ts, "keyword": KEYWORD, "logged_in_session": True,
                "loads": []}
seen_ids: set[str] = set()


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def is_blocked(status: int | None, html: str) -> bool:
    if status == 403:
        return True
    return ("사용권한" in html) or ("error403" in html)


def analyze(html: str) -> dict:
    flag = DISABLE_RE.search(html)
    ids = set(re.findall(r'/vp/products/(\d+)', html))
    new = ids - seen_ids
    seen_ids.update(ids)
    return {
        "unique_products": len(ids),
        "new_products": len(new),
        "kb": len(html) // 1024,
        "flag": flag.group(1) if flag else "?",
        "pager_present": ("srp_paginationBar" in html) or ('data-page="' in html),
        "search_count": (re.search(r'searchCount\\?":(\d+)', html) or [None, None])[1]
        if re.search(r'searchCount\\?":(\d+)', html) else None,
    }


def main() -> int:
    from camoufox.sync_api import Camoufox

    if not PROFILE_DIR.exists():
        log("영속 프로필 없음 — coupang_login_setup.py 를 먼저 실행하세요.")
        return 3

    with Camoufox(persistent_context=True, user_data_dir=str(PROFILE_DIR),
                  headless=False, geoip=True, locale="ko-KR",
                  humanize=True) as context:
        page = context.pages[0] if context.pages else context.new_page()

        def nav(url: str, name: str, first: bool = False) -> dict | None:
            if not first:
                time.sleep(random.uniform(20.0, 30.0))
            log(f"[{name}] → {url[:130]}")
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=40000)
            except Exception as e:  # noqa: BLE001
                report["loads"].append({"name": name, "error": str(e)[:150]})
                log(f"  실패: {e}")
                return None
            time.sleep(random.uniform(4.0, 6.0))
            html = page.content()
            code = resp.status if resp else None
            info = analyze(html)
            info.update({"name": name, "http": code, "url": page.url})
            report["loads"].append(info)
            (OUTPUT_DIR / f"poc20_{ts}_{name}.html").write_text(html, encoding="utf-8")
            log(f"  http={code} 상품={info['unique_products']}(신규{info['new_products']}) "
                f"flag={info['flag']} pager={info['pager_present']}")
            if is_blocked(code, html):
                log("  !! 차단/이상 감지 — 즉시 중단")
                BLOCK_MARKER.write_text(datetime.now().isoformat())
                report["aborted_at"] = name
                return None
            return info

        # 로그인 상태 확인
        page.goto("https://www.coupang.com/", wait_until="domcontentloaded", timeout=60000)
        cookies = {c["name"] for c in context.cookies()}
        report["cookie_names"] = sorted(cookies)
        if is_blocked(200, page.content()):
            log("홈에서 차단 — 중단")
            BLOCK_MARKER.write_text(datetime.now().isoformat())
            return _finish(2)
        logged = any(("token" in n.lower()) or n in ("wc", "WCK") for n in cookies)
        report["login_detected"] = logged
        log(f"로그인 감지: {logged} (쿠키 {len(cookies)}개)")
        if not logged:
            log("로그인 미감지 — 그래도 진행하되 결과 해석 주의")

        # 웜업
        end = time.monotonic() + 20
        while time.monotonic() < end:
            page.mouse.move(random.randint(100, 900), random.randint(100, 600))
            page.mouse.wheel(0, random.randint(50, 250))
            time.sleep(random.uniform(0.5, 1.5))

        # 1) SRP page1
        r = nav(f"https://www.coupang.com/np/search?q={quote(KEYWORD)}",
                "srp_page1", first=True)
        if r is None:
            return _finish(2)

        # 2) page2/3
        for pn in (2, 3):
            r = nav(f"https://www.coupang.com/np/search?q={quote(KEYWORD)}&page={pn}",
                    f"srp_page{pn}")
            if r is None:
                break
            if r["unique_products"] == 0:
                log(f"page{pn} 빈 결과 — page≥2 미제공 상태 지속")
                break

    return _finish(0)


def _finish(code: int) -> int:
    report["total_unique"] = len(seen_ids)
    out = OUTPUT_DIR / f"poc20_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    log("=" * 62)
    for p in report["loads"]:
        log(f"  {p.get('name','?'):12} http={p.get('http')} 상품={p.get('unique_products',0):4} "
            f"신규={p.get('new_products',0):4} flag={p.get('flag','?')} pager={p.get('pager_present')}")
    log(f"총 고유 상품: {len(seen_ids)}")
    log(f"리포트: {out}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
