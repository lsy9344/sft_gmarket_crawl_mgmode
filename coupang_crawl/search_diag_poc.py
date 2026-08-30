"""검색 결과 추가 로드 메커니즘 진단 POC 2 — 스크롤 컨테이너/센티넬 탐색

1회 세션: 웜업 → 검색 로드 → DOM 진단(스크롤 가능 컨테이너, 센티넬/버튼 탐색)
→ 올바른 컨테이너로 스크롤 시도 → 상품 수 변화 관찰. 요청 최소화.
"""
import json
import random
import time
from datetime import datetime
from pathlib import Path

from camoufox.sync_api import Camoufox

KEYWORD = "에어프라이어"
SEARCH_URL = f"https://www.coupang.com/np/search?q={KEYWORD}"
OUTPUT_DIR = Path(__file__).resolve().parent / "output"

DIAG_JS = """
() => {
  const out = {scrollable: [], sentinels: [], buttons: []};
  // scrollable candidates
  const all = document.querySelectorAll('*');
  let n = 0;
  for (const el of all) {
    if (n > 4000) break; n++;
    const cs = getComputedStyle(el);
    if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 100) {
      out.scrollable.push({
        tag: el.tagName, id: el.id, cls: (el.className||'').toString().slice(0,80),
        sh: el.scrollHeight, ch: el.clientHeight,
      });
    }
  }
  out.body = {sh: document.body.scrollHeight, ch: document.documentElement.clientHeight,
              y: window.scrollY};
  // sentinel candidates
  const cand = document.querySelectorAll(
    '[class*="loader"], [class*="infinite"], [class*="sentinel"], [class*="observe"], ' +
    '[class*="Loading"], [class*="loading"], [class*="more"], [data-observe]'
  );
  for (const el of cand) {
    const r = el.getBoundingClientRect();
    out.sentinels.push({tag: el.tagName, cls: (el.className||'').toString().slice(0,90),
                        visible: r.width>0&&r.height>0, top: Math.round(r.top)});
  }
  // buttons containing 더보기/더보기/다음
  for (const b of document.querySelectorAll('button, a')) {
    const t = (b.innerText||'').trim();
    if (t && t.length < 20 && /더보기|더 보기|다음|페이지|더많은|view more/i.test(t)) {
      out.buttons.push({tag: b.tagName, text: t, cls: (b.className||'').toString().slice(0,60)});
    }
  }
  out.items = document.querySelectorAll('#product-list > li').length;
  return out;
}
"""

SCROLL_TRY_JS = """
(target) => {
  const el = target === 'window' ? null : document.querySelector(target);
  if (el) { el.scrollTop = el.scrollHeight; return 'container:' + target + ' scrollTop=' + el.scrollTop; }
  window.scrollTo(0, document.body.scrollHeight);
  return 'window scrollTo bottom';
}
"""


def main() -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log: dict = {"started_at": ts, "requests": [], "steps": []}

    def on_request(req):
        if any(k in req.url for k in ("n-api", "/api/", "search")):
            log["requests"].append({
                "t": round(time.time(), 2), "method": req.method,
                "url": req.url[:300], "post": (req.post_data or "")[:300] or None,
            })

    with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
        page = browser.new_page()
        page.on("request", on_request)

        page.goto("https://www.coupang.com", timeout=60000)
        for _ in range(4):
            page.mouse.wheel(0, random.randint(300, 700))
            time.sleep(random.uniform(1.5, 3.0))

        t0 = time.time()
        page.goto(SEARCH_URL, timeout=60000)
        page.wait_for_timeout(4000)
        diag = page.evaluate(DIAG_JS)
        log["diag"] = diag
        print(json.dumps(diag, ensure_ascii=False, indent=1)[:3000])

        # pick scroll target: deepest scrollable container containing product list, else window
        target = "window"
        for s in diag["scrollable"]:
            if s["sh"] > 1500:
                sel = f"#{s['id']}" if s["id"] else None
                if sel:
                    target = sel
                    break
        print("scroll target:", target)

        prev = diag["items"]
        for i in range(1, 7):
            time.sleep(random.uniform(2.5, 4.0))
            res = page.evaluate(SCROLL_TRY_JS, target)
            page.wait_for_timeout(random.randint(2500, 4000))
            d2 = page.evaluate(DIAG_JS)
            cnt = d2["items"]
            log["steps"].append({"i": i, "target": res, "items": cnt,
                                 "delta": cnt - prev, "body_y": d2["body"]["y"]})
            print(f"[try {i}] {res} items={cnt} (+{cnt - prev})")
            if cnt == prev and i >= 3:
                break
            prev = cnt

        log["final_items"] = page.evaluate("() => document.querySelectorAll('#product-list > li').length")

    out = OUTPUT_DIR / f"search_diag_{ts}.json"
    out.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved:", out, "final:", log["final_items"])


if __name__ == "__main__":
    main()
