"""
Gmarket Fast Crawler - mg.gmarket.co.kr 경량 엔드포인트 기반
Phase 1: StealthySession으로 리스팅 페이지에서 goodscode 목록 추출 (카테고리당 1회)
Phase 2: mg.gmarket.co.kr/SellerInfo 직접 HTTP 호출 (브라우저 불필요, Cloudflare 없음)
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

import requests
from bs4 import BeautifulSoup

OUTPUT_DIR = Path(__file__).parent.parent / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

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

SELLER_INFO_URL = "https://mg.gmarket.co.kr/SellerInfo/SellerInfo?goodscode={}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}


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
    safe_label = re.sub(r'[/\\:*?"<>|]', '_', label) if label else ""
    suffix = f"_{safe_label}" if safe_label else ""
    json_path = OUTPUT_DIR / f"gmarket_fast{suffix}_{ts}.json"
    csv_path = OUTPUT_DIR / f"gmarket_fast{suffix}_{ts}.csv"

    json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    if results:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=results[0].keys())
            writer.writeheader()
            writer.writerows(results)

    return json_path, csv_path


def fetch_listing_codes(cat: dict) -> list[str]:
    """StealthySession으로 리스팅 페이지에서 goodscode 목록 추출 (1회성)"""
    from scrapling.fetchers import StealthySession

    is_best = "best" in cat["url"] and "superdeal" not in cat["url"]
    codes = []

    with StealthySession(headless=True) as session:
        session.fetch("https://www.gmarket.co.kr", timeout=20000)
        time.sleep(3)

        kwargs = {"timeout": 20000}
        if is_best:
            kwargs["wait_selector"] = 'a[href*="goodscode"]'
            kwargs["timeout"] = 15000

        r = session.fetch(cat["url"], **kwargs)
        if r.status == 200:
            codes = re.findall(r'goodscode=(\d+)', r.html_content)
            codes = list(dict.fromkeys(codes))

    return codes


def fetch_seller_info_fast(goodscode: str, session: requests.Session) -> dict | None:
    """mg.gmarket.co.kr에서 판매자정보 직접 HTTP GET (브라우저 불필요)"""
    url = SELLER_INFO_URL.format(goodscode)

    try:
        r = session.get(url, headers=HEADERS, timeout=10, allow_redirects=False)
    except requests.RequestException as e:
        print(f"    [ERR] {goodscode}: {e}")
        return None

    if r.status_code == 302 or r.status_code != 200:
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    info = {
        "goodscode": goodscode,
        "url": f"https://item.gmarket.co.kr/Item?goodscode={goodscode}",
        "store_name": "",
        "company_name": "",
        "ceo_name": "",
        "email": "",
        "phone": "",
        "business_number": "",
        "address": "",
        "collected_at": datetime.now().isoformat(),
    }

    store_el = soup.select_one("h3.store-title")
    if store_el:
        info["store_name"] = store_el.get_text(strip=True)

    label_map = {
        "seller": "company_name",
        "representative": "phone",
        "e-mail": "email",
        "business registration no": "business_number",
        "address": "address",
    }

    for row in soup.select("ul.vip-seller-info-list li.list-row"):
        label_el = row.select_one("span.list-label")
        value_el = row.select_one("span.list-selection")
        if not label_el or not value_el:
            continue

        label_text = label_el.get_text(strip=True).lower()
        value_text = value_el.get_text(strip=True)

        if not value_text:
            continue

        for key, field in label_map.items():
            if key in label_text:
                info[field] = value_text
                break

    filled = sum(1 for k, v in info.items()
                 if v and k not in ("goodscode", "url", "collected_at", "source", "address", "ceo_name"))
    if filled == 0:
        return None

    return info


def crawl_category_fast(cat: dict, source: str, max_items: int = 200, delay: float = 0.5) -> tuple[list[dict], dict]:
    """카테고리 1개: 리스팅 추출 → 판매자정보 경량 수집"""
    collected_ids = load_collected_ids()
    results = []
    stats = {"total": 0, "success": 0, "skip": 0, "fail": 0}

    print(f"\n{'='*60}")
    print(f"[{source}/{cat['name']}] 리스팅 추출 중...")
    print(f"{'='*60}")

    codes = fetch_listing_codes(cat)
    print(f"  추출된 goodscode: {len(codes)}개")

    new_codes = [c for c in codes if c not in collected_ids]
    targets = new_codes[:max_items]
    print(f"  신규 대상: {len(targets)}개 (이미 수집: {len(codes) - len(new_codes)})")

    if not targets:
        print("  수집할 항목 없음.")
        return results, stats

    http_session = requests.Session()
    stats["total"] = len(targets)

    for i, code in enumerate(targets):
        info = fetch_seller_info_fast(code, http_session)

        if info:
            info["source"] = cat["name"]
            results.append(info)
            collected_ids.add(code)
            stats["success"] += 1
            filled = sum(1 for k, v in info.items()
                         if v and k not in ("goodscode", "url", "collected_at", "source", "address"))
            print(f"  [{i+1}/{len(targets)}] {code} -> OK ({filled}/6) {info.get('store_name','')}")
        else:
            stats["fail"] += 1
            print(f"  [{i+1}/{len(targets)}] {code} -> MISS")

        if (i + 1) % 50 == 0:
            save_collected_ids(collected_ids)
            print(f"  --- 중간 저장: {stats['success']}개 성공 ---")

        time.sleep(delay + random.uniform(0, 0.3))

    save_collected_ids(collected_ids)
    http_session.close()
    return results, stats


def run_full_fast(max_items_per_category: int = 200, delay: float = 0.5):
    """전체 22개 카테고리 경량 수집"""
    state_file = OUTPUT_DIR / "fastcrawl_state.json"
    if state_file.exists():
        state = json.loads(state_file.read_text(encoding="utf-8"))
    else:
        state = {"completed": [], "total_success": 0}

    all_categories = []
    for cat in BEST_CATEGORIES:
        all_categories.append({"cat": cat, "source": "best"})
    for cat in SUPERDEAL_CATEGORIES:
        all_categories.append({"cat": cat, "source": "superdeal"})

    remaining = [c for c in all_categories if c["cat"]["name"] not in state["completed"]]

    print(f"[FAST CRAWL] 전체 {len(all_categories)}개 카테고리")
    print(f"[FAST CRAWL] 완료: {len(state['completed'])}, 남은: {len(remaining)}")
    print(f"[FAST CRAWL] 카테고리당 최대: {max_items_per_category}개")
    print(f"[FAST CRAWL] 요청 간격: {delay}s")
    print("=" * 60)

    all_results = []

    for entry in remaining:
        cat = entry["cat"]
        source = entry["source"]

        results, stats = crawl_category_fast(cat, source, max_items=max_items_per_category, delay=delay)
        all_results.extend(results)
        state["total_success"] += stats["success"]
        state["completed"].append(cat["name"])
        state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"\n  [결과] {cat['name']}: 성공 {stats['success']}, 실패 {stats['fail']}")
        print(f"  [누적] 총 {state['total_success']}개")

        if results:
            save_results(results, label=cat["name"])

        if remaining.index(entry) < len(remaining) - 1:
            wait = random.uniform(2, 5)
            print(f"  [대기] {wait:.1f}s...")
            time.sleep(wait)

    print(f"\n{'='*60}")
    print(f"[완료] 총 수집: {state['total_success']}개")
    print(f"{'='*60}")

    if all_results:
        jp, cp = save_results(all_results, label="ALL")
        print(f"  JSON: {jp}")
        print(f"  CSV:  {cp}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=200, help="카테고리당 최대 수집 수")
    parser.add_argument("--delay", type=float, default=0.5, help="요청 간격(초)")
    parser.add_argument("--category", type=str, default=None, help="특정 카테고리만 (예: 신선식품)")
    args = parser.parse_args()

    if args.category:
        all_cats = [(c, "best") for c in BEST_CATEGORIES] + [(c, "superdeal") for c in SUPERDEAL_CATEGORIES]
        match = [(c, s) for c, s in all_cats if args.category in c["name"]]
        if match:
            cat, source = match[0]
            results, stats = crawl_category_fast(cat, source, max_items=args.max, delay=args.delay)
            if results:
                jp, cp = save_results(results, label=cat["name"])
                print(f"\nJSON: {jp}")
                print(f"CSV:  {cp}")
        else:
            print(f"카테고리 '{args.category}' not found")
    else:
        run_full_fast(max_items_per_category=args.max, delay=args.delay)
