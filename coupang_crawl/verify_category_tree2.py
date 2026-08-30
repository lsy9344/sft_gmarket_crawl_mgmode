"""메가메뉴 2뎁스(호버 패널) 추출 + 캐시 대조."""
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
PROFILE_DIR = BASE_DIR / "runtime_profile"
CACHE = BASE_DIR.parent / "output" / "coupang_category_tree.json"
ts = datetime.now().strftime("%Y%m%d_%H%M%S")

OPEN_JS = r"""
async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const btn = document.querySelector('#wa-category');
  if (!btn) return false;
  btn.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
  btn.dispatchEvent(new MouseEvent('mouseenter', {bubbles: true}));
  await sleep(600);
  btn.click();
  for (let i = 0; i < 20; i++) {
    await sleep(500);
    if (document.querySelector('#wa-pc-category .shopping-menu-list li')) return true;
  }
  return false;
}
"""

HOVER_AND_READ_JS = r"""
async (idx) => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const lis = document.querySelectorAll('#wa-pc-category .shopping-menu-list > li');
  if (idx >= lis.length) return null;
  const li = lis[idx];
  const a1 = li.querySelector(':scope > a');
  const top = {name: a1 ? a1.textContent.trim() : '',
               href: a1 ? a1.getAttribute('href') : ''};
  li.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
  li.dispatchEvent(new MouseEvent('mouseenter', {bubbles: true}));
  await sleep(700);
  const subs = [];
  // 중첩 목록 또는 형제 서브 패널 둘 다 수집
  const scopes = [li, ...document.querySelectorAll('#wa-pc-category ul')];
  const seen = new Set();
  for (const sc of scopes) {
    for (const a of sc.querySelectorAll('a[href*="/np/categories/"]')) {
      if (a === a1) continue;
      const href = a.getAttribute('href') || '';
      const m = href.match(/\/np\/categories\/(\d+)/);
      if (!m) continue;
      const key = m[1] + '|' + a.textContent.trim();
      if (seen.has(key)) continue;
      seen.add(key);
      // 1뎁스 자신의 링크는 제외 (다른 탑 항목)
      const rect = a.getBoundingClientRect();
      subs.push({id: m[1], name: a.textContent.trim(),
                 nested: li.contains(a), top: Math.round(rect.top)});
    }
  }
  return {top: top, subs: subs};
}
"""

TOP_COUNT_JS = "() => document.querySelectorAll('#wa-pc-category .shopping-menu-list > li').length"


def cache_tree():
    d = json.loads(CACHE.read_text(encoding="utf-8"))
    idx = {}
    def walk(nodes):
        for n in nodes:
            idx[n["id"]] = n
            walk(n["children"])
    for _, roots in d["groups"]:
        walk(roots)
    return idx


def main() -> int:
    from camoufox.sync_api import Camoufox

    cache_idx = cache_tree()
    report = {"tops": [], "mismatch": []}

    with Camoufox(persistent_context=True, user_data_dir=str(PROFILE_DIR),
                  headless=False, geoip=True, locale="ko-KR", humanize=True) as context:
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.set_viewport_size({"width": 1440, "height": 900})
        except Exception:  # noqa: BLE001
            pass
        page.goto("https://www.coupang.com/", wait_until="domcontentloaded", timeout=40000)
        if "사용권한" in page.content():
            print("차단 — 중단")
            return 2
        end = time.monotonic() + 12
        while time.monotonic() < end:
            page.mouse.move(random.randint(100, 900), random.randint(100, 600))
            page.mouse.wheel(0, random.randint(50, 200))
            time.sleep(random.uniform(0.5, 1.2))

        if not page.evaluate(OPEN_JS):
            print("메가메뉴 열기 실패")
            return 3

        n = page.evaluate(TOP_COUNT_JS)
        print(f"탑 카테고리 {n}개 — 호버로 하위 추출")
        for i in range(n):
            r = page.evaluate(HOVER_AND_READ_JS, i)
            if r is None:
                continue
            m1 = re.search(r"/np/categories/(\d+)", r["top"]["href"] or "")
            tid = m1.group(1) if m1 else "?"
            nested = [s for s in r["subs"] if s["nested"]]
            panel = [s for s in r["subs"] if not s["nested"]]
            subs = nested if nested else panel
            # 순서 보존 중복 제거
            uniq, seen = [], set()
            for s in subs:
                if s["id"] not in seen:
                    seen.add(s["id"]); uniq.append(s)
            report["tops"].append({"id": tid, "name": r["top"]["name"],
                                   "subs": [{"id": s["id"], "name": s["name"]} for s in uniq]})
            # 캐시 대조
            cnode = cache_idx.get(tid)
            if cnode is None:
                report["mismatch"].append({"top": r["top"]["name"], "issue": "탑 ID 캐시에 없음"})
                continue
            cache_children = {c["id"]: c["name"] for c in cnode["children"]}
            for s in uniq:
                cname = cache_children.get(s["id"])
                if cname is None:
                    report["mismatch"].append(
                        {"top": r["top"]["name"], "issue": f"하위 {s['name']}({s['id']}) 캐시에 없음"})
                elif cname != s["name"]:
                    report["mismatch"].append(
                        {"top": r["top"]["name"],
                         "issue": f"이름 다름: {s['name']}({s['id']}) 캐시='{cname}'"})
            cache_only = [cid for cid in cache_children if cid not in {s["id"] for s in uniq}]
            if cache_only and uniq:
                report["mismatch"].append(
                    {"top": r["top"]["name"],
                     "issue": f"캐시에만 있는 하위 {len(cache_only)}개 (메뉴 노출 한도 13개 초과 등)"})
            print(f"  [{i+1}/{n}] {r['top']['name']}: 쿠팡 하위 {len(uniq)}개 / "
                  f"캐시 하위 {len(cache_children)}개")
            time.sleep(random.uniform(0.4, 0.9))

    out = OUTPUT_DIR / f"megamenu_depth2_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n저장: {out}")
    print(f"불일치 항목: {len(report['mismatch'])}")
    for m in report["mismatch"][:25]:
        print(f"   {m['top']}: {m['issue']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
