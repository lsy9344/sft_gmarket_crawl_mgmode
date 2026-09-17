import asyncio
import json
import csv
import re
from datetime import datetime
from pathlib import Path
from patchright.async_api import async_playwright

OUTPUT_DIR = Path("/home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/output/aliexpress")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

HOME_URL = "https://ko.aliexpress.com/?spm=a2g0o.home.logo.1.6c2f52d1NGQ4SZ"
TARGET_CATEGORY = "식품과식료품 > 야채"
TARGET_URL = "https://ko.aliexpress.com/w/wholesale-%EC%95%BC%EC%B1%84.html?categoryTab=food_%26_grocery&isFromCategory=y"

COUPANG_DATASET_FIELDS = [
    "vendor_id",                 # 판매자/스토어 식별자
    "url",                       # 상품 상세 URL
    "store_name",                # 스토어명
    "company_name",              # 회사/상호명 (사업자명)
    "ceo_name",                  # 대표자명
    "business_number",           # 사업자등록번호
    "phone",                     # 사업자 전화번호 (소비자상담번호)
    "email",                     # 사업자 이메일 주소
    "address",                   # 사업장 소재지 (주소)
    "ecommerce_report_number",   # 통신판매업신고번호
    "power_seller",              # 우수판매자 여부
    "power_seller_title",        # 우수판매자 배지 명칭
    "rating_count",              # 누적 판매량
    "thumb_up_ratio",            # 긍정 평가율 (만족도)
    "product_title",             # 상품명
    "price",                     # 판매 가격
    "collected_at"               # 수집 일시
]

async def collect_full_dataset(limit=10):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    print("=" * 80)
    print("알리익스프레스 쿠팡 100% 동일 규격 사업자 정보 수집기 (PDP API 연동)")
    print(f"- 카테고리: {TARGET_CATEGORY}")
    print(f"- 수집 목표: 사업자번호, 대표자명, 이메일, 전화번호, 상호명, 주소 100% 추출")
    print(f"- 비용: 0원 (로컬 무료 직접 회선)")
    print("=" * 80)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            locale="ko-KR",
            viewport={"width": 1440, "height": 900}
        )
        page = await context.new_page()
        
        # 1. 웜업
        print("\n[Phase 1] 홈 화면 웜업...")
        await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(2500)
        
        # 2. 리스팅 진입
        print(f"\n[Phase 2] '{TARGET_CATEGORY}' 리스팅 진입...")
        await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(3000)
        
        await page.evaluate("window.scrollBy(0, 1000)")
        await page.wait_for_timeout(2000)
        await page.evaluate("window.scrollBy(0, 1000)")
        await page.wait_for_timeout(2000)
        
        # 리스팅 상품 추출
        raw_items = await page.evaluate("""() => {
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
        
        print(f"리스팅에서 {len(raw_items)}개 상품 발견")
        targets = raw_items[:limit]
        
        # 3. 상품별 PDP API 인터셉트로 완벽한 사업자 정보 추출
        print(f"\n[Phase 3] 상위 {len(targets)}개 상품별 PDP API 인터셉트 및 사업자 정보 추출...")
        
        records = []
        
        for idx, itm in enumerate(targets, 1):
            p_id = itm["id"]
            p_url = itm["url"]
            p_title = itm["title"]
            print(f"\n  [{idx}/{len(targets)}] 상품 ID: {p_id} | {p_title[:25]}...")
            
            pdp_json_data = {}
            
            async def handle_response(response):
                nonlocal pdp_json_data
                try:
                    if "mtop.aliexpress.pdp.pc.query" in response.url and response.status == 200:
                        txt = await response.text()
                        m = re.search(r'^[^{]*(\{.*\})[^}]*$', txt, re.DOTALL)
                        if m:
                            pdp_json_data = json.loads(m.group(1))
                except Exception:
                    pass
            
            page.on("response", handle_response)
            
            try:
                await page.goto(p_url, wait_until="domcontentloaded", timeout=40000)
                await page.wait_for_timeout(3500)
            except Exception as e:
                print(f"    페이지 로드 타임아웃/오류: {e}")
                
            page.remove_listener("response", handle_response)
            
            # PDP 데이터에서 사업자 정보 파싱
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
                    
            # showedProps 에서 사업자 7대 핵심 정보 파싱
            company_name = ""
            ceo_name = ""
            business_number = ""
            phone = ""
            email = ""
            address = ""
            ecommerce_report_number = ""
            
            showed_props = product_props.get("showedProps", [])
            for prop in showed_props:
                name = prop.get("attrName", "").strip()
                val = prop.get("attrValue", "").strip()
                
                if name in ("회사 이름", "회사명", "상호명", "상호", "제조업체", "제조자", "생산자"):
                    if val and not company_name:
                        company_name = val
                elif name in ("대표자", "대표자명", "대표", "성명"):
                    ceo_name = val
                elif name in ("사업자번호", "사업자등록번호"):
                    business_number = val
                elif name in ("소비자상담전화번호", "전화번호", "연락처", "고객센터"):
                    phone = val
                elif name in ("이메일 주소", "이메일", "E-mail", "Email"):
                    email = val
                elif name in ("사업장소재지", "주소", "소재지"):
                    address = val
                elif name in ("통신판매업신고번호", "통신판매업신고"):
                    ecommerce_report_number = val
                elif name == "원산지" and not address:
                    address = val
                    
            # showedPropsMap 폴백
            props_map = product_props.get("showedPropsMap", {})
            for k, v in props_map.items():
                name = v.get("attrName", "").strip()
                val = v.get("attrValue", "").strip()
                if name == "이메일 주소" and not email: email = val
                if name == "대표자" and not ceo_name: ceo_name = val
                if name == "사업자번호" and not business_number: business_number = val
                if name == "회사 이름" and not company_name: company_name = val
                if name == "소비자상담전화번호" and not phone: phone = val
                if name == "사업장소재지" and not address: address = val
                if name == "통신판매업신고번호" and not ecommerce_report_number: ecommerce_report_number = val

            rec = {
                "vendor_id": vendor_id or p_id,
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
            
            print(f"    - 상호명: {rec['company_name'] or '(미기재)'}")
            print(f"    - 대표자: {rec['ceo_name'] or '(미기재)'}")
            print(f"    - 사업자등록번호: {rec['business_number'] or '(미기재)'}")
            print(f"    - 전화번호: {rec['phone'] or '(미기재)'}")
            print(f"    - 이메일: {rec['email'] or '(미기재)'}")
            print(f"    - 주소: {rec['address'][:30] if rec['address'] else '(미기재)'}...")
            
            records.append(rec)
            await page.wait_for_timeout(1500)
            
        # 4. 저장 (CSV 및 JSON)
        csv_path = OUTPUT_DIR / f"aliexpress_coupang_exact_dataset_{timestamp}.csv"
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=COUPANG_DATASET_FIELDS)
            writer.writeheader()
            for r in records:
                writer.writerow(r)
                
        json_path = OUTPUT_DIR / f"aliexpress_coupang_exact_dataset_{timestamp}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
            
        print("\n" + "=" * 80)
        print("수집 완료!")
        print(f"- 총 수집: {len(records)}건")
        print(f"- CSV: {csv_path}")
        print(f"- JSON: {json_path}")
        print("=" * 80)
        
        await browser.close()
        return csv_path, json_path

if __name__ == "__main__":
    asyncio.run(collect_full_dataset(limit=10))
