"""검색 페이지네이션 메커니즘 규명 POC 3 — SRP JS 청크 확보 + page=2 변형 테스트

- 라이브 브라우저 컨텍스트 안에서 SRP JS 청크를 fetch해 로컬 저장 (Akamai 통과)
- ?page=2 변형 URL 2종 로드 후 상품 수 측정
요청 최소화: 웜업 + 검색 1회 + 청크 fetch(정적 에셋) + page=2 변형 2회.
"""
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path

from camoufox.sync_api import Camoufox

KEYWORD = "에어프라이어"
OUTPUT_DIR = Path(__file__).resolve().parent / "output"
CHUNK_DIR = OUTPUT_DIR / "srp_chunks"
CHUNK_DIR.mkdir(exist_ok=True)

COUNT_JS = "() => document.querySelectorAll('#product-list > li').length"

PAGE2_VARIANTS = [
    f"https://www.coupang.com/np/search?q={KEYWORD}&page=2",
    f"https://www.coupang.com/np/search?component=&q={KEYWORD}&channel=user&page=2",
]


def main() -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log: dict = {"started_at": ts, "chunks": [], "variants": []}

    with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
        page = browser.new_page()

        page.goto("https://www.coupang.com", timeout=60000)
        for _ in range(4):
            page.mouse.wheel(0, random.randint(300, 700))
            time.sleep(random.uniform(1.5, 3.0))

        page.goto(f"https://www.coupang.com/np/search?q={KEYWORD}", timeout=60000)
        page.wait_for_timeout(4000)
        html = page.content()
        log["page1_items"] = page.evaluate(COUNT_JS)

        # discover chunk urls from page html
        chunk_urls = sorted(set(re.findall(
            r'(?:https://assets\.coupangcdn\.com|)("[^"]*static/chunks/(?:app/srp/|[a-z0-9-]*\.js)[^"]*")',
            html))) or []
        # simpler: grab all static/chunks/... .js tokens with base
        tokens = set(re.findall(r'(?:https://[^"\']+)?/?static/chunks/[A-Za-z0-9._/-]+\.js', html))
        # resolve base from html
        base_m = re.search(r'"(https://assets\.coupangcdn\.com/[^"]*?)/static/chunks/', html)
        base = base_m.group(1) if base_m else "https://www.coupang.com"
        urls = []
        for t in tokens:
            if t.startswith("http"):
                urls.append(t)
            else:
                urls.append(base + "/" + t.lstrip("/"))
        # prioritize srp app chunks
        urls.sort(key=lambda u: ("app/srp" not in u, u))
        urls = urls[:14]
        log["chunk_base"] = base
        print(f"chunks to fetch: {len(urls)}")

        for u in urls:
            try:
                resp = page.evaluate(
                    """async (u) => {
                        const r = await fetch(u, {credentials: 'omit'});
                        return {status: r.status, text: await r.text()};
                    }""", u)
                name = re.sub(r'[^A-Za-z0-9._-]', '_', u.split('/chunks/')[-1])
                p = CHUNK_DIR / name
                p.write_text(resp["text"], encoding="utf-8")
                log["chunks"].append({"url": u, "status": resp["status"],
                                      "kb": round(len(resp["text"]) / 1024), "file": str(p.name)})
                print(f"  {resp['status']} {len(resp['text'])//1024}KB {name}")
            except Exception as e:  # noqa: BLE001
                log["chunks"].append({"url": u, "error": str(e)[:120]})
                print(f"  ERR {u} {e}")
            time.sleep(random.uniform(0.5, 1.2))

        # page=2 variants
        for v in PAGE2_VARIANTS:
            time.sleep(random.uniform(3.0, 5.0))
            page.goto(v, timeout=60000)
            page.wait_for_timeout(4000)
            cnt = page.evaluate(COUNT_JS)
            body_txt = page.evaluate(
                "() => document.body.innerText.slice(0, 300).replace(/\\s+/g, ' ')")
            log["variants"].append({"url": v, "items": cnt, "body_head": body_txt[:200],
                                     "final_url": page.url})
            print(f"[variant] items={cnt} {v}")

    out = OUTPUT_DIR / f"search_mech_{ts}.json"
    out.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved:", out)


if __name__ == "__main__":
    main()
