import json, socket, time
from pathlib import Path

OUT = Path("/tmp/coupang-blockhunt")

def edge_ip():
    try:
        return socket.gethostbyname("www.coupang.com")
    except Exception:
        return None

def camoufox_default():
    from camoufox.sync_api import Camoufox
    cm = Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True)
    ctx = cm.__enter__()
    return ctx, lambda: cm.__exit__(None, None, None)

def pwff_default():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    ctx = pw.firefox.launch(headless=False)
    return ctx, lambda: (ctx.close(), pw.stop())

def hit(make, tag):
    rec = {"tag": tag, "edge_dns": edge_ip()}
    ctx, close = None, None
    try:
        ctx, close = make()
        page = ctx.new_page()
        hdrs = {}
        def on_resp(r):
            if r.request.resource_type == "document" and r.url.startswith("https://www.coupang.com"):
                hdrs.update({k: v for k, v in r.headers.items() if k in
                             ("server", "x-cache", "akamai-grn", "via", "x-reference-error", "content-encoding", "cache-control", "date")})
                hdrs["_status"] = r.status
        page.on("response", on_resp)
        resp = page.goto("https://www.coupang.com/", wait_until="domcontentloaded", timeout=40000)
        page.wait_for_timeout(2000)
        rec.update({"status": resp.status, "ua": page.evaluate("navigator.userAgent"),
                    "hdrs": hdrs, "title": page.title()[:50]})
    except Exception as e:
        rec["error"] = str(e)[:150]
    finally:
        if close:
            try: close()
            except Exception: pass
    return rec

seq = [("camoufox", camoufox_default), ("pwff", pwff_default),
       ("camoufox", camoufox_default), ("pwff", pwff_default),
       ("camoufox", camoufox_default)]
out = []
for i, (name, make) in enumerate(seq):
    r = hit(make, f"{i}-{name}")
    out.append(r)
    print(json.dumps(r, ensure_ascii=False), flush=True)
    time.sleep(5)
(OUT / "abab.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
