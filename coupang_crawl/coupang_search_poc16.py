"""
Coupang 검색 PoC v16 — SRP 페이지네이션 세션 신뢰도 실험 + 모바일 탐색
====================================================================
rev.19 확정: URL/RSC 파라미터로는 SRP page≥2 불가. 남은 경로는 세션 자체.
본 스크립트는 두 가설을 검증:

  A) 세션 신뢰도: 현재 모든 실행이 fresh 프로필(쿠키 미축적) → 서버가
     disableFixedPagination=true 로 렌더. **영속 프로필**(user_data_dir)로
     쿠키·신뢰 점수를 축적하면 플래그가 바뀌는지 측정.
     (프로필은 이후 실행에서 재사용 — 축적 효과 관찰)
  B) AB 옵션 판독: n-api/abtest/options 응답에 페이지네이션 관련 플래그가
     있는지 확인 — 우리 세션 배정 버킷 파악.
  C) 모바일 도메인: m.coupang.com 검색은 별도 프론트엔드일 수 있음 —
     page=2 동작 여부 1회 확인.

규율: 세션 1회, 로드 ≤6회, 딜레이 15~20초, 차단 즉시 중단.
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
PROFILE_DIR = BASE_DIR / "runtime_profile"  # 재사용 영속 프로필

COUPANG_HOME = "https://www.coupang.com/"
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
DISABLE_PAGINATION_RE = re.compile(r'disableFixedPagination\\?":(true|false)')


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def count_cards(html: str) -> int:
    return len(re.findall(r'<li class="ProductUnit_productUnit', html))


def blocked(html: str) -> bool:
    low = html.lower()
    return "사용권한" in html or "access denied" in low


def main() -> int:
    keyword = sys.argv[1] if len(sys.argv) > 1 else "뷰티"
    q = quote(keyword)
    log(f"PoC v16 — '{keyword}' 세션 신뢰도 실험 (영속 프로필: {PROFILE_DIR})")
    report = {"keyword": keyword, "loads": []}

    from camoufox.sync_api import Camoufox
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with Camoufox(persistent_context=True,
                  user_data_dir=str(PROFILE_DIR),
                  headless=False, geoip=True, locale="ko-KR",
                  humanize=True) as context:
        page = context.pages[0] if context.pages else context.new_page()

        log("웜업 20초...")
        page.goto(COUPANG_HOME, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        end = time.monotonic() + 20
        while time.monotonic() < end:
            page.mouse.move(random.randint(100, 900), random.randint(100, 600))
            page.mouse.wheel(0, random.randint(50, 250))
            time.sleep(random.uniform(0.5, 1.5))

        # B) AB 옵션 판독 (내비게이션 없음)
        log("[AB options] → n-api/abtest/options")
        try:
            ab = page.evaluate(
                "async () => { const r = await fetch('/n-api/abtest/options', "
                "{credentials:'include'}); const t = await r.text(); "
                "return {status: r.status, len: t.length, head: t.slice(0, 2000)}; }")
            log(f"  status={ab.get('status')} len={ab.get('len')}")
            log(f"  head: {ab.get('head', '')[:800]}")
            report["ab_options"] = ab
        except Exception as e:  # noqa: BLE001
            log(f"  실패: {type(e).__name__}: {e}")
            report["ab_options"] = {"error": str(e)}
        time.sleep(random.uniform(14, 19))

        def load_and_measure(url: str, name: str) -> dict:
            log(f"[{name}] → {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                log(f"  로드 실패: {type(e).__name__}: {e}")
                return {"name": name, "error": str(e)}
            time.sleep(random.uniform(3.0, 5.0))
            html = page.content()
            cards = count_cards(html)
            is_blocked = blocked(html)
            pager = len(re.findall(r'Pagination_|data-page=', html)) > 0
            m = DISABLE_PAGINATION_RE.search(html)
            flag = m.group(1) if m else "?"
            log(f"  카드 {cards}개 / {len(html) // 1024}KB / 차단={is_blocked} "
                f"/ pager={pager} / disableFixedPagination={flag}")
            safe = re.sub(r"[^a-z0-9]", "_", name)[:30]
            (OUTPUT_DIR / f"poc16_{ts}_{safe}.html").write_text(html, encoding="utf-8")
            result = {"name": name, "url": url[:200], "cards": cards,
                      "blocked": is_blocked, "pager": pager,
                      "disableFixedPagination": flag}
            report["loads"].append(result)
            time.sleep(random.uniform(14, 19))
            return result

        # A) 기준: page1 플래그 관찰 (영속 프로필 상태에서)
        r = load_and_measure(f"https://www.coupang.com/np/search?q={q}", "page1")
        if r.get("cards", 0) == 0:
            log("✗ 기본 검색 실패 — 중단")
            return 2

        # page2 직접 접근 (영속 프로필 상태에서 재확인)
        load_and_measure(f"https://www.coupang.com/np/search?q={q}&page=2", "page2")

        # C) 모바일 도메인 탐색
        load_and_measure("https://m.coupang.com/", "mobile_home")
        load_and_measure(f"https://m.coupang.com/np/search?q={q}", "mobile_search_p1")

    out = OUTPUT_DIR / f"poc16_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log("=" * 60)
    for p in report["loads"]:
        log(f"  {p.get('name', ''):20} 카드 {p.get('cards', 0):3d}개 / "
            f"pager={p.get('pager')} / flag={p.get('disableFixedPagination')}")
    log(f"리포트: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
