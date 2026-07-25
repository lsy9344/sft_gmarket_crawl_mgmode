"""
Coupang Seller Business Info Crawler v2.0
==========================================
Full pipeline:
1. Camoufox warmup (headed+xvfb, humanize) -> passes Akamai
2. Category page -> extract vendorItemIds from product links
3. individualInfo/products API -> map products to vendorIds
4. getStoreReview API -> full seller business info
5. Output: JSON + CSV

Fields: vendor_id, store_name, company_name, ceo_name, business_number,
        address, phone, email, ecommerce_report_number, power_seller,
        rating_count, thumb_up_ratio

Usage:
    xvfb-run -a python coupang_seller_crawler_v2.py [--categories IDS] [--max-pages N]
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
    "497202",   # 베이비
    "178602",   # 생활/주방
    "497197",   # 식품
    "497201",   # 스포츠/레저
    "497196",   # 가전/디지털
]

COUPANG_HOME = "https://www.coupang.com/"


def natural_interaction(page, duration=15):
    """Simulate natural mouse/scroll to pass Akamai behavioral check."""
    end_time = time.time() + duration
    while time.time() < end_time:
        action = random.choice(["move", "scroll", "pause", "move", "scroll"])
        if action == "move":
            page.mouse.move(random.randint(100, 900), random.randint(100, 600))
            time.sleep(random.uniform(0.1, 0.4))
        elif action == "scroll":
            page.mouse.wheel(0, random.randint(50, 300))
            time.sleep(random.uniform(0.3, 0.8))
        else:
            time.sleep(random.uniform(0.5, 1.5))


def extract_vendor_item_ids(page):
    """Extract vendorItemIds from product links on current page."""
    return page.evaluate("""() => {
        const links = document.querySelectorAll('a[href*="vendorItemId"]');
        const results = [];
        const seen = new Set();
        for (const link of links) {
            const href = link.href || '';
            const viidMatch = href.match(/vendorItemId=(\\d+)/);
            const pidMatch = href.match(/products\\/(\\d+)/);
            if (viidMatch && !seen.has(viidMatch[1])) {
                seen.add(viidMatch[1]);
                results.push({
                    vendorItemId: parseInt(viidMatch[1]),
                    productId: pidMatch ? parseInt(pidMatch[1]) : null
                });
            }
        }
        return results;
    }""")


def get_vendors_for_items(page, vendor_item_ids, store_id=109671, vendor_id="A00067881"):
    """Call individualInfo/products to map vendorItemIds to vendorIds."""
    try:
        result = page.evaluate(f"""async () => {{
            try {{
                const r = await fetch("https://shop.coupang.com/api/v2/store/individualInfo/products", {{
                    method: "POST",
                    credentials: "include",
                    headers: {{"Content-Type": "application/json"}},
                    body: JSON.stringify({{
                        vendorItemIds: {json.dumps(vendor_item_ids)},
                        isVIBased: true,
                        storeId: {store_id},
                        vendorId: "{vendor_id}",
                        ignoreAdultCheck: false,
                        pageType: 3
                    }})
                }});
                const text = await r.text();
                return {{status: r.status, body: text}};
            }} catch(e) {{ return {{error: e.message}}; }}
        }}""")

        if result.get("status") == 200 and result.get("body"):
            body = result["body"]
            if not body.strip().startswith("<!"):
                data = json.loads(body)
                if data.get("code") == 200 and data.get("data", {}).get("products"):
                    vendors = {}
                    for product in data["data"]["products"]:
                        store_info = product.get("storeInfoArea", {})
                        vid = store_info.get("vendorId")
                        sid = store_info.get("storeId")
                        name = store_info.get("displayName")
                        viid = product.get("vendorItemId")
                        if vid:
                            vendors[vid] = {
                                "vendorId": vid,
                                "storeId": sid,
                                "displayName": name,
                                "vendorItemId": viid
                            }
                    return vendors
        return {}
    except Exception:
        return {}


def fetch_vendor_business_info(page, vendor_id):
    """Call getStoreReview API for full business info."""
    try:
        result = page.evaluate(f"""async () => {{
            try {{
                const params = new URLSearchParams({{vendorId: '{vendor_id}', urlName: '{vendor_id}'}});
                const r = await fetch('https://shop.coupang.com/api/v1/store/getStoreReview?' + params.toString(), {{credentials: 'include'}});
                const text = await r.text();
                return {{status: r.status, body: text}};
            }} catch(e) {{ return {{error: e.message}}; }}
        }}""")

        if result.get("status") == 200 and result.get("body"):
            body = result["body"]
            if not body.strip().startswith("<!"):
                return json.loads(body)
        return None
    except Exception:
        return None


def crawl_category_vendor_items(page, category_ids, max_pages=3):
    """Browse categories and collect all vendorItemIds."""
    all_items = []
    seen_viids = set()

    for cat_id in category_ids:
        for page_num in range(1, max_pages + 1):
            url = f"https://www.coupang.com/np/categories/{cat_id}?page={page_num}"
            print(f"    Category {cat_id} page {page_num}...")

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(2)
                natural_interaction(page, duration=random.uniform(5, 10))

                # Scroll to load lazy content
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(1.5)

                items = extract_vendor_item_ids(page)
                new_items = [i for i in items if i["vendorItemId"] not in seen_viids]
                for item in new_items:
                    seen_viids.add(item["vendorItemId"])
                all_items.extend(new_items)

                print(f"      {len(items)} items ({len(new_items)} new)")

                if not items and page_num > 1:
                    break

            except Exception as e:
                print(f"      Error: {e}")
                break

            time.sleep(random.uniform(2.0, 4.0))

    return all_items


def main():
    parser = argparse.ArgumentParser(description="Coupang Seller Business Info Crawler v2")
    parser.add_argument("--categories", nargs="+", default=DEFAULT_CATEGORIES[:2])
    parser.add_argument("--max-pages", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=10,
                        help="VendorItemIds per API batch")
    parser.add_argument("--warmup-time", type=int, default=20)
    parser.add_argument("--delay-min", type=float, default=1.0)
    parser.add_argument("--delay-max", type=float, default=2.5)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_prefix = args.output or f"coupang_sellers_{ts}"
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f"{'='*60}")
    print(f"Coupang Seller Business Info Crawler v2.0")
    print(f"Time: {ts}")
    print(f"Categories: {args.categories}")
    print(f"Max pages/category: {args.max_pages}")
    print(f"{'='*60}")

    with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
        page = browser.new_page()

        # Phase 1: Warmup
        print(f"\n[Phase 1] Warmup ({args.warmup_time}s)...")
        page.goto(COUPANG_HOME, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        natural_interaction(page, duration=args.warmup_time)
        print("  Akamai validated.")

        # Phase 2: Collect vendorItemIds from categories
        print(f"\n[Phase 2] Collecting vendorItemIds from categories...")
        all_items = crawl_category_vendor_items(page, args.categories, args.max_pages)
        print(f"\n  Total vendorItemIds: {len(all_items)}")

        if not all_items:
            print("  ERROR: No items found. Exiting.")
            return

        # Phase 3: Establish shop session + map items to vendors
        print(f"\n[Phase 3] Mapping products to vendors...")
        page.goto("https://shop.coupang.com/A00067881", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        natural_interaction(page, duration=5)

        all_vendors = {}
        viids = [item["vendorItemId"] for item in all_items]

        for i in range(0, len(viids), args.batch_size):
            batch = viids[i:i + args.batch_size]
            vendors = get_vendors_for_items(page, batch)
            all_vendors.update(vendors)
            print(f"    Batch {i//args.batch_size + 1}: {len(batch)} items -> {len(vendors)} vendors")
            time.sleep(random.uniform(1.0, 2.0))

        unique_vendor_ids = set(all_vendors.keys())
        print(f"\n  Unique vendors discovered: {len(unique_vendor_ids)}")
        for vid in sorted(unique_vendor_ids):
            info = all_vendors[vid]
            print(f"    {vid}: {info.get('displayName', '?')} (store {info.get('storeId')})")

        # Phase 4: Fetch business info for each vendor
        print(f"\n[Phase 4] Fetching business info for {len(unique_vendor_ids)} vendors...")
        results = []

        for i, vendor_id in enumerate(sorted(unique_vendor_ids)):
            data = fetch_vendor_business_info(page, vendor_id)

            if data and data.get("name"):
                store_display = all_vendors.get(vendor_id, {}).get("displayName", "")
                record = {
                    "vendor_id": vendor_id,
                    "store_name": store_display,
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
                }
                results.append(record)
                power = " [POWER]" if record["power_seller"] else ""
                print(f"    [{i+1:3d}/{len(unique_vendor_ids)}] {vendor_id}: "
                      f"{record['company_name']} | {record['ceo_name']} | "
                      f"{record['business_number']}{power}")
            elif data:
                print(f"    [{i+1:3d}/{len(unique_vendor_ids)}] {vendor_id}: (brand seller - no info)")
            else:
                print(f"    [{i+1:3d}/{len(unique_vendor_ids)}] {vendor_id}: (error)")

            time.sleep(random.uniform(args.delay_min, args.delay_max))

        # Phase 5: Save results
        print(f"\n[Phase 5] Saving results...")
        json_path = os.path.join(OUTPUT_DIR, f"{output_prefix}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        csv_path = os.path.join(OUTPUT_DIR, f"{output_prefix}.csv")
        if results:
            fieldnames = list(results[0].keys())
            with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(results)

        # Summary
        print(f"\n{'='*60}")
        print(f"RESULTS")
        print(f"{'='*60}")
        print(f"  Categories crawled: {len(args.categories)}")
        print(f"  VendorItemIds found: {len(all_items)}")
        print(f"  Unique vendors: {len(unique_vendor_ids)}")
        print(f"  With business info: {len(results)}")
        print(f"  Power sellers: {sum(1 for r in results if r['power_seller'])}")
        print(f"  JSON: {json_path}")
        print(f"  CSV:  {csv_path}")

        if results:
            print(f"\n  Sample:")
            for r in results[:5]:
                print(f"    {r['company_name']} | {r['ceo_name']} | {r['business_number']} | {r['phone']}")

        print(f"\n[DONE] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
