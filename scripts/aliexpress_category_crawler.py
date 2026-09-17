import os
import sys
import time
import json
import csv
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from camoufox.sync_api import Camoufox

OUTPUT_DIR = Path("/home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/output/aliexpress")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

CATEGORIES = [
    {
        "name": "여성_캐주얼_드레스",
        "display_name": "여성 캐주얼 드레스",
        "url": "https://ko.aliexpress.com/w/wholesale-%EC%97%AC%EC%84%B1-%EC%BA%90%EC%A3%BC%EC%96%BC-%EB%93%9C%EB%A0%88%EC%8A%A4.html?isFromCategory=y&categoryTab=women%27s_clothing"
    },
    {
        "name": "여성_바지",
        "display_name": "여성 바지",
        "url": "https://ko.aliexpress.com/w/wholesale-%EC%97%AC%EC%84%B1-%EB%B0%94%EC%A7%80.html?isFromCategory=y&categoryTab=women%27s_clothing"
    }
]

def clean_text(text):
    if not text:
        return ""
    return " ".join(text.split()).strip()

def extract_products_from_page(page):
    """현재 리스팅 페이지에서 상품 카드 정보들을 추출"""
    raw_products = page.evaluate("""() => {
        const items = [];
        const links = Array.from(document.querySelectorAll('a[href*=\"/item/\"], [class*=\"search-item\"], [class*=\"list--item\"]'));
        
        for (const el of links) {
            let aTag = el.tagName === 'A' ? el : el.querySelector('a[href*=\"/item/\"]');
            if (!aTag) continue;
            
            let href = aTag.href || aTag.getAttribute('href') || '';
            if (!href.includes('/item/')) continue;
            
            // Item ID
            const idMatch = href.match(/item\/(\\d+)\\.html/);
            const itemId = idMatch ? idMatch[1] : '';
            
            // Title
            let title = '';
            const titleEl = el.querySelector('h1, h2, h3, [class*=\"title\"], [class*=\"Title\"]') || aTag;
            title = titleEl.innerText || '';
            
            // Price
            let price = '';
            const priceEl = el.querySelector('[class*=\"price\"], [class*=\"Price\"]');
            if (priceEl) {
                price = priceEl.innerText || '';
            }
            
            // Rating & Orders
            let rating = '';
            let orders = '';
            const text = el.innerText || '';
            const lines = text.split('\\n').map(x => x.trim()).filter(Boolean);
            for (const l of lines) {
                if (l.includes('판매') || l.includes('orders') || l.includes('sold')) {
                    orders = l;
                }
                if (/^[3-5]\\.\\d$/.test(l)) {
                    rating = l;
                }
            }
            
            items.push({
                product_id: itemId,
                title: title.split('\\n')[0].trim(),
                url: href,
                price_text: price,
                rating: rating,
                orders: orders
            });
        }
        return items;
    }""")
    
    # 중복 제거 (product_id 기준)
    seen_ids = set()
    unique_products = []
    for p in raw_products:
        pid = p.get("product_id")
        if pid and pid not in seen_ids:
            seen_ids.add(pid)
            unique_products.append(p)
            
    return unique_products

def collect_seller_details(page, product_url):
    """상품 상세 페이지로 이동하여 판매자 및 스토어 사업자 상세 정보 수집"""
    try:
        page.goto(product_url, wait_until="domcontentloaded", timeout=35000)
        time.sleep(2.5)
        
        info = page.evaluate("""() => {
            const data = {
                store_name: '',
                store_url: '',
                store_id: '',
                seller_id: '',
                store_rating: '',
                followers: '',
                company_name: '',
                business_number: '',
                ceo_name: '',
                contact_phone: '',
                contact_email: '',
                seller_country: ''
            };
            
            // 1. 스토어 링크 및 이름
            const storeA = document.querySelector('a[href*=\"/store/\"]');
            if (storeA) {
                data.store_url = storeA.href;
                const m = storeA.href.match(/store\\/(\\d+)/);
                if (m) data.store_id = m[1];
            }
            
            // 스토어명 후보군
            const storeNameEls = document.querySelectorAll('[class*=\"seller-name\"], [class*=\"store-name\"], [class*=\"store-header\"], [class*=\"store--\"], a[href*=\"/store/\"]');
            for (const el of storeNameEls) {
                const txt = el.innerText.trim();
                if (txt && txt.length < 40 && !txt.includes('장바구니') && !txt.includes('방문')) {
                    data.store_name = txt.split('\\n')[0].trim();
                    break;
                }
            }
            
            // 2. 메시지 링크에서 sellerSeq / sellerAdminSeq 추출
            const msgLink = document.querySelector('a[href*=\"sellerSeq\"], a[href*=\"sellerAdminSeq\"]');
            if (msgLink) {
                const href = msgLink.href;
                const sm = href.match(/sellerSeq=(\\d+)/);
                if (sm) data.seller_id = sm[1];
            }
            
            // 3. 스토어 평점 / 팔로워
            const pageText = document.body.innerText;
            const evalMatch = pageText.match(/(\\d{1,2}\\.\\d)%\\s*(?:좋아요|긍정적인 평가|positive)/i);
            if (evalMatch) {
                data.store_rating = evalMatch[1] + '%';
            }
            
            const folMatch = pageText.match(/(\\d[\\d,]*)\\s*팔로/);
            if (folMatch) {
                data.followers = folMatch[1];
            }
            
            // 4. 국내 공정위/사업자정보 고시 (K-Venue 등)
            const bizNumMatch = pageText.match(/사업자등록번호[\\s:]*([0-9-]{10,12})/);
            if (bizNumMatch) {
                data.business_number = bizNumMatch[1].trim();
            }
            
            const compMatch = pageText.match(/상호(?:명)?[\\s:]*([^\\n\\r]+)/);
            if (compMatch) {
                data.company_name = compMatch[1].trim().slice(0, 50);
            }
            
            const ceoMatch = pageText.match(/대표자(?:명)?[\\s:]*([^\\n\\r]+)/);
            if (ceoMatch) {
                data.ceo_name = ceoMatch[1].trim().slice(0, 30);
            }
            
            const phoneMatch = pageText.match(/(?:연락처|전화번호)[\\s:]*([0-9-]{9,14})/);
            if (phoneMatch) {
                data.contact_phone = phoneMatch[1].trim();
            }
            
            const emailMatch = pageText.match(/(?:이메일|E-mail)[\\s:]*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,})/i);
            if (emailMatch) {
                data.contact_email = emailMatch[1].trim();
            }
            
            return data;
        }""")
        
        return info
    except Exception as e:
        print(f"      [상세 조회 오류]: {e}")
        return {}

def run_crawler():
    home_url = "https://ko.aliexpress.com/?spm=a2g0o.home.logo.1.6c2f52d1NGQ4SZ"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    print("=" * 70)
    print("알리익스프레스(AliExpress) 여성 의류 2개 카테고리 데이터 수집 시작")
    print(f"기점 홈 URL: {home_url}")
    print(f"수집 대상 카테고리: {[c['display_name'] for c in CATEGORIES]}")
    print(f"출력 디렉토리: {OUTPUT_DIR}")
    print("=" * 70)
    
    all_records = []
    
    with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
        page = browser.new_page()
        page.set_viewport_size({"width": 1440, "height": 900})
        
        # 1. 웜업: 지정된 홈 URL 진입
        print("\n[Phase 1] 홈 화면 웜업 및 보안 세션 형성...")
        resp = page.goto(home_url, wait_until="domcontentloaded", timeout=45000)
        print(f"  홈 접속 상태: {resp.status if resp else 'N/A'}")
        time.sleep(3)
        
        # 2. 카테고리별 수집 루프
        for cat_idx, cat in enumerate(CATEGORIES, 1):
            cat_name = cat["name"]
            cat_display = cat["display_name"]
            cat_url = cat["url"]
            cat_records = []
            
            print(f"\n[Phase 2.{cat_idx}] 카테고리 진입: {cat_display}")
            print(f"  URL: {cat_url}")
            
            resp = page.goto(cat_url, wait_until="domcontentloaded", timeout=45000)
            time.sleep(4)
            
            # 리스팅 스크롤하여 추가 상품 렌더링 유도
            page.evaluate("window.scrollBy(0, 1000)")
            time.sleep(2)
            page.evaluate("window.scrollBy(0, 1000)")
            time.sleep(2)
            
            # 상품 카드 추출
            products = extract_products_from_page(page)
            print(f"  -> 리스팅에서 발견된 상품 수: {len(products)}개")
            
            # 카테고리당 상위 10개 상품에 대해 상세 판매자 정보 수집
            target_products = products[:10]
            print(f"  -> 판매자 비즈니스 정보 상세 수집 진행 (대상: {len(target_products)}개)...")
            
            for p_idx, prod in enumerate(target_products, 1):
                p_title = prod.get("title", "")
                p_url = prod.get("url", "")
                if not p_url.startswith("http"):
                    p_url = "https:" + p_url
                    
                print(f"    [{p_idx}/{len(target_products)}] 상품: {p_title[:35]}...")
                
                # 상세 페이지 진입 후 판매자 정보 수집
                seller = collect_seller_details(page, p_url)
                
                record = {
                    "category": cat_display,
                    "product_id": prod.get("product_id", ""),
                    "product_title": p_title,
                    "product_url": p_url,
                    "price_info": prod.get("price_text", "").replace("\n", " "),
                    "product_rating": prod.get("rating", ""),
                    "product_orders": prod.get("orders", ""),
                    "store_name": seller.get("store_name", ""),
                    "store_url": seller.get("store_url", ""),
                    "store_id": seller.get("store_id", ""),
                    "seller_id": seller.get("seller_id", ""),
                    "store_rating": seller.get("store_rating", ""),
                    "followers": seller.get("followers", ""),
                    "company_name": seller.get("company_name", ""),
                    "business_number": seller.get("business_number", ""),
                    "ceo_name": seller.get("ceo_name", ""),
                    "contact_phone": seller.get("contact_phone", ""),
                    "contact_email": seller.get("contact_email", ""),
                    "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                
                cat_records.append(record)
                all_records.append(record)
                time.sleep(1.5)
                
            # 카테고리별 저장 (JSON)
            cat_json_path = OUTPUT_DIR / f"aliexpress_{cat_name}_{timestamp}.json"
            with open(cat_json_path, "w", encoding="utf-8") as f:
                json.dump(cat_records, f, ensure_ascii=False, indent=2)
            print(f"  [저장 완료] {cat_display} 결과: {len(cat_records)}건 -> {cat_json_path.name}")
            
            # 카테고리 간 안전 딜레이
            if cat_idx < len(CATEGORIES):
                print("  카테고리 전환 안전 딜레이 대기 (5초)...")
                time.sleep(5)
                
        # 3. 전체 통합 저장 (JSON 및 CSV)
        total_json_path = OUTPUT_DIR / f"aliexpress_women_clothing_total_{timestamp}.json"
        with open(total_json_path, "w", encoding="utf-8") as f:
            json.dump(all_records, f, ensure_ascii=False, indent=2)
            
        total_csv_path = OUTPUT_DIR / f"aliexpress_women_clothing_total_{timestamp}.csv"
        fieldnames = [
            "category", "product_id", "product_title", "price_info",
            "product_rating", "product_orders", "store_name", "store_id",
            "seller_id", "store_rating", "followers", "company_name",
            "business_number", "ceo_name", "contact_phone", "contact_email",
            "store_url", "product_url", "collected_at"
        ]
        with open(total_csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in all_records:
                writer.writerow(r)
                
        print("\n" + "=" * 70)
        print("수집 완료 요약:")
        print(f"- 총 수집 카테고리: {len(CATEGORIES)}개")
        print(f"- 총 수집 레코드: {len(all_records)}건")
        print(f"- 통합 JSON 파일: {total_json_path}")
        print(f"- 통합 CSV 파일: {total_csv_path}")
        print("=" * 70)

if __name__ == "__main__":
    run_crawler()
