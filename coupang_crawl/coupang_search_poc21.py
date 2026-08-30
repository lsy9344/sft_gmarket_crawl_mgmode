"""
Coupang PoC v21 — /n-api/web-adapter/category-list 페이로드 구조 확인
====================================================================
카테고리 컨셉 전환(키워드→카테고리 선택) 준비: '카테고리' 버튼 메가메뉴의
데이터 소스 응답 형태를 1회 요청으로 확보한다 (홈 웜업 + in-page fetch 1회).
"""
import json
import random
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
PROFILE_DIR = BASE_DIR / "runtime_profile"
ts = datetime.now().strftime("%Y%m%d_%H%M%S")

FETCH_JS = """
async () => {
    // JSONP 콜백 방식으로 페이지 자신이 쓰는 패턴 재현
    const cb = '__poc21_cb_' + Date.now();
    const url = '/n-api/web-adapter/category-list?callback=' + cb + '&_=' + Date.now();
    return await new Promise((resolve) => {
        const timer = setTimeout(() => resolve({error: 'timeout'}), 15000);
        window[cb] = (data) => {
            clearTimeout(timer);
            delete window[cb];
            try { document.head.removeChild(s); } catch (e) {}
            resolve({ok: true, payload: data});
        };
        const s = document.createElement('script');
        s.src = url;
        s.onerror = () => { clearTimeout(timer); resolve({error: 'script_error'}); };
        document.head.appendChild(s);
    });
}
"""

FETCH_PLAIN_JS = """
async () => {
    const r = await fetch('/n-api/web-adapter/category-list', {credentials: 'include'});
    return {status: r.status, ctype: r.headers.get('content-type'),
            text: await r.text()};
}
"""


def main() -> int:
    from camoufox.sync_api import Camoufox

    with Camoufox(persistent_context=True, user_data_dir=str(PROFILE_DIR),
                  headless=False, geoip=True, locale="ko-KR",
                  humanize=True) as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.coupang.com/", wait_until="domcontentloaded", timeout=60000)
        html = page.content()
        if "사용권한" in html or "error403" in html:
            print("차단 상태 — 중단")
            return 2
        end = time.monotonic() + 15
        while time.monotonic() < end:
            page.mouse.move(random.randint(100, 900), random.randint(100, 600))
            page.mouse.wheel(0, random.randint(50, 250))
            time.sleep(random.uniform(0.5, 1.5))

        r = page.evaluate(FETCH_JS)
        out = OUTPUT_DIR / f"poc21_{ts}_category_list.json"
        if r.get("ok"):
            payload = r["payload"]
            out.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            print(f"JSONP OK — keys: {list(payload.keys()) if isinstance(payload, dict) else type(payload)}")
            if isinstance(payload, dict):
                for k, v in payload.items():
                    if isinstance(v, dict):
                        print(f"  {k}: dict keys={list(v.keys())[:12]}")
                    elif isinstance(v, list):
                        print(f"  {k}: list len={len(v)} first={json.dumps(v[0], ensure_ascii=False)[:150] if v else None}")
        else:
            print(f"JSONP 실패: {r.get('error')} — plain fetch 시도")
            r2 = page.evaluate(FETCH_PLAIN_JS)
            print(f"plain: status={r2['status']} ctype={r2['ctype']} len={len(r2['text'])}")
            out.write_text(r2["text"][:2_000_000], encoding="utf-8")
        print("saved:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
