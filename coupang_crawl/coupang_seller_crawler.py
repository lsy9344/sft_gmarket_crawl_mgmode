"""
Coupang Seller Business Info Crawler v1.0
==========================================
Pipeline:
1. Camoufox (headed+xvfb) warmup with natural interaction -> passes Akamai
2. Browse category pages -> extract seller store IDs (shop.coupang.com/Axxxxx)
3. Call shop.coupang.com/api/v1/store/getStoreReview -> full business info
4. Output: JSON + CSV with all fields

Fields collected:
- vendor_id, company_name, ceo_name, business_number, address, phone, email,
  ecommerce_report_number, power_seller, rating_count, thumb_up_ratio

Usage:
    xvfb-run -a python coupang_seller_crawler.py [--categories CAT_IDS] [--max-pages N] [--output FILE]
"""
import time
import json
import random
import os
import re
import csv
import argparse
from datetime import datetime

from camoufox.sync_api import Camoufox

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

DEFAULT_CATEGORIES = [
    "497202",   # 베이비 (original target)
    "178602",   # 생활/주방
    "497197",   # 식품
    "497201",   # 스포츠/레저
    "497196",   # 가전/디지털
]

COUPANG_HOME = "https://www.coupang.com/"


def natural_interaction(page, duration=15):
    """Simulate natural mouse movements and scrolls to pass Akamai behavioral check."""
    end_time = time.time() + duration
    while time.time() < end_time:
        action = random.choice(["move", "scroll", "pause", "move", "scroll"])
        if action == "move":
            x = random.randint(100, 900)
            y = random.randint(100, 600)
            page.mouse.move(x, y)
            time.sleep(random.uniform(0.1, 0.4))
        elif action == "scroll":
            delta = random.randint(50, 300)
            page.mouse.wheel(0, delta)
            time.sleep(random.uniform(0.3, 0.8))
        else:
            time.sleep(random.uniform(0.5, 1.5))


def extract_store_ids_from_page(page):
    """Extract unique seller store IDs from current page HTML."""
    html = page.content()
    store_ids = set(re.findall(r'shop\.coupang\.com/(A\d+)', html))
    product_ids = set(re.findall(r'/vp/products/(\d+)', html))
    return store_ids, product_ids


def fetch_vendor_info(page, vendor_id):
    """Call getStoreReview API to get vendor business info."""
    try:
        result = page.evaluate(f"""async () => {{
            try {{
                const params = new URLSearchParams({{
                    vendorId: '{vendor_id}',
                    urlName: '{vendor_id}'
                }});
                const r = await fetch('https://shop.coupang.com/api/v1/store/getStoreReview?' + params.toString(), {{
                    credentials: 'include'
                }});
                const text = await r.text();
                return {{status: r.status, body: text}};
            }} catch(e) {{
                return {{error: e.message}};
            }}
        }}""")

        if result.get("status") == 200 and result.get("body"):
            body = result["body"]
            if not body.strip().startswith("<!"):
                data = json.loads(body)
                return data
        return None
    except Exception:
        return None


def crawl_categories(page, category_ids, max_pages=3):
    """Browse category pages and collect all unique store IDs."""
    all_store_ids = set()
    all_product_ids = set()

    for cat_id in category_ids:
        for page_num in range(1, max_pages + 1):
            url = f"https://www.coupang.com/np/categories/{cat_id}?page={page_num}"
            print(f"    Loading category {cat_id} page {page_num}...")

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(2)
                natural_interaction(page, duration=random.uniform(5, 10))

                # Scroll to load lazy content
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(1.5)

                store_ids, product_ids = extract_store_ids_from_page(page)
                new_stores = store_ids - all_store_ids
                all_store_ids.update(store_ids)
                all_product_ids.update(product_ids)

                print(f"      Found {len(store_ids)} stores ({len(new_stores)} new), {len(product_ids)} products")

                if not store_ids and page_num > 1:
                    break  # No more results

            except Exception as e:
                print(f"      Error: {e}")
                break

            # Rate limiting between pages
            time.sleep(random.uniform(2.0, 4.0))

    return all_store_ids, all_product_ids


def batch_fetch_vendor_info(page, store_ids, delay_range=(1.0, 2.5)):
    """Fetch vendor business info for all store IDs."""
    results = []
    total = len(store_ids)

    for i, vendor_id in enumerate(sorted(store_ids)):
        data = fetch_vendor_info(page, vendor_id)

        if data and data.get("name"):
            record = {
                "vendor_id": vendor_id,
                "company_name": data.get("name"),
                "ceo_name": data.get("repPersonName"),
                "business_number": data.get("businessNumber"),
                "phone": data.get("repPhoneNum"),
                "email": data.get("repEmail"),
                "address": f"{data.get('repAddr1', '')} {data.get('repAddr2', '')}".strip(),
                "ecommerce_report_number": data.get("eCommerceReportNumber"),
                "power_seller": bool(data.get("qualitySellerBadgeDto")),
                "power_seller_title": (data.get("qualitySellerBadgeDto") or {}).get("qualityTitle", ""),
                "rating_count": data.get("ratingCount", 0),
                "thumb_up_ratio": data.get("thumbUpRatio", 0),
                "seller_review_link": data.get("sellerReviewDetailLink"),
            }
            results.append(record)
            power = " [POWER]" if record["power_seller"] else ""
            print(f"    [{i+1:3d}/{total}] {vendor_id}: {record['company_name']} | "
                  f"{record['ceo_name']} | {record['business_number']}{power}")
        elif data:
            print(f"    [{i+1:3d}/{total}] {vendor_id}: (brand seller - no individual info)")
        else:
            print(f"    [{i+1:3d}/{total}] {vendor_id}: (error/no response)")

        # Rate limiting
        time.sleep(random.uniform(*delay_range))

    return results


def save_results(results, output_prefix):
    """Save results to JSON and CSV."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # JSON
    json_path = os.path.join(OUTPUT_DIR, f"{output_prefix}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    # CSV
    csv_path = os.path.join(OUTPUT_DIR, f"{output_prefix}.csv")
    if results:
        fieldnames = list(results[0].keys())
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)

    return json_path, csv_path


def main():
    parser = argparse.ArgumentParser(description="Coupang Seller Business Info Crawler")
    parser.add_argument("--categories", nargs="+", default=DEFAULT_CATEGORIES[:2],
                        help="Category IDs to crawl")
    parser.add_argument("--max-pages", type=int, default=2,
                        help="Max pages per category")
    parser.add_argument("--output", default=None,
                        help="Output file prefix")
    parser.add_argument("--warmup-time", type=int, default=20,
                        help="Warmup interaction time in seconds")
    parser.add_argument("--delay-min", type=float, default=1.0,
                        help="Min delay between API calls")
    parser.add_argument("--delay-max", type=float, default=2.5,
                        help="Max delay between API calls")
    args = parser.parse_args()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_prefix = args.output or f"coupang_sellers_{ts}"

    print(f"{'='*60}")
    print(f"Coupang Seller Business Info Crawler v1.0")
    print(f"Time: {ts}")
    print(f"Categories: {args.categories}")
    print(f"Max pages/category: {args.max_pages}")
    print(f"{'='*60}")

    with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
        page = browser.new_page()

        # Phase 1: Warmup
        print(f"\n[Phase 1] Warmup ({args.warmup_time}s interaction)...")
        page.goto(COUPANG_HOME, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        natural_interaction(page, duration=args.warmup_time)
        print("  Done. Akamai _abck should be validated.")

        # Phase 2: Crawl categories
        print(f"\n[Phase 2] Crawling categories for store IDs...")
        store_ids, product_ids = crawl_categories(page, args.categories, args.max_pages)
        print(f"\n  Total unique stores: {len(store_ids)}")
        print(f"  Total unique products: {len(product_ids)}")
        print(f"  Store IDs: {sorted(store_ids)}")

        if not store_ids:
            print("\n  ERROR: No store IDs found. Exiting.")
            return

        # Phase 3: Establish shop.coupang.com session
        print(f"\n[Phase 3] Establishing shop.coupang.com session...")
        first_store = sorted(store_ids)[0]
        page.goto(f"https://shop.coupang.com/{first_store}", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        natural_interaction(page, duration=5)
        print("  Session established.")

        # Phase 4: Batch fetch vendor info
        print(f"\n[Phase 4] Fetching vendor business info ({len(store_ids)} stores)...")
        results = batch_fetch_vendor_info(page, store_ids, (args.delay_min, args.delay_max))

        # Phase 5: Save results
        print(f"\n[Phase 5] Saving results...")
        json_path, csv_path = save_results(results, output_prefix)

        # Summary
        print(f"\n{'='*60}")
        print(f"RESULTS SUMMARY")
        print(f"{'='*60}")
        print(f"  Stores crawled: {len(store_ids)}")
        print(f"  With business info: {len(results)}")
        print(f"  Power sellers: {sum(1 for r in results if r['power_seller'])}")
        print(f"  JSON: {json_path}")
        print(f"  CSV:  {csv_path}")

        if results:
            print(f"\n  Sample records:")
            for r in results[:5]:
                print(f"    {r['company_name']} | {r['ceo_name']} | {r['business_number']} | {r['phone']}")

        print(f"\n[DONE] Crawl complete at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
