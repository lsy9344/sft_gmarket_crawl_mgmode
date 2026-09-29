import json, time
from curl_cffi import requests

EDGE_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0"
FF_UA   = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0"

ARMS = [
    ("chrome150-default",  "chrome150",  None),
    ("firefox147-default", "firefox147", None),
    ("ff147-tls-chrome-ua","firefox147", {"User-Agent": EDGE_UA}),
    ("chrome150-tls-ff-ua","chrome150",  {"User-Agent": FF_UA}),
]

out = []
for tag, imp, headers in ARMS:
    try:
        r = requests.get("https://www.coupang.com/", impersonate=imp, headers=headers,
                         timeout=25, allow_redirects=True)
        body = r.text
        rec = {"tag": tag, "impersonate": imp,
               "status": r.status_code, "server": r.headers.get("server"),
               "body_len": len(body), "body_head": body[:220].replace("\n", " ")}
    except Exception as e:
        rec = {"tag": tag, "error": f"{type(e).__name__}: {e}"}
    out.append(rec)
    print(json.dumps(rec, ensure_ascii=False), flush=True)
    time.sleep(4)

with open("/tmp/coupang-blockhunt/matrix.json", "w") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
