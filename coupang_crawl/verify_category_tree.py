"""
카테고리 트리 대조 검증 — 실제 쿠팡 메가메뉴 vs 우리 캐시
==========================================================
'카테고리' 버튼 메가메뉴(#wa-pc-category)는 홈페이지 HTML 에 서버 렌더링된다.
그 실제 이름/계층을 추출해 우리가 구축한 캐시 트리와 대조한다.

요청 최소화: 홈 1회 로드 + in-page 읽기만.
"""
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

# 메가메뉴 열기: #wa-category 버튼 호버/클릭 → JS 채워지기 대기 → 트리 추출
OPEN_AND_EXTRACT_JS = r"""
async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const btn = document.querySelector('#wa-category');
  if (!btn) return {opened: false, reason: '#wa-category not found', items: []};
  btn.scrollIntoView({block: 'center'});
  btn.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
  btn.dispatchEvent(new MouseEvent('mouseenter', {bubbles: true}));
  await sleep(600);
  btn.click();
  // 쇼핑 메뉴가 채워질 때까지 대기 (JS 가 category-list fetch 후 렌더)
  let ok = false;
  for (let i = 0; i < 20; i++) {
    await sleep(500);
    if (document.querySelector('#wa-pc-category .shopping-menu-list li')) { ok = true; break; }
  }
  if (!ok) return {opened: false, reason: 'menu not populated (fetch 지연/차단 가능)', items: []};
  await sleep(800);

  // 구조 추출: ul.menu.fd(1뎁스) → 내부 ul(2뎁스) → 내부 ul(3뎁스)
  const items = [];
  const catId = (a) => { const m = (a.getAttribute('href') || '').match(/\/np\/categories\/(\d+)/); return m ? m[1] : null; };
  const walk = (liEl, depth, group) => {
    const a = liEl.querySelector(':scope > a');
    if (a) {
      const id = catId(a);
      if (id) items.push({id: id, name: (a.innerText || a.textContent || '').trim(), depth: depth, group: group});
    }
    for (const ul of liEl.querySelectorAll(':scope > ul')) {
      for (const childLi of ul.querySelectorAll(':scope > li')) walk(childLi, depth + 1, group);
    }
  };
  const groups = [['shopping-menu-list', '쇼핑'], ['ticket-menu-list', '티켓'], ['theme-menu-list', '테마관']];
  for (const [cls, label] of groups) {
    const ul = document.querySelector('#wa-pc-category .' + cls.split(' ')[0]);
    if (!ul) continue;
    for (const li of ul.querySelectorAll(':scope > li')) walk(li, 1, label);
  }
  return {opened: true, items: items};
}
"""


def load_cache_index():
    d = json.loads(CACHE.read_text(encoding="utf-8"))
    idx = {}
    def walk(nodes):
        for n in nodes:
            idx[n["id"]] = n["name"]
            walk(n["children"])
    for _, roots in d["groups"]:
        walk(roots)
    return idx


def href_id(href):
    if not href:
        return None
    m = re.search(r"/np/categories/(\d+)", href)
    return m.group(1) if m else None


def main() -> int:
    from camoufox.sync_api import Camoufox

    cache_idx = load_cache_index()
    print(f"캐시 카테고리 수: {len(cache_idx)}")

    with Camoufox(persistent_context=True, user_data_dir=str(PROFILE_DIR),
                  headless=False, geoip=True, locale="ko-KR", humanize=True) as context:
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.set_viewport_size({"width": 1440, "height": 900})
        except Exception:  # noqa: BLE001
            pass
        page.goto("https://www.coupang.com/", wait_until="domcontentloaded", timeout=40000)
        html = page.content()
        if "사용권한" in html or "error403" in html:
            print("차단 상태 — 중단")
            return 2
        # 가벼운 웜업
        end = time.monotonic() + 12
        while time.monotonic() < end:
            page.mouse.move(random.randint(100, 900), random.randint(100, 600))
            page.mouse.wheel(0, random.randint(50, 200))
            time.sleep(random.uniform(0.5, 1.2))

        data = page.evaluate(OPEN_AND_EXTRACT_JS)
        # 원본 HTML 도 보관 (오프라인 재분석용)
        (OUTPUT_DIR / f"megamenu_{ts}.html").write_text(page.content(), encoding="utf-8")

    if not data.get("opened"):
        print(f"메가메뉴 열기 실패: {data.get('reason')} — HTML 저장분으로 대체 분석")
        (OUTPUT_DIR / f"megamenu_{ts}_extract.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return 3

    live_names = {}   # id -> (name, depth)
    order = []
    for it in data["items"]:
        if it["id"] not in live_names:
            live_names[it["id"]] = (it["name"], it["depth"])
            order.append((it["depth"], it["id"], it["name"]))

    (OUTPUT_DIR / f"megamenu_{ts}_extract.json").write_text(
        json.dumps({"items": data["items"]}, ensure_ascii=False, indent=1),
        encoding="utf-8")

    print(f"실제 메가메뉴 카테고리 수: {len(live_names)} (1뎁스 {sum(1 for d,_,_ in order if d==1)})")

    # 대조
    missing_in_cache = []   # 쿠팡엔 있는데 우리 캐시에 없음
    name_mismatch = []      # id 는 있는데 이름 다름
    for cid, (name, depth) in live_names.items():
        if cid not in cache_idx:
            missing_in_cache.append((cid, name, depth))
        elif cache_idx[cid] != name:
            name_mismatch.append((cid, cache_idx[cid], name))
    extra_in_cache = [cid for cid in cache_idx if cid not in live_names]

    print("\n=== 대조 결과 ===")
    print(f"쿠팡 메가메뉴에는 있는데 캐시에 없음: {len(missing_in_cache)}")
    for cid, name, depth in missing_in_cache[:20]:
        print(f"   [{depth}뎁스] {name} ({cid})")
    print(f"이름 불일치: {len(name_mismatch)}")
    for cid, ours, theirs in name_mismatch[:20]:
        print(f"   {cid}: 캐시='{ours}' vs 쿠팡='{theirs}'")
    print(f"캐시에만 있고 메가메뉴에 없음: {len(extra_in_cache)} (테마/여행 등 다른 그룹일 수 있음)")

    # 헬스/건강식품 하위 구조 샘플 대조
    print("\n=== 샘플: 1뎁스 순서 (쿠팡 실제) ===")
    for depth, cid, name in [o for o in order if o[0] == 1][:20]:
        print(f"   {name} ({cid})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
