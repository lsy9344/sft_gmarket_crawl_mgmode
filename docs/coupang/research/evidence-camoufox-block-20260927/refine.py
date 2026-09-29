import json, time
from curl_cffi import requests

FF_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0"
out = {}

# 1) TLS vs H2 split: Firefox TLS stack but force HTTP/1.1 (no h2 ALPN)
try:
    from curl_cffi import CurlHttpVersion
    r = requests.get("https://www.coupang.com/", impersonate="firefox147",
                     http_version=CurlHttpVersion.V1_1, timeout=25)
    out["ff147-tls-http11"] = {"status": r.status_code, "server": r.headers.get("server"), "len": len(r.text)}
except Exception as e:
    out["ff147-tls-http11"] = {"error": f"{type(e).__name__}: {e}"}

# 2) other Coupang hosts with ff vs chrome stack
for host in ("https://m.coupang.com/", "https://www.coupang.com/nm/api/v1/desktop/categories", "https://api.coupang.com/"):
    for imp in ("firefox147", "chrome150"):
        try:
            r = requests.get(host, impersonate=imp, timeout=20)
            out[f"{host}|{imp}"] = {"status": r.status_code, "server": r.headers.get("server"), "len": len(r.text)}
        except Exception as e:
            out[f"{host}|{imp}"] = {"error": str(e)[:120]}
        time.sleep(2)

# 3) concrete fingerprint hashes via tls.peet.ws (JA4 + Akamai H2 fingerprint)
for imp in ("firefox147", "chrome150"):
    try:
        r = requests.get("https://tls.peet.ws/api/all", impersonate=imp, timeout=20)
        j = r.json()
        out[f"fp|{imp}"] = {
            "ja4": j.get("ja4"), "ja3_hash": j.get("ja3"), "peetprint": (j.get("peetprint") or "")[:24],
            "h2_akamai_hash": j.get("http2", {}).get("akamai_fingerprint_hash"),
            "h2_settings": j.get("http2", {}).get("sent_settings"),
        }
    except Exception as e:
        out[f"fp|{imp}"] = {"error": str(e)[:160]}
    time.sleep(2)

print(json.dumps(out, ensure_ascii=False, indent=1))
with open("/tmp/coupang-blockhunt/refine.json", "w") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
