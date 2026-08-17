"""
Coupang 검색 PoC v15 — SRP page≥2 돌파 재조사 (미시도 가설 7종)
================================================================
rev.9~10 확정(처럼 보였던 것): SRP(검색결과) page≥2 는 빈 셸(128KB)만 SSR.
그러나 rev.11 에서 PLP(카테고리)는 ?page=N 이 SSR 됨을 확인 — 'page≥2 전면 차단'은
거짓이었고 SRP 전용 게이트였음. SRP 게이트를 여는 조건이 아직 남아있을 가능성이 있어
미시도 조합을 순차 재검증한다 (사용자 요청: "다른 방면에서 다시 조사").

기시도 (재시도 안 함):
  ?page=2 fresh / 동일세션 / &channel=user / 사용자 traceId URL 그대로 → 전부 빈 셸

미시도 가설 (이번에 검증):
  T1  page=2 + searchId    — 페이지1 RSC 페이로드에 서버 발급 searchId 내장.
                             page2 서빙 조건이 searchId 일 가능성 (traceId 는 실패)
  T2  component=&page=2&listSize=72 — 공식 URL 스키마
      (/np/search?component=&q=..&page=N&listSize=72, PAGE_STRUCTURE.md §2) 미시도
  T3  listSize=36 + page=2 (+searchId) — 페이로드 props 에 listSize:36 내장.
                             60(기본) 은 '전체' 취급일 수 있음
  T4  sorter=scoreDesc&page=2 (+searchId) — 명시적 기본 정렬 + page
  T5  RSC flight fetch — Next.js 클라이언트 내비게이션 프로토콜(RSC:1 헤더)로
      페이지 안에서 fetch → SSR 게이트와 다른 경로일 수 있음
  T6  channel=user&component=&page=2&searchId — 채널+컴포넌트+서치ID 조합
  T7  PLP page=2 대조 — 세션 정상성 확인용 (성공 기대)

부가 측정:
  - 페이지1의 disableFixedPagination 플래그·페이지 번호 요소 존재 여부 기록
  - 전 로드의 네트워크 요청 캡처 — 클라이언트가 page 전환 시 호출하는
    내부 API/RSC 엔드포인트 존재 판별 (SEARCH_APPROACH_REVIEW §2 계획 이행)

규율: 세션 1회(5/5 중 1), 페이지 로드 총 8회, 로드 간 15~20초 딜레이,
      차단 감지 즉시 중단.
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

COUPANG_HOME = "https://www.coupang.com/"
ts = datetime.now().strftime("%Y%m%d_%H%M%S")

SEARCH_ID_RE = re.compile(r'searchId\\":\\"([a-z0-9]+)')
TRACE_ID_RE = re.compile(r'traceId\\":\\"([a-z0-9]+)')
DISABLE_PAGINATION_RE = re.compile(r'disableFixedPagination\\?":(true|false)')


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


def count_cards(html: str) -> int:
    return len(re.findall(r'<li class="ProductUnit_productUnit', html))


def blocked(html: str) -> bool:
    low = html.lower()
    return "사용권한" in html or "access denied" in low


def rsc_fetch(page, url: str) -> dict:
    """페이지 안에서 RSC flight fetch 실행 (내비게이션 없음)."""
    script = """
    async (url) => {
      try {
        const r = await fetch(url, {
          headers: { RSC: '1', 'Next-Router-State-Tree': '%5B%22%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%5D%7D%2Cnull%2Cnull%2Ctrue%5D',
                     'Next-Router-Prefetch': '0' },
          credentials: 'include',
        });
        const text = await r.text();
        return { status: r.status, len: text.length,
                 hasProducts: text.includes('ProductUnit') || text.includes('productList'),
                 head: text.slice(0, 300) };
      } catch (e) { return { error: String(e) }; }
    }
    """
    try:
        return page.evaluate(script, url)
    except Exception as e:  # noqa: BLE001 - evaluate 경계
        return {"error": f"{type(e).__name__}: {e}"}


def main() -> int:
    keyword = sys.argv[1] if len(sys.argv) > 1 else "뷰티"
    q = quote(keyword)
    log(f"PoC v15 — '{keyword}' SRP page≥2 돌파 재조사")

    report = {"keyword": keyword, "loads": [], "network": []}
    base_ids: set[str] = set()

    from camoufox.sync_api import Camoufox
    with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
        page = browser.new_page()
        network_log: list[str] = []
        page.on("request", lambda r: network_log.append(r.url))

        log("웜업 20초...")
        page.goto(COUPANG_HOME, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        end = time.monotonic() + 20
        while time.monotonic() < end:
            page.mouse.move(random.randint(100, 900), random.randint(100, 600))
            page.mouse.wheel(0, random.randint(50, 250))
            time.sleep(random.uniform(0.5, 1.5))

        def load_and_measure(url: str, name: str, save_html: bool = True) -> dict:
            nonlocal base_ids
            log(f"[{name}] → {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                log(f"  로드 실패: {type(e).__name__}: {e}")
                return {"name": name, "error": str(e)}
            time.sleep(random.uniform(3.0, 5.0))
            ids = set()
            for _ in range(10):
                ids = product_ids(page)
                if ids:
                    break
                time.sleep(1.5)
            html = page.content()
            cards = count_cards(html)
            fresh = ids - base_ids
            is_blocked = blocked(html)
            pager = len(re.findall(r'Pagination_|data-page=', html)) > 0
            m = DISABLE_PAGINATION_RE.search(html)
            flag = m.group(1) if m else "?"
            log(f"  카드 {cards}개 / href {len(ids)}개 (기준 대비 신규 {len(fresh)}) "
                f"/ {len(html) // 1024}KB / 차단={is_blocked} / pager={pager} "
                f"/ disableFixedPagination={flag}")
            if save_html:
                safe = re.sub(r"[^a-z0-9]", "_", name)[:30]
                (OUTPUT_DIR / f"poc15_{ts}_{safe}.html").write_text(html, encoding="utf-8")
            result = {"name": name, "url": url[:220], "cards": cards,
                      "items": len(ids), "fresh_vs_base": len(fresh),
                      "blocked": is_blocked, "pager": pager,
                      "disableFixedPagination": flag}
            report["loads"].append(result)
            time.sleep(random.uniform(14, 19))
            return result

        # 기준: 기본 검색 page1 + searchId 추출
        r = load_and_measure(f"https://www.coupang.com/np/search?q={q}", "base_page1")
        if r.get("cards", 0) == 0:
            log("✗ 기본 검색 실패 — 중단")
            return 2
        base_ids = product_ids(page) or base_ids
        html = page.content()
        sid = (SEARCH_ID_RE.search(html) or [None, None])
        sid = sid.group(1) if sid else ""
        trid = TRACE_ID_RE.search(html)
        trid = trid.group(1) if trid else ""
        log(f"  searchId={sid}  traceId={trid}")
        if not sid:
            log("  ✗ searchId 미추출 — T1/T3/T4/T6 는 searchId 없이 진행")
        report["searchId"] = sid
        report["traceId"] = trid

        search = f"https://www.coupang.com/np/search?q={q}"

        # T1: page=2 + searchId
        load_and_measure(f"{search}&page=2&searchId={sid}", "T1_page2_searchId")

        # T2: 공식 스키마 component=&listSize=72
        load_and_measure(f"{search}&component=&page=2&listSize=72", "T2_component_listSize72")

        # T3: listSize=36 + page=2 (+searchId)
        load_and_measure(
            f"{search}&listSize=36&page=2&searchId={sid}", "T3_listSize36_searchId")

        # T4: 명시 기본 정렬 + page=2 + searchId
        load_and_measure(
            f"{search}&sorter=scoreDesc&page=2&searchId={sid}", "T4_scoreDesc_page2")

        # T5: RSC flight fetch (내비게이션 없음)
        log("[T5_rsc_fetch] → in-page RSC fetch")
        rsc = rsc_fetch(page, f"{search}&page=2")
        log(f"  status={rsc.get('status')} len={rsc.get('len')} "
            f"hasProducts={rsc.get('hasProducts')} error={rsc.get('error')}")
        log(f"  head: {rsc.get('head', '')[:150]}")
        report["loads"].append({"name": "T5_rsc_fetch",
                                "url": f"{search}&page=2", **rsc})
        time.sleep(random.uniform(14, 19))

        # T6: channel=user + component + page=2 + searchId
        load_and_measure(
            f"{search}&channel=user&component=&page=2&searchId={sid}",
            "T6_channel_component_searchId")

        # T7: PLP page=2 대조 (세션 정상성 확인)
        load_and_measure("https://www.coupang.com/np/categories/176522?page=2",
                         "T7_plp_page2_control")

    report["network"] = sorted({u for u in network_log
                                if "np/search" in u or "api" in u or "rsc" in u})
    out = OUTPUT_DIR / f"poc15_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log("=" * 60)
    log("요약:")
    for p in report["loads"]:
        if "error" in p and p.get("name", "").startswith("T"):
            log(f"  {p['name']:26} 오류: {p['error'][:60]}")
        elif p.get("name") != "T5_rsc_fetch":
            log(f"  {p['name']:26} 카드 {p.get('cards', 0):3d}개 / "
                f"pager={p.get('pager')} / flag={p.get('disableFixedPagination')}")
    log(f"  네트워크 후보 {len(report['network'])}건: {report['network'][:5]}")
    log(f"리포트: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
