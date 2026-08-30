"""
Coupang 검색 PoC v19 — 비로그인 잔여 경로 마무리 검증
====================================================
rev.24 기준 미실행/미판정 잔여만 최소 요청으로 확인:

  C) 모바일 도메인 — m.coupang.com 홈 + nm/search (rev.24 "미실행 잔여")
  B) abtest/options POST — 페이지 자신이 보내는 POST를 그대로 재현해 응답 판독
     (rev.24에서 GET은 405)
  +  SRP page1 플래그/페이저 재관측 (영속 프로필 3회차 — 신뢰 축적 관찰)

규율: 내비게이션 4회, 간격 18~25초, 차단 감지(403/사용권한) 즉시 중단.
차단 판정 주의: `bazadebezolkohpepadr` 는 정상 센서 마커 — 차단 마커 아님.
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
report: dict = {"started_at": ts, "loads": []}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def is_blocked(status: int | None, html: str) -> bool:
    if status == 403:
        return True
    return ("사용권한" in html) or ("error403" in html)


def analyze(html: str) -> dict:
    flag = DISABLE_RE.search(html)
    return {
        "unique_products": len(set(re.findall(r'/vp/products/(\d+)', html))),
        "kb": len(html) // 1024,
        "flag": flag.group(1) if flag else "?",
        "pager_present": ("srp_paginationBar" in html) or ('data-page="' in html),
        "no_result": "검색결과가 없습니다" in html,
    }


def main() -> int:
    from camoufox.sync_api import Camoufox

    ab_posts: list[dict] = []

    with Camoufox(persistent_context=True, user_data_dir=str(PROFILE_DIR),
                  headless=False, geoip=True, locale="ko-KR",
                  humanize=True) as context:
        page = context.pages[0] if context.pages else context.new_page()

        def on_request(req):
            if "abtest/options" in req.url and req.method == "POST":
                ab_posts.append({"url": req.url, "post": req.post_data})

        page.on("request", on_request)

        def nav(url: str, name: str, first: bool = False) -> dict | None:
            if not first:
                time.sleep(random.uniform(18.0, 25.0))
            log(f"[{name}] → {url[:130]}")
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=40000)
            except Exception as e:  # noqa: BLE001
                report["loads"].append({"name": name, "error": str(e)[:150]})
                log(f"  실패: {e}")
                return None
            time.sleep(random.uniform(3.5, 5.5))
            html = page.content()
            code = resp.status if resp else None
            info = analyze(html)
            info.update({"name": name, "http": code, "url": page.url})
            report["loads"].append(info)
            (OUTPUT_DIR / f"poc19_{ts}_{name}.html").write_text(html, encoding="utf-8")
            log(f"  http={code} 상품={info['unique_products']} flag={info['flag']} "
                f"pager={info['pager_present']} no_result={info['no_result']}")
            if is_blocked(code, html):
                log("  !! 차단 감지 — 즉시 중단")
                BLOCK_MARKER.write_text(datetime.now().isoformat())
                report["aborted_at"] = name
                return None
            return info

        # 웜업
        page.goto("https://www.coupang.com/", wait_until="domcontentloaded", timeout=40000)
        if is_blocked(200, page.content()):
            log("홈에서 차단 — 중단")
            BLOCK_MARKER.write_text(datetime.now().isoformat())
            return _finish(2)
        end = time.monotonic() + 20
        while time.monotonic() < end:
            page.mouse.move(random.randint(100, 900), random.randint(100, 600))
            page.mouse.wheel(0, random.randint(50, 250))
            time.sleep(random.uniform(0.5, 1.5))
        log("웜업 완료")

        # 1) SRP page1 재관측 (영속 프로필 3회차)
        r = nav(f"https://www.coupang.com/np/search?q={quote(KEYWORD)}",
                "srp_page1", first=True)
        if r is None:
            return _finish(2)

        # 2) abtest POST 재현 (페이지가 보낸 본문 그대로)
        if ab_posts:
            report["ab_posts_seen"] = ab_posts[-3:]
            body = ab_posts[-1]["post"] or "{}"
            try:
                ab = page.evaluate(
                    """async (body) => {
                        const r = await fetch('/n-api/abtest/options', {
                            method: 'POST', credentials: 'include',
                            headers: {'Content-Type': 'application/json'},
                            body: body});
                        return {status: r.status, text: await r.text()};
                    }""", body)
                report["ab_post"] = {"sent": body[:500], **ab,
                                     "text": ab["text"][:3000]}
                log(f"AB POST: status={ab['status']} resp_len={len(ab['text'])}")
            except Exception as e:  # noqa: BLE001
                report["ab_post"] = {"error": str(e)}
        else:
            report["ab_posts_seen"] = []
            log("abtest POST 미관측")

        # 3) 모바일 도메인
        nav("https://m.coupang.com/", "mobile_home")
        nav(f"https://m.coupang.com/nm/search?q={quote(KEYWORD)}", "mobile_search")

    return _finish(0)


def _finish(code: int) -> int:
    out = OUTPUT_DIR / f"poc19_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    log("=" * 62)
    for p in report["loads"]:
        log(f"  {p.get('name','?'):16} http={p.get('http')} 상품={p.get('unique_products',0):4} "
            f"flag={p.get('flag','?')} pager={p.get('pager_present')}")
    log(f"리포트: {out}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
