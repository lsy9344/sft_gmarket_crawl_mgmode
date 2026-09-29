"""Same-minute comparison: Camoufox/Chromium x direct/Decodo-KR lines."""
import json, socket, sys, tempfile
from pathlib import Path

OUT = Path("/tmp/coupang-blockhunt")
sys.path.insert(0, "/home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode")
from app.core import decodo

settings = decodo.load_settings()
proxy = decodo.sticky_proxy_dict(settings, session_id="zcode-blockhunt-a")
pw_proxy = {"server": f"http://{proxy['server']}", "username": proxy["username"], "password": proxy["password"]}
print("proxy host:", proxy["server"].split(":")[0], flush=True)

def hit(make, tag):
    rec = {"tag": tag, "edge_dns": socket.gethostbyname("www.coupang.com")}
    ctx = cm = pw = None
    try:
        ctx, cm, pw = make()
        page = ctx.new_page()
        resp = page.goto("https://www.coupang.com/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2000)
        rec.update({"status": resp.status, "server": resp.headers.get("server"),
                    "ua": page.evaluate("navigator.userAgent")[:70],
                    "title": page.title()[:45]})
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {str(e)[:140]}"
    finally:
        for c in (lambda: cm.__exit__(None, None, None), lambda: ctx.close()):
            try: c()
            except Exception: pass
        if pw:
            try: pw.stop()
            except Exception: pass
    return rec

def camou(proxy_dict):
    from camoufox.sync_api import Camoufox
    def make():
        cm = Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True,
                      proxy=proxy_dict or None)
        return cm.__enter__(), cm, None
    return make

def chromium(proxy_dict):
    from patchright.sync_api import sync_playwright
    prof = tempfile.mkdtemp(prefix="pp-", dir="/tmp")
    def make():
        pw = sync_playwright().start()
        ctx = pw.chromium.launch_persistent_context(
            prof + "/direct" if proxy_dict is None else prof + "/proxy",
            headless=False, locale="ko-KR", viewport={"width": 1280, "height": 800},
            proxy=proxy_dict)
        return ctx, None, pw
    return make

try:
    exit_ip = decodo.fetch_exit_ip(proxy).ip
    print("decodo exit ip:", exit_ip, flush=True)
except Exception as e:
    print("exit ip fetch failed:", str(e)[:120], flush=True)

arms = [
    ("1-camou-direct", camou(None)),
    ("2-camou-decodo", camou(pw_proxy)),
    ("3-chromium-decodo", chromium(pw_proxy)),
    ("4-camou-direct-again", camou(None)),
]
out = []
for tag, make in arms:
    r = hit(make, tag)
    out.append(r)
    print(json.dumps(r, ensure_ascii=False), flush=True)
(OUT / "proxy_probe.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
