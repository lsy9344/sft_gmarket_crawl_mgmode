import json, socket, sys, tempfile
from pathlib import Path

OUT = Path("/tmp/coupang-blockhunt")
sys.path.insert(0, "/home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode")
from app.core import decodo

proxy = decodo.sticky_proxy_dict(decodo.load_settings(), session_id="zcode-blockhunt-b")

def hit(make, tag):
    rec = {"tag": tag, "ts_edge": socket.gethostbyname("www.coupang.com")}
    ctx = cm = pw = None
    try:
        ctx, cm, pw = make()
        page = ctx.new_page()
        resp = page.goto("https://www.coupang.com/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2000)
        rec.update({"status": resp.status, "server": resp.headers.get("server"),
                    "ua": page.evaluate("navigator.userAgent")[:70],
                    "title": page.title()[:40]})
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {str(e)[:130]}"
    finally:
        for c in (lambda: cm.__exit__(None, None, None), lambda: ctx.close()):
            try: c()
            except Exception: pass
        if pw:
            try: pw.stop()
            except Exception: pass
    return rec

def camou(p):
    from camoufox.sync_api import Camoufox
    def make():
        cm = Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True, proxy=p)
        return cm.__enter__(), cm, None
    return make

def chromium(p):
    from patchright.sync_api import sync_playwright
    prof = tempfile.mkdtemp(prefix="pp2-", dir="/tmp")
    def make():
        pw = sync_playwright().start()
        ctx = pw.chromium.launch_persistent_context(
            prof + ("/proxy" if p else "/direct"), headless=False,
            locale="ko-KR", viewport={"width": 1280, "height": 800}, proxy=p)
        return ctx, None, pw
    return make

try:
    print("decodo exit ip:", decodo.fetch_exit_ip(proxy).ip, flush=True)
except Exception as e:
    print("exitip err:", str(e)[:100], flush=True)

arms = [
    ("1-chromium-decodo", chromium(proxy)),
    ("2-camou-decodo", camou(proxy)),
    ("3-camou-direct", camou(None)),
    ("4-chromium-decodo-2", chromium(proxy)),
]
out = []
for tag, make in arms:
    r = hit(make, tag)
    out.append(r)
    print(json.dumps(r, ensure_ascii=False), flush=True)
(OUT / "proxy_probe2.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
