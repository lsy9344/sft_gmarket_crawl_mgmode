import json, time
from curl_cffi import requests
out = []
for imp in ("safari260", "safari184", "tor145", "edge101"):
    try:
        r = requests.get("https://www.coupang.com/", impersonate=imp, timeout=25)
        out.append({"imp": imp, "status": r.status_code, "server": r.headers.get("server"), "len": len(r.text)})
    except Exception as e:
        out.append({"imp": imp, "error": str(e)[:120]})
    time.sleep(3)
print(json.dumps(out, ensure_ascii=False, indent=1))
with open("/tmp/coupang-blockhunt/family.json", "w") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
