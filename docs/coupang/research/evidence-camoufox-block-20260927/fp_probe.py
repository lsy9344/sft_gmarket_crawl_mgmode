import json, sys, tempfile
from pathlib import Path

OUT = Path("/tmp/coupang-blockhunt")

def get_fp(page, tag):
    rec = {"tag": tag}
    try:
        page.goto("https://tls.peet.ws/api/all", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
        raw = page.locator("body").inner_text(timeout=5000)
        j = json.loads(raw)
        rec["raw_keys"] = sorted(j.keys())
        def dig(d, *keys):
            for k in keys:
                if isinstance(d, dict) and k in d: d = d[k]
                else: return None
            return d
        rec["http2"] = j.get("http2")
        rec["ja3"] = dig(j, "tls", "ja3") or j.get("ja3")
        rec["ja4"] = j.get("ja4")
        rec["tls_ua"] = dig(j, "tls", "user_agent") or j.get("user_agent")
        (OUT / f"fp-{tag}.json").write_text(raw)
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return rec

def run(tag, engine):
    from camoufox.sync_api import Camoufox
    from patchright.sync_api import sync_playwright
    rec = {"tag": tag, "engine": engine}
    ctx = cm = pw = None
    try:
        if engine == "camoufox":
            cm = Camoufox(headless=False, os="windows")
            ctx = cm.__enter__()
        else:
            pw = sync_playwright().start()
            ctx = pw.chromium.launch_persistent_context(
                tempfile.mkdtemp(prefix="fpp-", dir="/tmp"), headless=False,
                locale="ko-KR", viewport={"width": 1280, "height": 800})
        rec.update(get_fp(ctx.new_page(), tag))
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        for c in (lambda: cm.__exit__(None, None, None), lambda: ctx.close()):
            try: c()
            except Exception: pass
        if pw:
            try: pw.stop()
            except Exception: pass
    print(json.dumps(rec, ensure_ascii=False), flush=True)
    return rec

results = [run("fp-camoufox", "camoufox"), run("fp-chromium", "chromium")]
(OUT / "fp_probe.json").write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str))
