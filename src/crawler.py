"""
Gmarket Seller Crawler v3 - 전체 카테고리 대량 수집
- Scrapling StealthySession (세션 지속, Cloudflare 우회)
- 베스트 10개 + 슈퍼딜 12개 카테고리
- wait_selector로 JS 렌더링 대기
- 점진적 수집량 제어
"""

import sys
sys.stdout.reconfigure(line_buffering=True)

import json
import re
import time
import random
import csv
from pathlib import Path
from datetime import datetime

OUTPUT_DIR = Path(__file__).parent.parent / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

BOT_KEYWORDS = ["기다리십시오", "확인 안내", "확인 절차", "자동입력 방지", "접근이 제한"]

BEST_CATEGORIES = [
    {"name": "신선식품", "url": "https://www.gmarket.co.kr/n/best?groupCode=100000006"},
    {"name": "가공식품", "url": "https://www.gmarket.co.kr/n/best?groupCode=100000005"},
    {"name": "생필품/육아", "url": "https://www.gmarket.co.kr/n/best?groupCode=100000007"},
    {"name": "생활/주방", "url": "https://www.gmarket.co.kr/n/best?groupCode=100001001"},
    {"name": "패션/잡화", "url": "https://www.gmarket.co.kr/n/best?groupCode=100000001"},
    {"name": "뷰티", "url": "https://www.gmarket.co.kr/n/best?groupCode=100000003"},
    {"name": "디지털/가전", "url": "https://www.gmarket.co.kr/n/best?groupCode=100001007"},
    {"name": "가구/홈", "url": "https://www.gmarket.co.kr/n/best?groupCode=100001004"},
    {"name": "스포츠/건강", "url": "https://www.gmarket.co.kr/n/best?groupCode=100001002"},
    {"name": "취미/문구/펫", "url": "https://www.gmarket.co.kr/n/best?groupCode=100001003"},
]

SUPERDEAL_CATEGORIES = [
    {"name": "추천", "url": "https://www.gmarket.co.kr/n/superdeal"},
    {"name": "브랜드패션", "url": "https://www.gmarket.co.kr/n/superdeal?categoryCode=400000135"},
    {"name": "트랜드패션", "url": "https://www.gmarket.co.kr/n/superdeal?categoryCode=400000136"},
    {"name": "뷰티/잡화", "url": "https://www.gmarket.co.kr/n/superdeal?categoryCode=400000137"},
    {"name": "유아동", "url": "https://www.gmarket.co.kr/n/superdeal?categoryCode=400000138"},
    {"name": "식품", "url": "https://www.gmarket.co.kr/n/superdeal?categoryCode=400000139"},
    {"name": "생필품", "url": "https://www.gmarket.co.kr/n/superdeal?categoryCode=400000140"},
    {"name": "가구/침구", "url": "https://www.gmarket.co.kr/n/superdeal?categoryCode=400000141"},
    {"name": "생활/건강", "url": "https://www.gmarket.co.kr/n/superdeal?categoryCode=400000143"},
    {"name": "스포츠/레저", "url": "https://www.gmarket.co.kr/n/superdeal?categoryCode=400000146"},
    {"name": "가전/컴퓨터", "url": "https://www.gmarket.co.kr/n/superdeal?categoryCode=400000148"},
    {"name": "디지털", "url": "https://www.gmarket.co.kr/n/superdeal?categoryCode=400000149"},
]


def is_bot_detected(html: str) -> bool:
    return any(kw in html for kw in BOT_KEYWORDS)


def extract_goods_links(html: str) -> list[str]:
    codes = re.findall(r'goodscode=(\d+)', html)
    return list(dict.fromkeys(codes))


def extract_seller_info(html: str, url: str, code: str) -> dict:
    info = {
        "goodscode": code,
        "url": url,
        "store_name": "",
        "company_name": "",
        "ceo_name": "",
        "email": "",
        "phone": "",
        "business_number": "",
        "collected_at": datetime.now().isoformat(),
    }

    m = re.search(r'text__seller[^>]*>.*?<a[^>]*>([^<]+)</a>', html, re.DOTALL)
    if m:
        info["store_name"] = m.group(1).strip()

    field_patterns = {
        "company_name": r'상호명\s*:\s*<span[^>]*>([^<]+)</span>',
        "ceo_name": r'대표자\s*:\s*<span[^>]*>([^<]+)</span>',
        "phone": r'연락처\s*:\s*<span[^>]*>([^<]+)</span>',
        "business_number": r'사업자\s*등록번호\s*:\s*<span[^>]*>([^<]+)</span>',
        "email": r'E-?mail\s*:\s*<span[^>]*>([^<]+)</span>',
    }

    for field, pat in field_patterns.items():
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            info[field] = m.group(1).strip()

    if not info["business_number"]:
        m = re.search(r'사업자\s*번호\s*:\s*<span[^>]*>([^<]+)</span>', html)
        if m:
            info["business_number"] = m.group(1).strip()

    return info


def load_collected_ids() -> set:
    path = OUTPUT_DIR / "collected_ids.json"
    if path.exists():
        return set(json.loads(path.read_text(encoding="utf-8")))
    return set()


def save_collected_ids(ids: set):
    path = OUTPUT_DIR / "collected_ids.json"
    path.write_text(json.dumps(sorted(ids), ensure_ascii=False), encoding="utf-8")


def save_results(results: list[dict], label: str = ""):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{label}" if label else ""
    json_path = OUTPUT_DIR / f"gmarket_sellers{suffix}_{ts}.json"
    csv_path = OUTPUT_DIR / f"gmarket_sellers{suffix}_{ts}.csv"

    json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    if results:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=results[0].keys())
            writer.writeheader()
            writer.writerows(results)

    return json_path, csv_path


def fetch_listing(session, url: str, is_best: bool = True) -> list[str]:
    kwargs = {"timeout": 20000}
    if is_best:
        kwargs["wait_selector"] = 'a[href*="goodscode"]'
        kwargs["timeout"] = 15000

    r = session.fetch(url, **kwargs)
    if r.status != 200:
        print(f"    listing status={r.status}")
        return []
    if is_bot_detected(r.html_content):
        print("    [BOT] listing blocked")
        return []
    return extract_goods_links(r.html_content)


SESSION_BATCH_SIZE = 8
SESSION_COOLDOWN = (300.0, 480.0)


def crawl(max_items=10, delay_range=(8.0, 15.0), source="best", category_idx=None):
    """
    source: "best" | "superdeal" | "all"
    category_idx: None=random, 0~N=specific category

    Splits work into batches of SESSION_BATCH_SIZE per browser session
    to avoid bot detection (triggers ~14 consecutive detail requests).
    """
    from scrapling.fetchers import StealthySession

    collected_ids = load_collected_ids()
    results = []
    stats = {"total_attempts": 0, "success": 0, "bot_detected": 0, "no_info": 0, "sessions": 0}

    if source == "best":
        categories = BEST_CATEGORIES
    elif source == "superdeal":
        categories = SUPERDEAL_CATEGORIES
    else:
        categories = BEST_CATEGORIES + SUPERDEAL_CATEGORIES

    if category_idx is not None:
        cat = categories[category_idx % len(categories)]
    else:
        cat = random.choice(categories)

    is_best = "best" in cat["url"] and "superdeal" not in cat["url"]

    print("=" * 60)
    print(f"[Gmarket Crawler v3.1] source={source}")
    print(f"  category: {cat['name']}")
    print(f"  max_items={max_items}, batch_size={SESSION_BATCH_SIZE}")
    print(f"  delay={delay_range[0]}~{delay_range[1]}s")
    print(f"  already_collected={len(collected_ids)}")
    print("=" * 60)

    # Phase 1: Get listing (one session for listing)
    all_links = []
    with StealthySession(headless=True) as session:
        print("\n[Phase 1] Warmup + Listing...")
        session.fetch("https://www.gmarket.co.kr")
        time.sleep(5)
        all_links = fetch_listing(session, cat["url"], is_best=is_best)

    new_links = [l for l in all_links if l not in collected_ids]
    targets = new_links[:max_items]
    print(f"  total={len(all_links)}, new={len(new_links)}, targets={len(targets)}")

    if not targets:
        print("  No new items.")
        return results, stats

    # Phase 2: Collect in batches (new session per batch)
    batches = [targets[i:i + SESSION_BATCH_SIZE] for i in range(0, len(targets), SESSION_BATCH_SIZE)]
    print(f"\n[Phase 2] Collecting {len(targets)} items in {len(batches)} batch(es)...")

    for batch_idx, batch in enumerate(batches):
        if stats["bot_detected"] >= 3:
            print("\n[ABORT] Too many bot detections overall.")
            break

        if batch_idx > 0:
            cooldown = random.uniform(*SESSION_COOLDOWN)
            print(f"\n  [Session break] cooling down {cooldown:.0f}s...")
            time.sleep(cooldown)

        stats["sessions"] += 1
        print(f"\n  --- Session {stats['sessions']} (batch {batch_idx+1}/{len(batches)}, {len(batch)} items) ---")

        batch_start = time.time()
        BATCH_TIMEOUT = 480
        batch_aborted = False

        with StealthySession(headless=True) as session:
            session.fetch("https://www.gmarket.co.kr", timeout=20000)
            time.sleep(5)

            for i, code in enumerate(batch):
                if time.time() - batch_start > BATCH_TIMEOUT:
                    print(f"\n  [TIMEOUT] Batch exceeded {BATCH_TIMEOUT}s, aborting.")
                    batch_aborted = True
                    break

                url = f"https://item.gmarket.co.kr/Item?goodscode={code}"
                stats["total_attempts"] += 1
                print(f"  [{i+1}/{len(batch)}] {code}", end=" ")

                try:
                    d = session.fetch(url, timeout=30000)

                    if d.status == 403 or d.status == 429 or is_bot_detected(d.html_content):
                        print(f"-> [BOT/{d.status}]")
                        stats["bot_detected"] += 1
                        batch_aborted = True
                        break

                    if d.status != 200:
                        print(f"-> [FAIL] {d.status}")
                        batch_aborted = True
                        break

                    info = extract_seller_info(d.html_content, url, code)
                    info["source"] = cat["name"]
                    filled = sum(1 for k, v in info.items()
                               if v and k not in ("goodscode", "url", "collected_at", "source"))

                    if filled >= 3:
                        results.append(info)
                        collected_ids.add(code)
                        stats["success"] += 1
                        print(f"-> [OK] {info['store_name']} ({filled}/6)")
                    else:
                        stats["no_info"] += 1
                        print(f"-> [MISS] {filled} fields")

                except Exception as e:
                    print(f"-> [ERR] {e}")
                    batch_aborted = True
                    break

                if i < len(batch) - 1:
                    time.sleep(random.uniform(delay_range[0], delay_range[1]))

        # Save incrementally after each batch
        save_collected_ids(collected_ids)
        if results:
            jp, cp = save_results(results, label=source)
            print(f"\n  [SAVED] {jp.name} ({len(results)} records)")
            results = []

        if batch_aborted and stats["bot_detected"] >= 3:
            print("\n[ABORT] Too many bot detections, stopping category.")
            break

    save_collected_ids(collected_ids)
    if results:
        jp, cp = save_results(results, label=source)
        print(f"\n[SAVED] {jp.name}")

    print(f"\n{'=' * 60}")
    print(f"[RESULT] success={stats['success']} bot={stats['bot_detected']} "
          f"no_info={stats['no_info']} sessions={stats['sessions']}")
    print(f"[TOTAL] unique collected: {len(collected_ids)}")
    print(f"{'=' * 60}")

    return results, stats


if __name__ == "__main__":
    import sys
    items = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    source = sys.argv[2] if len(sys.argv) > 2 else "best"
    cat_idx = int(sys.argv[3]) if len(sys.argv) > 3 else None
    crawl(max_items=items, source=source, category_idx=cat_idx)
