import os
import sys
import time
import json
import csv
import re
from datetime import datetime
from pathlib import Path
from patchright.sync_api import sync_playwright

OUTPUT_DIR = Path("/home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/output/aliexpress")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

HOME_URL = "https://ko.aliexpress.com/?spm=a2g0o.home.logo.1.6c2f52d1NGQ4SZ"
TARGET_CATEGORY = "식품과식료품 > 야채"
BASE_LISTING_URL = "https://ko.aliexpress.com/w/wholesale-%EC%95%BC%EC%B1%84.html?categoryTab=food_%26_grocery&isFromCategory=y"

COUPANG_DATASET_FIELDS = [
    "vendor_id",                 # 판매자/스토어 식별자
    "url",                       # 상품 상세 URL
    "store_name",                # 스토어명
    "company_name",              # 회사/상호명 (사업자명)
    "ceo_name",                  # 대표자명
    "business_number",           # 사업자등록번호
    "phone",                     # 사업자 전화번호 (소비자상담 / 고객센터)
    "email",                     # 사업자 이메일 주소
    "address",                   # 사업장 소재지 (주소)
    "ecommerce_report_number",   # 통신판매업신고번호
    "power_seller",              # 우수판매자 여부
    "power_seller_title",        # 우수판매자 배지 명칭
    "rating_count",              # 누적 판매량
    "thumb_up_ratio",            # 긍정 평가율 (만족도)
    "product_title",             # 대표 상품명
    "price",                     # 판매 가격
    "collected_at"               # 수집 일시
]

def run_full_category_crawl(max_pages=30):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_file = OUTPUT_DIR / f"aliexpress_vegetables_ALL_PAGES_{timestamp}.csv"
    json_file = OUTPUT_DIR / f"aliexpress_vegetables_ALL_PAGES_{timestamp}.json"
    
    print("=" * 80)
    print("알리익스프레스 [식품과식료품 > 야채] 전체 페이지 전량 수집기 가동")
    print(f"- 카테고리: {TARGET_CATEGORY}")
    print(f"- 최대 탐색 페이지: {max_pages} 페이지 (끝까지 순회)")
    print(f"- 비용: 0원 (로컬 무료 직접 회선)")
    print(f"- 수집 항목: 쿠팡 카테고리 규격 (이메일, 대표자, 사업자번호, 상호 등 7대 핵심 정보)")
    print(f"- 출력 CSV: {csv_file}")
    print(f"- 출력 JSON: {json_file}")
    print("=" * 80, flush=True)
    
    # CSV 헤더 사전 작성
    with open(csv_file, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COUPANG_DATASET_FIELDS)
        writer.writeheader()
        
    collected_records = []
    seen_vendor_ids = set()
    all_unique_products = {}
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            locale="ko-KR",
            viewport={"width": 1440, "height": 900}
        )
        page = context.new_page()
        
        # Phase 1: 홈 웜업
        print("\n[Phase 1] 홈 화면 웜업 및 보안 세션 형성...", flush=True)
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=40000)
        time.sleep(2.5)
        
        # Phase 2: 전체 페이지 상품 목록 전량 수집
        print(f"\n[Phase 2] '{TARGET_CATEGORY}' 1페이지부터 끝까지 전량 리스팅 탐색...", flush=True)
        empty_streak = 0
        
        for p_num in range(1, max_pages + 1):
            p_url = f"{BASE_LISTING_URL}&page={p_num}"
            print(f"  [페이지 {p_num}/{max_pages}] 목록 탐색 중...", flush=True)
            
            try:
                page.goto(p_url, wait_until="domcontentloaded", timeout=35000)
                time.sleep(2.0)
            except Exception as e:
                print(f"    페이지 로드 타임아웃/오류: {e}", flush=True)
                time.sleep(1.5)
                continue
                
            page.evaluate("window.scrollBy(0, 1500)")
            time.sleep(1.2)
            page.evaluate("window.scrollBy(0, 1500)")
            time.sleep(1.2)
            
            # 상품 카드 파싱
            items = page.evaluate("""() => {
                const list = [];
                const cards = Array.from(document.querySelectorAll('a[href*=\"/item/\"]'));
                for (const a of cards) {
                    const href = a.href || '';
                    const m = href.match(/item\\/(\\d+)\\.html/);
                    if (!m) continue;
                    const id = m[1];
                    if (list.some(x => x.id === id)) continue;
                    
                    const card = a.closest('[class*=\"search-item\"], [class*=\"list--item\"], div') || a;
                    const text = card.innerText || '';
                    const lines = text.split('\\n').map(l => l.trim()).filter(Boolean);
                    
                    let title = a.title || '';
                    if (!title) {
                        title = lines.find(l => !l.startsWith('-') && !l.startsWith('₩') && l.length > 5) || lines[0] || '';
                    }
                    
                    let price = '';
                    const pm = text.match(/₩[\\d,]+/);
                    if (pm) price = pm[0];
                    
                    let orders = '';
                    const om = text.match(/([\\d,]+(?:\\+|개)?\\s*판매)/);
                    if (om) orders = om[1];
                    
                    const isTop = text.includes('TOP셀러') || text.includes('Top Seller');
                    
                    list.push({
                        id: id,
                        url: href,
                        title: title.trim(),
                        price: price,
                        orders: orders,
                        is_top_seller: isTop
                    });
                }
                return list;
            }""")
            
            new_in_page = 0
            for itm in items:
                if itm["id"] not in all_unique_products:
                    all_unique_products[itm["id"]] = itm
                    new_in_page += 1
                    
            print(f"    -> 발견: {len(items)}개 (신규: {new_in_page}개, 누적 고유 상품: {len(all_unique_products)}개)", flush=True)
            
            if len(items) == 0 or new_in_page == 0:
                empty_streak += 1
                if empty_streak >= 2:
                    print(f"    -> 연속 2회 신규 상품 없음: 카테고리 전체 목록 끝 도달. (총 {len(all_unique_products)}개 고유 상품)", flush=True)
                    break
            else:
                empty_streak = 0
                
        # Phase 3: 고유 판매자 전량 수집 (모든 상품 순회)
        product_list = list(all_unique_products.values())
        total_prods = len(product_list)
        print(f"\n[Phase 3] 수집된 {total_prods}개 전체 상품 대상 판매자 및 사업자 정보(이메일, 대표자 등) 전량 추출 시작...", flush=True)
        
        for idx, itm in enumerate(product_list, 1):
            p_id = itm["id"]
            p_url = itm["url"]
            p_title = itm["title"]
            
            pdp_json_data = {}
            pdp_received = False
            
            def handle_response(response):
                nonlocal pdp_json_data, pdp_received
                try:
                    if "mtop.aliexpress.pdp.pc.query" in response.url and response.status == 200:
                        txt = response.text()
                        m = re.search(r'^[^{]*(\{.*\})[^}]*$', txt, re.DOTALL)
                        if m:
                            pdp_json_data = json.loads(m.group(1))
                            pdp_received = True
                except Exception:
                    pass
                    
            page.on("response", handle_response)
            
            try:
                page.goto(p_url, wait_until="domcontentloaded", timeout=25000)
                # mtop 응답 도착 시까지 대기 (최대 3.0초, 도착 시 즉시 진행)
                for _ in range(30):
                    if pdp_received:
                        break
                    time.sleep(0.1)
            except Exception:
                time.sleep(0.5)
                
            page.remove_listener("response", handle_response)
            
            # 파싱
            result = pdp_json_data.get("data", {}).get("result", {})
            shop_card = result.get("SHOP_CARD_PC", {})
            product_props = result.get("PRODUCT_PROP_PC", {})
            global_data = result.get("GLOBAL_DATA", {}).get("globalData", {})
            
            store_name = shop_card.get("storeName") or ""
            store_rating = ""
            for b in shop_card.get("benefitInfoList", []):
                if "좋아요" in b.get("title", "") or "positive" in b.get("title", "").lower():
                    store_rating = b.get("value", "")
                    
            vendor_id = str(global_data.get("sellerId") or "")
            if not vendor_id and shop_card.get("licenseInfo"):
                lm = re.search(r'storeNum=(\d+)', shop_card["licenseInfo"].get("actionTarget", ""))
                if lm:
                    vendor_id = lm.group(1)
            if not vendor_id:
                vendor_id = p_id
                
            # 이미 수집한 판매자라면 즉시 다음 상품으로 스킵 (고유 판매자 전량 수집)
            if vendor_id in seen_vendor_ids:
                if idx % 20 == 0 or idx == total_prods:
                    print(f"  [{idx}/{total_prods}] 진행 중... (현재 고유 판매자: {len(collected_records)}명 확보)", flush=True)
                continue
                
            company_name = ""
            ceo_name = ""
            business_number = ""
            phone = ""
            email = ""
            address = ""
            ecommerce_report_number = ""
            
            # showedProps 에서 7대 항목 파싱
            showed_props = product_props.get("showedProps", [])
            for prop in showed_props:
                n = prop.get("attrName", "").strip()
                v = prop.get("attrValue", "").strip()
                if n in ("회사 이름", "회사명", "상호명", "상호", "제조업체", "제조자", "생산자"):
                    if v and not company_name and "참조" not in v: company_name = v
                elif n in ("대표자", "대표자명", "대표", "성명"): ceo_name = v
                elif n in ("사업자번호", "사업자등록번호"): business_number = v
                elif n in ("소비자상담전화번호", "전화번호", "연락처", "고객센터"):
                    if "참조" not in v: phone = v
                elif n in ("이메일 주소", "이메일", "E-mail", "Email"): email = v
                elif n in ("사업장소재지", "주소", "소재지"): address = v
                elif n in ("통신판매업신고번호", "통신판매업신고"): ecommerce_report_number = v
                elif n == "원산지" and not address: address = v
                
            # showedPropsMap 폴백
            props_map = product_props.get("showedPropsMap", {})
            for k, v in props_map.items():
                n = v.get("attrName", "").strip()
                val = v.get("attrValue", "").strip()
                if n == "이메일 주소" and not email: email = val
                if n == "대표자" and not ceo_name: ceo_name = val
                if n == "사업자번호" and not business_number: business_number = val
                if n in ("회사 이름", "상호명") and not company_name and "참조" not in val: company_name = val
                if n in ("소비자상담전화번호", "전화번호") and not phone and "참조" not in val: phone = val
                if n in ("사업장소재지", "주소") and not address: address = val
                if n == "통신판매업신고번호" and not ecommerce_report_number: ecommerce_report_number = val
                
            # 전화번호에서 불필요한 레이블 정리
            if phone:
                phone = phone.replace("소비자상담전화번호", "").replace("고객센터", "").strip()
                
            # 상호명 폴백 (스토어명 사용)
            if not company_name or "참조" in company_name or company_name == "0":
                company_name = store_name or "(상세 미기재)"

            rec = {
                "vendor_id": vendor_id,
                "url": p_url,
                "store_name": store_name,
                "company_name": company_name,
                "ceo_name": ceo_name,
                "business_number": business_number,
                "phone": phone,
                "email": email,
                "address": address,
                "ecommerce_report_number": ecommerce_report_number,
                "power_seller": "TRUE" if itm["is_top_seller"] else "FALSE",
                "power_seller_title": "AliExpress TOP셀러" if itm["is_top_seller"] else "",
                "rating_count": itm.get("orders", ""),
                "thumb_up_ratio": store_rating,
                "product_title": p_title,
                "price": itm.get("price", ""),
                "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
            seen_vendor_ids.add(vendor_id)
            collected_records.append(rec)
            
            # 실시간 1건씩 CSV에 추가 기록 (데이터 보존)
            with open(csv_file, "a", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=COUPANG_DATASET_FIELDS)
                writer.writerow(rec)
                
            # JSON 갱신
            with open(json_file, "w", encoding="utf-8") as f:
                json.dump(collected_records, f, ensure_ascii=False, indent=2)
                
            print(f"  [{len(collected_records)}번째 고유 판매자 확보] ({idx}/{total_prods}) 상호: {rec['company_name']} | 대표: {rec['ceo_name'] or '(미기재)'} | 이메일: {rec['email'] or '(미기재)'} | 사업자번호: {rec['business_number'] or '(미기재)'}", flush=True)
            
            time.sleep(0.8)
            
        print("\n" + "=" * 80, flush=True)
        print("전체 수집 완료 보고:")
        print(f"- 카테고리: {TARGET_CATEGORY}")
        print(f"- 총 탐색 상품 수: {len(all_unique_products)}개")
        print(f"- 최종 확보 고유 판매자 수: {len(collected_records)}명")
        
        # 데이터 품질 지표 통계
        has_email = sum(1 for r in collected_records if r["email"])
        has_ceo = sum(1 for r in collected_records if r["ceo_name"])
        has_biz_num = sum(1 for r in collected_records if r["business_number"])
        has_phone = sum(1 for r in collected_records if r["phone"])
        
        print(f"- 이메일 확보율: {has_email}/{len(collected_records)} ({has_email/len(collected_records)*100:.1f}%)" if collected_records else "- 수집 데이터 없음")
        print(f"- 대표자명 확보율: {has_ceo}/{len(collected_records)} ({has_ceo/len(collected_records)*100:.1f}%)" if collected_records else "")
        print(f"- 사업자등록번호 확보율: {has_biz_num}/{len(collected_records)} ({has_biz_num/len(collected_records)*100:.1f}%)" if collected_records else "")
        print(f"- 전화번호 확보율: {has_phone}/{len(collected_records)} ({has_phone/len(collected_records)*100:.1f}%)" if collected_records else "")
        print(f"- CSV 저장 경로: {csv_file}")
        print(f"- JSON 저장 경로: {json_file}")
        print("=" * 80, flush=True)
        
        browser.close()
        return csv_file, json_file, collected_records

if __name__ == "__main__":
    run_full_category_crawl(max_pages=30)
