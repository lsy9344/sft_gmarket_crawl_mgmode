import json
from pathlib import Path

OUT = Path("/tmp/coupang-blockhunt")
UA_FF152 = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0"
UA_FF156 = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) Gecko/20100101 Firefox/156.0"
UA_FF156L = "Mozilla/5.0 (X11; Linux x86_64; rv:156.0) Gecko/20100101 Firefox/156.0"

def hit_coupang(page, tag, rec):
    docs = []
    page.on("response", lambda r: docs.append(
        {"status": r.status, "server": r.headers.get("server")})
        if r.request.resource_type == "document" else None)
    try:
        resp = page.goto("https://www.coupang.com/", wait_until="domcontentloaded", timeout=40000)
        page.wait_for_timeout(2500)
        body = page.locator("body").inner_text(timeout=4000)
        rec[tag] = {"status": resp.status if resp else None,
                    "server": resp.headers.get("server") if resp else None,
                    "title": page.title()[:60], "denied": "access denied" in (page.title() + body).lower()}
    except Exception as e:
        rec[tag] = {"error": str(e)[:150]}

def pw_firefox_ua(ua):
    from playwright.sync_api import sync_playwright
    rec = {}
    with sync_playwright() as pw:
        b = pw.firefox.launch(headless=False,
                              firefox_user_prefs={"general.useragent.override": ua})
        p = b.new_page()
        sent_ua = p.evaluate("navigator.userAgent")
        try:
            p.goto("https://tls.peet.ws/api/all", wait_until="domcontentloaded", timeout=30000)
            j = json.loads(p.locator("body").inner_text(timeout=5000))
            rec["header_ua_sent"] = j.get("user_agent")
        except Exception as e:
            rec["header_ua_sent"] = f"err {str(e)[:80]}"
        hit_coupang(p, "coupang", rec)
        rec["nav_ua"] = sent_ua
        b.close()
    return rec

def camoufox_ua(ua, label):
    from camoufox.sync_api import Camoufox
    rec = {}
    cm = Camoufox(headless=False, os="windows", firefox_user_prefs={"general.useragent.override": ua})
    try:
        ctx = cm.__enter__()
        p = ctx.new_page()
        try:
            p.goto("https://tls.peet.ws/api/all", wait_until="domcontentloaded", timeout=30000)
            j = json.loads(p.locator("body").inner_text(timeout=5000))
            rec["header_ua_sent"] = j.get("user_agent")
            rec["h2_hash"] = j.get("http2", {}).get("akamai_fingerprint_hash")
        except Exception as e:
            rec["header_ua_sent"] = f"err {str(e)[:80]}"
        hit_coupang(p, "coupang", rec)
        rec["nav_ua"] = p.evaluate("navigator.userAgent")
    finally:
        try: cm.__exit__(None, None, None)
        except Exception: pass
    return rec

results = {}
for tag, ua in [("pwff+ua152", UA_FF152), ("pwff+ua156", UA_FF156)]:
    results[tag] = pw_firefox_ua(ua); print(tag, json.dumps(results[tag], ensure_ascii=False), flush=True)
for tag, ua in [("camoufox+ua156win", UA_FF156), ("camoufox+ua156linux", UA_FF156L)]:
    results[tag] = camoufox_ua(ua, tag); print(tag, json.dumps(results[tag], ensure_ascii=False), flush=True)
(OUT / "ua_isolate.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
