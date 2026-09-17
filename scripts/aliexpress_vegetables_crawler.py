import os
import sys
import time
import json
import csv
import re
from datetime import datetime
from pathlib import Path
from camoufox.sync_api import Camoufox

OUTPUT_DIR = Path("/home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/output/aliexpress")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

HOME_URL = "https://ko.aliexpress.com/?spm=a2g0o.home.logo.1.6c2f52d1NGQ4SZ"
TARGET_CATEGORY = "식품과식료품 > 야채"
TARGET_URL = "https://ko.aliexpress.com/w/wholesale-%EC%95%BC%EC%B1%84.html?categoryTab=food_%26_grocery&isFromCategory=y"

def clean_text(text):
    if not text:
        return ""
    return " ".join(text.split()).strip()

def collect_vegetables_data(limit=15):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    print("=" * 75)
    print("알리익스프레스(AliExpress) [식품과식료품 > 야채] 무료 수집기 가동")
    print(f"- 기점 홈 URL (웜업): {HOME_URL}")
    print(f"- 대상 카테고리: {TARGET_CATEGORY}")
    print(f"- 대상 리스팅 URL: {TARGET_URL}")
    print(f"- 프록시 비용: 0원 (로컬 직접 회선 + Camoufox 안티디텍트)")
    print(f"- 저장 폴더: {OUTPUT_DIR}")
    print("=" * 75)
    
    collected_records = []
    
    with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
        page = browser.new_page()
        page.set_viewport_size({"width": 1440, "height": 900})
        
        # Phase 1: 홈 화면 웜업 (Akamai 및 알리 안티봇 신뢰 세션 확보)
        print("\n[Phase 1] 홈 화면 웜업 및 브라우저 세션 형성 (비용: 0원)...")
        resp = page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45000)
        print(f"  -> 홈 응답 코드: {resp.status if resp else 'N/A'}")
        time.sleep(2.5)
        
        # Phase 2: '식품과식료품 > 야채' 리스팅 진입
        print(f"\n[Phase 2] '{TARGET_CATEGORY}' 리스팅 페이지 진입...")
        resp = page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=45000)
        print(f"  -> 리스팅 응답 코드: {resp.status if resp else 'N/A'}")
        time.sleep(3)
        
        # 리스팅 스크롤하여 지연 로딩 상품 유도
        print("  -> 상품 목록 스크롤 렌더링...")
        page.evaluate("window.scrollBy(0, 1000)")
        time.sleep(2)
        page.evaluate("window.scrollBy(0, 1000)")
        time.sleep(2)
        
        # 상품 카드 파싱
        raw_products = page.evaluate("""() => {
            const list = [];
            const cards = Array.from(document.querySelectorAll('a[href*=\"/item/\"]'));
            
            for (const a of cards) {
                const href = a.href || '';
                const m = href.match(/item\\/(\\d+)\\.html/);
                if (!m) continue;
                const itemId = m[1];
                if (list.some(x => x.product_id === itemId)) continue;
                
                const card = a.closest('[class*=\"search-item\"], [class*=\"list--item\"], div') || a;
                const text = card.innerText || '';
                const lines = text.split('\\n').map(x => x.trim()).filter(Boolean);
                
                let title = a.title || '';
                if (!title) {
                    title = lines[0] || '';
                    if (title.startsWith('-') || title.startsWith('₩')) {
                        title = lines.find(l => !l.startsWith('-') && !l.startsWith('₩') && l.length > 5) || lines[0];
                    }
                }
                
                let price = '';
                const priceMatch = text.match(/₩[\\d,]+/);
                if (priceMatch) price = priceMatch[0];
                
                let rating = '';
                const ratingMatch = text.match(/\\b([3-5]\\.\\d)\\b/);
                if (ratingMatch) rating = ratingMatch[1];
                
                let orders = '';
                const orderMatch = text.match(/([\\d,]+(?:\\+|개)?\\s*판매)/);
                if (orderMatch) orders = orderMatch[1];
                
                list.push({
                    product_id: itemId,
                    title: title.trim(),
                    url: href,
                    price: price,
                    rating: rating,
                    orders: orders
                });
            }
            return list;
        }""")
        
        print(f"  -> 리스팅에서 총 {len(raw_products)}개 상품 발견")
        
        # Phase 3: 상품별 상세 정보 및 판매자 정보 수집
        targets = raw_products[:limit]
        print(f"\n[Phase 3] 상위 {len(targets)}개 상품 상세 및 판매자 스토어 정보 연계 수집...")
        
        for idx, prod in enumerate(targets, 1):
            p_id = prod["product_id"]
            p_title = prod["title"]
            p_url = prod["url"]
            print(f"  [{idx}/{len(targets)}] 상품 ID: {p_id} | {p_title[:30]}...")
            
            # 상세 페이지 이동
            try:
                page.goto(p_url, wait_until="domcontentloaded", timeout=35000)
                time.sleep(2.5)
                
                detail = page.evaluate("""() => {
                    const res = {
                        store_name: '',
                        store_url: '',
                        store_id: '',
                        seller_id: '',
                        store_rating: '',
                        followers: '',
                        seller_country: '',
                        store_open_date: '',
                        phone: '',
                        company_name: '',
                        business_number: '',
                        ceo_name: '',
                        origin: ''
                    };
                    
                    const text = document.body.innerText;
                    
                    // 스토어 정보
                    const storeA = document.querySelector('a[href*=\"/store/\"]');
                    if (storeA) {
                        res.store_url = storeA.href;
                        const sm = storeA.href.match(/store\\/(\\d+)/);
                        if (sm) res.store_id = sm[1];
                    }
                    
                    // 스토어 이름
                    const storeEls = document.querySelectorAll('a[href*=\"/store/\"], [class*=\"store-name\"], [class*=\"seller-name\"]');
                    for (const el of storeEls) {
                        const t = el.innerText.trim();
                        if (t && !t.includes('방문') && !t.includes('장바구니') && t.length < 40) {
                            res.store_name = t.split('\\n')[0].replace('판매자', '').trim();
                            break;
                        }
                    }
                    
                    // 셀러 ID (메시지 링크)
                    const msgA = document.querySelector('a[href*=\"sellerSeq\"], a[href*=\"sellerAdminSeq\"]');
                    if (msgA) {
                        const sm = msgA.href.match(/sellerSeq=(\\d+)/);
                        if (sm) res.seller_id = sm[1];
                    }
                    
                    // 평점 및 팔로워
                    const evalMatch = text.match(/(\\d{1,2}\\.\\d)%\\s*(?:좋아요|긍정적인 평가|positive)/i);
                    if (evalMatch) res.store_rating = evalMatch[1] + '%';
                    
                    const folMatch = text.match(/(\\d[\\d,.]*[KMk]?)\\s*팔로/);
                    if (folMatch) res.followers = folMatch[1];
                    
                    // 원산지
                    const origMatch = text.match(/원산지[\\s:]*([^\\n\\r]+)/);
                    if (origMatch) res.origin = origMatch[1].trim();
                    
                    // 고객센터 연락처
                    const phoneMatch = text.match(/(?:고객센터|연락처|전화번호)[\\s:]*([0-9-]{9,15})/);
                    if (phoneMatch) res.phone = phoneMatch[1].trim();
                    
                    // 사업자등록번호 등 (K-Venue 고시)
                    const bizMatch = text.match(/사업자(?:등록)?번호[\\s:]*([0-9-]{10,14})/);
                    if (bizMatch) res.business_number = bizMatch[1].trim();
                    
                    const compMatch = text.match(/상호(?:명)?[\\s:]*([^\\n\\r]+)/);
                    if (compMatch) res.company_name = compMatch[1].trim();
                    
                    const ceoMatch = text.match(/대표자(?:명)?[\\s:]*([^\\n\\r]+)/);
                    if (ceoMatch) res.ceo_name = ceoMatch[1].trim();
                    
                    return res;
                }""")
                
                # 스토어 피드백 페이지에서 개설 국가/개설일 추가 조회
                store_id = detail.get("store_id")
                if store_id:
                    feedback_url = f"https://ko.aliexpress.com/store/feedback-score/{store_id}.html"
                    try:
                        page.goto(feedback_url, wait_until="domcontentloaded", timeout=25000)
                        time.sleep(1.5)
                        fb_info = page.evaluate("""() => {
                            const t = document.body.innerText;
                            const m = t.match(/([a-zA-Z가-힣\\s]+)\\s*\\|\\s*From\\s*([a-zA-Z0-9,\\s]+)/);
                            if (m) {
                                return { country: m[1].trim(), open_date: m[2].trim() };
                            }
                            return {};
                        }""")
                        if fb_info.get("country"):
                            detail["seller_country"] = fb_info["country"]
                        if fb_info.get("open_date"):
                            detail["store_open_date"] = fb_info["open_date"]
                    except Exception:
                        pass
                
                record = {
                    "category_path": TARGET_CATEGORY,
                    "product_id": p_id,
                    "product_title": p_title,
                    "price": prod.get("price", ""),
                    "rating": prod.get("rating", ""),
                    "orders": prod.get("orders", ""),
                    "origin": detail.get("origin", ""),
                    "store_name": detail.get("store_name", ""),
                    "store_id": detail.get("store_id", ""),
                    "seller_id": detail.get("seller_id", ""),
                    "seller_country": detail.get("seller_country", ""),
                    "store_open_date": detail.get("store_open_date", ""),
                    "store_rating": detail.get("store_rating", ""),
                    "followers": detail.get("followers", ""),
                    "contact_phone": detail.get("phone", ""),
                    "company_name": detail.get("company_name", ""),
                    "business_number": detail.get("business_number", ""),
                    "ceo_name": detail.get("ceo_name", ""),
                    "product_url": p_url,
                    "store_url": detail.get("store_url", ""),
                    "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                
                collected_records.append(record)
                time.sleep(1.5)
                
            except Exception as e:
                print(f"    [상세 수집 실패]: {e}")
                time.sleep(2)
                
        # Phase 4: 정형 데이터 저장
        print("\n[Phase 4] 수집 결과 파일 저장...")
        json_file = OUTPUT_DIR / f"aliexpress_vegetables_{timestamp}.json"
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(collected_records, f, ensure_ascii=False, indent=2)
            
        csv_file = OUTPUT_DIR / f"aliexpress_vegetables_{timestamp}.csv"
        fields = [
            "category_path", "product_id", "product_title", "price", "rating",
            "orders", "origin", "store_name", "store_id", "seller_id",
            "seller_country", "store_open_date", "store_rating", "followers",
            "contact_phone", "company_name", "business_number", "ceo_name",
            "product_url", "store_url", "collected_at"
        ]
        with open(csv_file, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for r in collected_records:
                writer.writerow(r)
                
        print("=" * 75)
        print("수집 완료!")
        print(f"- 수집 레코드 수: {len(collected_records)}건")
        print(f"- JSON 파일: {json_file}")
        print(f"- CSV 파일: {csv_file}")
        print("=" * 75)
        
        return json_file, csv_file, collected_records

if __name__ == "__main__":
    collect_vegetables_data(limit=10)
