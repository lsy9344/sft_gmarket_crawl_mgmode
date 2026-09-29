"""Actual-browser reproduction: Camoufox (Firefox engine) vs Patchright Chromium, direct line."""
import json, sys, tempfile, shutil, traceback
from pathlib import Path

OUT = Path("/tmp/coupang-blockhunt")
OUT.mkdir(exist_ok=True)
URL = "https://www.coupang.com/"

def probe_page(page):
    docs = []
    def on_response(r):
        try:
            if r.request.resource_type == "document":
                docs.append({"url": r.url, "status": r.status, "server": r.headers.get("server")})
        except Exception:
            pass
    page.on("response", on_response)
    resp = None
    nav_err = None
    try:
        resp = page.goto(URL, wait_until="domcontentloaded", timeout=40000)
    except Exception as e:
        nav_err = f"{type(e).__name__}: {str(e)[:200]}"
    page.wait_for_timeout(3500)
    fp = {}
    try:
        fp = page.evaluate(
            "(() => { const gl = document.createElement('canvas').getContext('webgl');"
            " let webgl = null; try { const e = gl.getExtension('WEBGL_debug_renderer_info');"
            " webgl = [gl.getParameter(e.UNMASKED_VENDOR_WEBGL), gl.getParameter(e.UNMASKED_RENDERER_WEBGL)]; } catch (err) {}"
            " return {ua: navigator.userAgent, platform: navigator.platform, oscpu: navigator.oscpu,"
            " lang: navigator.language, tz: Intl.DateTimeFormat().resolvedOptions().timeZone,"
            " screen: [screen.width, screen.height], dpr: window.devicePixelRatio,"
            " hw: navigator.hardwareConcurrency, webdriver: navigator.webdriver, webgl}; })()"
        )
    except Exception as e:
        fp = {"error": str(e)}
    title, body_len, body_head = None, None, ""
    try:
        title = page.title()
        body = page.locator("body").inner_text(timeout=4000)
        body_len, body_head = len(body), body[:200].replace("\n", " ")
    except Exception:
        pass
    denied = "access denied" in ((title or "") + body_head).lower()
    return {"docs": docs, "status": resp.status if resp else None, "nav_error": nav_err,
            "title": title, "body_len": body_len, "body_head": body_head,
            "denied": denied, "fp": fp}

def run_arm(tag, launcher):
    rec = {"tag": tag}
    ctx = cm = pw = None
    try:
        ctx, cm, pw = launcher()
        page = ctx.new_page()
        rec.update(probe_page(page))
        page.screenshot(path=str(OUT / f"{tag}.png"))
    except Exception as e:
        rec["launch_error"] = f"{type(e).__name__}: {str(e)[:300]}"
        rec["trace"] = traceback.format_exc()[-800:]
    finally:
        for closer in (lambda: cm.__exit__(None, None, None), lambda: ctx.close()):
            try:
                closer()
            except Exception:
                pass
        if pw:
            try: pw.stop()
            except Exception: pass
    print(json.dumps(rec, ensure_ascii=False), flush=True)
    return rec

def camoufox_launcher(**kw):
    from camoufox.sync_api import Camoufox
    def launch():
        cm = Camoufox(**kw)
        ctx = cm.__enter__()
        return ctx, cm, None
    return launch

def chromium_launcher():
    from patchright.sync_api import sync_playwright
    profile = tempfile.mkdtemp(prefix="pr-chromium-", dir="/tmp")
    def launch():
        pw = sync_playwright().start()
        ctx = pw.chromium.launch_persistent_context(
            profile, headless=False, locale="ko-KR",
            viewport={"width": 1280, "height": 800})
        return ctx, None, pw
    return launch

if __name__ == "__main__":
    arms = json.loads(sys.argv[1])
    results = []
    for spec in arms:
        tag = spec["tag"]
        if spec.get("engine") == "chromium":
            launcher = chromium_launcher()
        else:
            kw = dict(spec.get("kwargs", {}))
            kw.setdefault("headless", False)
            launcher = camoufox_launcher(**kw)
        results.append(run_arm(tag, launcher))
    (OUT / "browser_probe.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print("SUMMARY:", json.dumps(
        [{"tag": r.get("tag"), "status": r.get("status"), "server": (r.get("docs") or [{}])[0].get("server"),
          "denied": r.get("denied"), "launch_error": bool(r.get("launch_error"))} for r in results]))
