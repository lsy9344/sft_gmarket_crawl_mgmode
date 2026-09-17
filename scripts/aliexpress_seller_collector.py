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

# 쿠팡 카테고리 데이터셋 규격과 동일한 필드 정의
COUPANG_COMPATIBLE_FIELDS = [
    "vendor_id",                 # 판매자 고유 식별자 (sellerId / store_id)
    "url",                       # 상품 URL
    "store_name",                # 스토어명
    "company_name",              # 사업자명 (상호명 / 법인명)
    "ceo_name",                  # 대표자명
    "business_number",           # 사업자등록번호
    "phone",                     # 사업자 전화번호 (소비자상담 / 고객센터)
    "email",                     # 이메일 주소
    "address",                   # 사업장 주소 / 소재지
    "ecommerce_report_number",   # 통신판매업신고번호
    "power_seller",              # 우수 판매자 여부 (TOP셀러)
    "power_seller_title",        # 우수 판매자 배지명
    "rating_count",              # 누적 리뷰 / 판매 건수
    "thumb_up_ratio",            # 긍정 평가율 (만족도)
    "product_title",             # 상품명
    "price",                     # 판매 가격
    "collected_at"               # 수집 일시
]

def clean_value(val):
    if not val:
        return ""
    return str(val).replace("\n", " ").strip()

def collect_aliexpress_business_dataset(limit=10):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    print("=" * 80)
    print("알리익스프레스 [식품과식료품 > 야채] 쿠팡 규격 사업자 정보 정밀 수집기")
    print(f"- 기점 홈 (웜업): {HOME_URL}")
    print(f"- 카테고리: {TARGET_CATEGORY}")
    print(f"- 비용: 0원 (로컬 무료 직접 회선 + Camoufox 안티디텍트 브라우저)")
    print(f"- 대상 수집 건수: {limit}건")
    print("=" * 80)
    
    results = []
    
    with Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True) as browser:
        page = browser.new_page()
        page.set_viewport_size({"width": 1440, "height": 900})
        
        # 1. 홈 세션 웜업 (비용 0원)
        print("\n[단계 1] 홈 화면 웜업 및 보안 세션 확립...")
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45000)
        time.sleep(2.5)
        
        # 2. 카테고리 리스팅 진입
        print(f"\n[단계 2] 카테고리 리스팅 진입: {TARGET_URL}")
        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=45000)
        time.sleep(3)
        
        # 스크롤하여 상품 카드 확장
        page.evaluate("window.scrollBy(0, 1000)")
        time.sleep(2)
        page.evaluate("window.scrollBy(0, 1000)")
        time.sleep(2)
        
        # 상품 카드 파싱
        items = page.evaluate("""() => {
            const list = [];
            const links = Array.from(document.querySelectorAll('a[href*=\"/item/\"]'));
            for (const a of links) {
                const href = a.href || '';
                const m = href.match(/item\\/(\\d+)\\.html/);
                if (!m) continue;
                const id = m[1];
                if (list.some(x => x.id === id)) continue;
                
                const card = a.closest('[class*=\"search-item\"], [class*=\"list--item\"], div') || a;
                const text = card.innerText || '';
                
                let title = a.title || '';
                const lines = text.split('\\n').map(l => l.trim()).filter(Boolean);
                if (!title) {
                    title = lines.find(l => !l.startsWith('-') && !l.startsWith('₩') && l.length > 5) || lines[0] || '';
                }
                
                let price = '';
                const pm = text.match(/₩[\\d,]+/);
                if (pm) price = pm[0];
                
                let rating = '';
                const rm = text.match(/\\b([3-5]\\.\\d)\\b/);
                if (rm) rating = rm[1];
                
                let orders = '';
                const om = text.match(/([\\d,]+(?:\\+|개)?\\s*판매)/);
                if (om) orders = om[1];
                
                const isTop = text.includes('TOP셀러') || text.includes('Top Seller');
                
                list.push({
                    id: id,
                    url: href,
                    title: title.trim(),
                    price: price,
                    rating: rating,
                    orders: orders,
                    is_top_seller: isTop
                });
            }
            return list;
        }""")
        
        print(f"-> 리스팅에서 {len(items)}개 상품 확보")
        target_items = items[:limit]
        
        # 3. 상품별 사업자 정보 정밀 추출 (쿠팡 카테고리 계약과 동일)
        print(f"\n[단계 3] 상위 {len(target_items)}개 상품별 사업자 정보(대표자명, 사업자번호, 상호, 전화번호 등) 정밀 추출...")
        
        for idx, itm in enumerate(target_items, 1):
            p_id = itm["id"]
            p_url = itm["url"]
            p_title = itm["title"]
            print(f"\n  [{idx}/{len(target_items)}] 상품 ID: {p_id} | {p_title[:28]}...")
            
            try:
                page.goto(p_url, wait_until="domcontentloaded", timeout=40000)
                time.sleep(3)
                
                # 상세 페이지 렌더링 후 전자상거래 고시 및 스토어 사업자 정보 추출
                biz_data = page.evaluate("""() => {
                    const res = {
                        store_name: '',
                        company_name: '',
                        ceo_name: '',
                        business_number: '',
                        phone: '',
                        email: '',
                        address: '',
                        ecommerce_report_number: '',
                        vendor_id: '',
                        store_rating: '',
                        rating_count: ''
                    };
                    
                    const text = document.body.innerText;
                    
                    // 1. 스토어 기본 정보
                    const storeA = document.querySelector('a[href*=\"/store/\"]');
                    if (storeA) {
                        const m = storeA.href.match(/store\\/(\\d+)/);
                        if (m) res.vendor_id = m[1];
                        res.store_name = storeA.innerText.split('\\n')[0].replace('판매자', '').trim();
                    }
                    
                    // 2. 전자상거래 등에서의 상품정보제공 고시 (specification)
                    const specProps = Array.from(document.querySelectorAll('[class*=\"specification--prop\"]'));
                    for (const p of specProps) {
                        const tEl = p.querySelector('[class*=\"specification--title\"]');
                        const dEl = p.querySelector('[class*=\"specification--desc\"]');
                        if (!tEl || !dEl) continue;
                        const title = tEl.innerText.trim();
                        const desc = dEl.innerText.trim();
                        
                        if (title.includes('제조업체') || title.includes('제조자') || title.includes('생산자') || title.includes('판매자') || title.includes('수입자')) {
                            if (desc && !desc.includes('상세페이지') && !desc.includes('참조')) {
                                res.company_name = desc;
                            }
                        }
                        if (title.includes('소비자상담') || title.includes('전화번호') || title.includes('연락처') || title.includes('A/S')) {
                            const pm = desc.match(/([0-9-]{9,15})/);
                            if (pm) res.phone = pm[1];
                            // 회사명이 포함된 경우 (예: "농업회사법인(유)감동 고객센터 1533-2210")
                            const compM = desc.match(/(.+?)(?:\\s*고객센터|\\s*상담실|[0-9])/);
                            if (compM && !res.company_name) {
                                res.company_name = compM[1].trim();
                            }
                        }
                        if (title.includes('소재지') || title.includes('주소') || title.includes('원산지')) {
                            if (!res.address) res.address = desc;
                        }
                    }
                    
                    // 3. 페이지 텍스트 내 정규식 백필 (사업자등록번호, 대표자, 이메일, 상호 등)
                    const bizNumMatch = text.match(/사업자(?:등록)?번호[\\s:]*([0-9-]{10,14})/);
                    if (bizNumMatch) res.business_number = bizNumMatch[1].trim();
                    
                    const ceoMatch = text.match(/(?:대표자(?:명)?|대표|성명)[\\s:]*([가-힣a-zA-Z]{2,10})/);
                    if (ceoMatch) res.ceo_name = ceoMatch[1].trim();
                    
                    const compMatch = text.match(/(?:상호(?:명)?|회사명|법인명)[\\s:]*([가-힣a-zA-Z0-9()㈜]{2,30})/);
                    if (compMatch && !res.company_name) res.company_name = compMatch[1].trim();
                    
                    const emailMatch = text.match(/(?:이메일|E-mail)[\\s:]*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,})/i);
                    if (emailMatch) res.email = emailMatch[1].trim();
                    
                    const telMatch = text.match(/통신판매업(?:신고번호)?[\\s:]*([^\\n\\r]+)/);
                    if (telMatch) res.ecommerce_report_number = telMatch[1].trim();
                    
                    // 4. 평점 및 만족도
                    const ratingMatch = text.match(/(\\d{1,2}\\.\\d)%\\s*(?:좋아요|긍정적인 평가|positive)/i);
                    if (ratingMatch) res.store_rating = ratingMatch[1] + '%';
                    
                    return res;
                }""")
                
                # 상호명이 아직 비어있다면 스토어명으로 백필
                comp_name = biz_data.get("company_name") or biz_data.get("store_name") or ""
                phone_num = biz_data.get("phone") or ""
                
                record = {
                    "vendor_id": biz_data.get("vendor_id") or p_id,
                    "url": p_url,
                    "store_name": biz_data.get("store_name") or "",
                    "company_name": comp_name,
                    "ceo_name": biz_data.get("ceo_name") or "",
                    "business_number": biz_data.get("business_number") or "",
                    "phone": phone_num,
                    "email": biz_data.get("email") or "",
                    "address": biz_data.get("address") or "",
                    "ecommerce_report_number": biz_data.get("ecommerce_report_number") or "",
                    "power_seller": "TRUE" if itm["is_top_seller"] else "FALSE",
                    "power_seller_title": "AliExpress TOP셀러" if itm["is_top_seller"] else "",
                    "rating_count": itm.get("orders") or itm.get("rating") or "",
                    "thumb_up_ratio": biz_data.get("store_rating") or "",
                    "product_title": p_title,
                    "price": itm.get("price") or "",
                    "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                
                print(f"    - 상호명: {record['company_name'] or '(미기재)'}")
                print(f"    - 전화번호: {record['phone'] or '(미기재)'}")
                print(f"    - 대표자명: {record['ceo_name'] or '(상세페이지 미노출)'}")
                print(f"    - 사업자등록번호: {record['business_number'] or '(상세페이지 미노출)'}")
                print(f"    - 스토어ID: {record['vendor_id']} | 우수판매자: {record['power_seller']}")
                
                results.append(record)
                time.sleep(1.5)
                
            except Exception as e:
                print(f"    [추출 에러]: {e}")
                time.sleep(2)
                
        # 4. CSV 및 JSON 파일 저장 (쿠팡 스키마 호환)
        csv_path = OUTPUT_DIR / f"aliexpress_seller_dataset_{timestamp}.csv"
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=COUPANG_COMPATIBLE_FIELDS)
            writer.writeheader()
            for r in results:
                writer.writerow(r)
                
        json_path = OUTPUT_DIR / f"aliexpress_seller_dataset_{timestamp}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
            
        print("\n" + "=" * 80)
        print("수집 완료 보고:")
        print(f"- 수집 총 건수: {len(results)}건")
        print(f"- CSV 파일: {csv_path}")
        print(f"- JSON 파일: {json_path}")
        print("=" * 80)
        
        return csv_path, json_path, results

if __name__ == "__main__":
    collect_aliexpress_business_dataset(limit=10)
