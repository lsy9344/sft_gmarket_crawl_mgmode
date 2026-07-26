"""
Coupang /np/omp Seller Business Info Crawler v3.0
==================================================
Target: https://www.coupang.com/np/omp ('전체' tab - infinite scroll)

Pipeline:
1. Camoufox warmup (headed+xvfb, humanize) -> passes Akamai
2. Load /np/omp, capture getPromotion request template (device fingerprint)
3. Replay getPromotion with continuationToken pagination -> all vendorItemIds
4. individualInfo/products API -> map vendorItemIds to vendorIds
5. getStoreReview API -> full seller business info
6. Output: JSON + CSV

Usage:
    xvfb-run -a python coupang_omp_crawler.py [--max-scroll-pages N] [--output FILE]
"""
import time
import json
import random
import os
import csv
import argparse
from datetime import datetime

from camoufox.sync_api import Camoufox

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
OMP_URL = "https://www.coupang.com/np/omp"
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


def fetch_promotion_page(page, template, token):
    """Replay getPromotion with a continuationToken. Returns (items, next_token)."""
    template["query"]["continuationToken"] = token
    template["query"]["nextPageKey"] = token
    body_json = json.dumps(template)

    result = page.evaluate("""async (bodyStr) => {
        try {
            const r = await fetch("https://www.coupang.com/np/omp/api/getPromotion", {
                method: "POST",
                credentials: "include",
                headers: {"Content-Type": "application/json"},
                body: bodyStr
            });
            const text = await r.text();
            return {status: r.status, body: text};
        } catch(e) { return {error: e.message}; }
    }""", body_json)

    body = result.get("body", "")
    if body and not body.strip().startswith("<!"):
        data = json.loads(body)
        # NOTE: ret is the string "0" on success
        if str(data.get("ret")) == "0":
            d = data.get("data", {})
            return d.get("promotionData", []) or [], d.get("token")
    return [], None


def get_vendors_for_items(page, vendor_item_ids, store_id=109671, vendor_id="A00067881"):
    """Map vendorItemIds to vendorIds via individualInfo/products."""
    try:
        result = page.evaluate("""async (args) => {
            try {
                const r = await fetch("https://shop.coupang.com/api/v2/store/individualInfo/products", {
                    method: "POST",
                    credentials: "include",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({
                        vendorItemIds: args.viids,
                        isVIBased: true,
                        storeId: args.storeId,
                        vendorId: args.vendorId,
                        ignoreAdultCheck: false,
                        pageType: 3
                    })
                });
                const text = await r.text();
                return {status: r.status, body: text};
            } catch(e) { return {error: e.message}; }
        }""", {"viids": vendor_item_ids, "storeId": store_id, "vendorId": vendor_id})

        if result.get("status") == 200 and result.get("body"):
            body = result["body"]
            if not body.strip().startswith("<!"):
                data = json.loads(body)
                if data.get("code") == 200 and data.get("data", {}).get("products"):
                    vendors = {}
                    for product in data["data"]["products"]:
                        store_info = product.get("storeInfoArea", {})
                        vid = store_info.get("vendorId")
                        if vid:
                            vendors[vid] = {
                                "vendorId": vid,
                                "storeId": store_info.get("storeId"),
                                "displayName": store_info.get("displayName"),
                                "productId": product.get("productId"),
                                "itemId": product.get("itemId"),
                                "vendorItemId": product.get("vendorItemId"),
                            }
                    return vendors
        return {}
    except Exception:
        return {}


def fetch_vendor_business_info(page, vendor_id):
    """Get full business info via getStoreReview."""
    try:
        result = page.evaluate("""async (vid) => {
            try {
                const params = new URLSearchParams({vendorId: vid, urlName: vid});
                const r = await fetch('https://shop.coupang.com/api/v1/store/getStoreReview?' + params.toString(), {credentials: 'include'});
                const text = await r.text();
                return {status: r.status, body: text};
            } catch(e) { return {error: e.message}; }
        }""", vendor_id)

        if result.get("status") == 200 and result.get("body"):
            body = result["body"]
            if not body.strip().startswith("<!"):
                return json.loads(body)
        return None
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description="Coupang /np/omp Seller Crawler v3")
    parser.add_argument("--max-scroll-pages", type=int, default=10,
                        help="Max getPromotion pages to fetch (50 items each)")
    parser.add_argument("--batch-size", type=int, default=10,
                        help="VendorItemIds per individualInfo batch")
    parser.add_argument("--warmup-time", type=int, default=20)
    parser.add_argument("--delay-min", type=float, default=1.0)
    parser.add_argument("--delay-max", type=float, default=2.5)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_prefix = args.output or f"coupang_omp_sellers_{ts}"
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f"{'='*60}")
    print(f"Coupang /np/omp Seller Crawler v3.0")
    print(f"Time: {ts}")
    print(f"Target: {OMP_URL}")
    print(f"Max scroll pages: {args.max_scroll_pages}")
    print(f"{'='*60}")

    request_template = [None]

    with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
        page = browser.new_page()

        def on_request(request):
            if "getPromotion" in request.url and request_template[0] is None:
                try:
                    request_template[0] = request.post_data
                except Exception:
                    pass

        page.on("request", on_request)

        # Phase 1: Warmup
        print(f"\n[Phase 1] Warmup ({args.warmup_time}s)...")
        page.goto(COUPANG_HOME, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        natural_interaction(page, duration=args.warmup_time)
        print("  Akamai validated.")

        # Phase 2: Load /np/omp and capture request template
        print(f"\n[Phase 2] Loading {OMP_URL}...")
        page.goto(OMP_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(4)
        natural_interaction(page, duration=8)

        if not request_template[0]:
            print("  ERROR: getPromotion request not captured. Exiting.")
            return

        template = json.loads(request_template[0])
        print(f"  Request template captured. feedId: {template['query'].get('feedId')}")

        # Phase 3: Paginate getPromotion to collect all vendorItemIds
        print(f"\n[Phase 3] Fetching products via getPromotion pagination...")
        all_items = {}  # viid -> item info
        token = template["query"].get("continuationToken", "seemore=CGs=")

        for page_num in range(args.max_scroll_pages):
            items, next_token = fetch_promotion_page(page, template, token)

            new_count = 0
            for item in items:
                viid = item.get("vendorItemId")
                if viid and viid not in all_items:
                    all_items[viid] = {
                        "itemId": item.get("itemId"),
                        "title": item.get("title", ""),
                        "categoryId": item.get("categoryId"),
                    }
                    new_count += 1

            print(f"    Page {page_num+1}: {len(items)} items, {new_count} new, total={len(all_items)}")

            if not items or new_count == 0 or not next_token or next_token == token:
                print(f"    Reached end of feed.")
                break

            token = next_token
            time.sleep(random.uniform(1.0, 2.0))

        viids = list(all_items.keys())
        print(f"\n  Total unique vendorItemIds: {len(viids)}")

        if not viids:
            print("  ERROR: No items found. Exiting.")
            return

        # Phase 4: Establish shop session + map items to vendors
        print(f"\n[Phase 4] Mapping products to vendors...")
        page.goto("https://shop.coupang.com/A00067881", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        natural_interaction(page, duration=5)

        all_vendors = {}
        for i in range(0, len(viids), args.batch_size):
            batch = viids[i:i + args.batch_size]
            vendors = get_vendors_for_items(page, batch)
            all_vendors.update(vendors)
            print(f"    Batch {i//args.batch_size + 1}: {len(batch)} items -> {len(vendors)} vendors")
            time.sleep(random.uniform(1.0, 2.0))

        unique_vendor_ids = set(all_vendors.keys())
        print(f"\n  Unique vendors discovered: {len(unique_vendor_ids)}")

        # Phase 5: Fetch business info
        print(f"\n[Phase 5] Fetching business info for {len(unique_vendor_ids)} vendors...")
        results = []

        for i, vendor_id in enumerate(sorted(unique_vendor_ids)):
            data = fetch_vendor_business_info(page, vendor_id)

            if data and data.get("name"):
                vinfo = all_vendors.get(vendor_id, {})
                pid = vinfo.get("productId")
                iid = vinfo.get("itemId")
                viid = vinfo.get("vendorItemId")
                url = (f"https://www.coupang.com/vp/products/{pid}"
                       f"?itemId={iid}&vendorItemId={viid}") if pid and iid and viid else ""
                record = {
                    "vendor_id": vendor_id,
                    "url": url,
                    "store_name": vinfo.get("displayName", ""),
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

        # Phase 6: Save results
        print(f"\n[Phase 6] Saving results...")
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
        print(f"  Products scraped: {len(all_items)}")
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
