"""알리익스프레스 카테고리 전량 수집기 — Gmarket/Coupang 검증 2단계 파이프라인.

핵심 탑재 기술 (Coupang & Gmarket 방법론 적용):
1. Phase A: 카테고리(식품과식료품 > 야채) 1~30페이지 전량 리스팅 순회 및 캐시 보존
2. 스마트 판매자 중복 제거: 기확보된 판매자 정보 자동 매핑으로 중복 조회 최소화
3. 자동 회선 순환 (Session Rotation): 25건 단위 정기 회선 교체 + WAF punish/차단 감지 시 즉시 새 회선 세션으로 자가 교체 및 재시도
4. 실시간 체크포인트 내구성: 매 건 수집 즉시 CSV/JSON 동시 영속화 (중간 유실 0건)
5. 산출물: 쿠팡 카테고리 17개 필드 100% 동일 규격
"""

import asyncio
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# 프로젝트 루트 경로 추가
PROJECT_ROOT = Path("/home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from patchright.async_api import async_playwright
from app.core import decodo

# ── 설정 및 경로 ──────────────────────────────────────────────────────────
OUTPUT_DIR = PROJECT_ROOT / "output" / "aliexpress"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

HOME_WARMUP_URL = "https://ko.aliexpress.com/?spm=a2g0o.home.logo.1.6c2f52d1NGQ4SZ"
TARGET_CATEGORY = "식품과식료품 > 야채"
BASE_LISTING_URL = "https://ko.aliexpress.com/w/wholesale-%EC%95%BC%EC%B1%84.html?categoryTab=food_%26_grocery&isFromCategory=y"

COUPANG_DATASET_FIELDS = [
    "vendor_id",                 # 판매자/스토어 식별자
    "url",                       # 상품 상세 URL
    "store_name",                # 스토어명
    "company_name",              # 회사/상호명 (사업자명)
    "ceo_name",                  # 대표자명
    "business_number",           # 사업자등록번호
    "phone",                     # 사업자 전화번호 (고객센터/소비자상담)
    "email",                     # 사업자 이메일 주소
    "address",                   # 사업장 소재지 (주소)
    "ecommerce_report_number",   # 통신판매업신고번호
    "power_seller",              # 우수판매자 여부 (TRUE/FALSE)
    "power_seller_title",        # 우수판매자 배지 명칭
    "rating_count",              # 누적 판매량
    "thumb_up_ratio",            # 긍정 평가율 (만족도)
    "product_title",             # 상품명
    "price",                     # 판매 가격
    "collected_at"               # 수집 일시
]

ROTATION_BATCH_SIZE = 25  # 25건마다 회선 세션 자동 교체
MAX_ITEM_RETRIES = 3      # 차단 시 세션 교체 후 최대 재시도 횟수


class SessionManager:
    """Decodo 스티키 주거용 고정 회선 세션 수명 주기 관리자."""

    def __init__(self, p_instance):
        self.p = p_instance
        self.browser = None
        self.context = None
        self.page = None
        self.session_counter = 0
        self.current_sid = ""

    async def open_new_session(self):
        """기존 브라우저를 닫고 완전히 새로운 IP 세션으로 기동 및 홈 웜업 수행."""
        await self.close()
        self.session_counter += 1
        self.current_sid = f"ali_rot_{int(time.time()) % 100000}_{self.session_counter}"
        proxy = decodo.sticky_proxy_dict(session_id=self.current_sid)

        launch_args = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ]
        }
        if proxy:
            launch_args["proxy"] = proxy

        self.browser = await self.p.chromium.launch(**launch_args)
        self.context = await self.browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            locale="ko-KR",
            viewport={"width": 1440, "height": 900}
        )

        # 한국 지역 쿠키 주입
        await self.context.add_cookies([
            {"name": "aep_usuc_f", "value": "region=KR&b_locale=ko_KR&site=kor&c_tp=KRW", "domain": ".aliexpress.com", "path": "/"},
            {"name": "xman_us_f", "value": "x_locale=ko_KR&x_l=0", "domain": ".aliexpress.com", "path": "/"}
        ])

        self.page = await self.context.new_page()

        # 홈 웜업 (보안 세션 토큰 확립)
        try:
            await self.page.goto(HOME_WARMUP_URL, wait_until="domcontentloaded", timeout=35000)
            await self.page.wait_for_timeout(2000)
        except Exception:
            pass

    async def close(self):
        if self.page:
            try: await self.page.close()
            except Exception: pass
            self.page = None
        if self.context:
            try: await self.context.close()
            except Exception: pass
            self.context = None
        if self.browser:
            try: await self.browser.close()
            except Exception: pass
            self.browser = None


async def run_full_crawler(max_pages: int = 30):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_file = OUTPUT_DIR / f"aliexpress_vegetables_full_dataset_{timestamp}.csv"
    json_file = OUTPUT_DIR / f"aliexpress_vegetables_full_dataset_{timestamp}.json"
    listing_cache_file = OUTPUT_DIR / "vegetables_listing_cache.json"

    print("=" * 80)
    print("알리익스프레스 [식품과식료품 > 야채] 전량 수집기 (세션 로테이션 & 스마트 중복제거)")
    print(f"- 대상 카테고리: {TARGET_CATEGORY}")
    print(f"- 최대 탐색 페이지: {max_pages} 페이지")
    print(f"- 회선 교체 주기: 매 {ROTATION_BATCH_SIZE}건 조회 시 또는 보안 감지 시 즉시 자동 교체")
    print(f"- 수집 규격: 쿠팡 카테고리 100% 동일 규격 (이메일, 대표자, 사업자번호 등 7대 핵심 정보)")
    print(f"- 출력 CSV: {csv_file}")
    print(f"- 출력 JSON: {json_file}")
    print("=" * 80, flush=True)

    # CSV 헤더 사전 작성
    with open(csv_file, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COUPANG_DATASET_FIELDS)
        writer.writeheader()

    async with async_playwright() as p:
        session_mgr = SessionManager(p)
        print("\n[초기화] 초기 주거용 고정 회선 세션 기동 중...", flush=True)
        await session_mgr.open_new_session()
        print(f"  -> 세션 연결 완료: {session_mgr.current_sid}")

        # ── Phase A: 카테고리 리스팅 전량 수집 ─────────────────────────
        unique_products = {}

        # 캐시가 있고 500개 이상이면 재사용, 없으면 전량 순회
        if listing_cache_file.exists():
            try:
                with open(listing_cache_file, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                    if len(cached) >= 500:
                        unique_products = {p["id"]: p for p in cached}
                        print(f"\n[Phase A] 기존 리스팅 캐시 로드 완료: {len(unique_products)}개 상품 확보")
            except Exception:
                pass

        if not unique_products:
            print(f"\n[Phase A] '{TARGET_CATEGORY}' 1페이지부터 끝까지 전량 리스팅 탐색...", flush=True)
            empty_streak = 0

            for p_num in range(1, max_pages + 1):
                page_url = f"{BASE_LISTING_URL}&page={p_num}"
                print(f"  [페이지 {p_num}/{max_pages}] 목록 로드 중...", flush=True)

                try:
                    await session_mgr.page.goto(page_url, wait_until="domcontentloaded", timeout=35000)
                    await session_mgr.page.wait_for_timeout(2000)
                except Exception as e:
                    print(f"    페이지 로드 타임아웃: {e}", flush=True)
                    continue

                await session_mgr.page.evaluate("window.scrollBy(0, 1500)")
                await session_mgr.page.wait_for_timeout(1000)
                await session_mgr.page.evaluate("window.scrollBy(0, 1500)")
                await session_mgr.page.wait_for_timeout(1000)

                items = await session_mgr.page.evaluate("""() => {
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
                    if itm["id"] not in unique_products:
                        unique_products[itm["id"]] = itm
                        new_in_page += 1

                print(f"    -> 발견: {len(items)}개 (신규: {new_in_page}개, 누적: {len(unique_products)}개)", flush=True)

                if len(items) == 0 or new_in_page == 0:
                    empty_streak += 1
                    if empty_streak >= 2:
                        print(f"    -> 카테고리 전체 목록 끝 도달. (총 {len(unique_products)}개 고유 상품)", flush=True)
                        break
                else:
                    empty_streak = 0

                await session_mgr.page.wait_for_timeout(1000)

            # 리스팅 캐시 저장
            with open(listing_cache_file, "w", encoding="utf-8") as f:
                json.dump(list(unique_products.values()), f, ensure_ascii=False, indent=2)

        total_targets = len(unique_products)
        product_targets = list(unique_products.values())
        print(f"\n[Plan 확정] 총 {total_targets}개 상품 대상 사업자정보 수집 계획 확정.")

        # ── Phase B: 스마트 판매자 수집 및 회선 로테이션 ───────────────
        print(f"\n[Phase B] 판매자 사업자 상세정보(이메일, 대표자, 사업자번호 등) 전량 수집 시작...", flush=True)
        collected_records = []
        vendor_cache = {}  # vendor_id -> 사업자 정보 dict (스마트 중복 제거용)
        req_count_in_session = 0

        # 기존 성공 파일들에서 고유 판매자 시드(Seed) 자동 로드
        seed_files = list(OUTPUT_DIR.glob("aliexpress_vegetables_*.json"))
        loaded_seed_count = 0
        for sf in seed_files:
            try:
                with open(sf, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for r in data:
                        vid = r.get("vendor_id")
                        cname = r.get("company_name")
                        bnum = r.get("business_number")
                        if vid and cname and bnum and cname != "(상세 미기재)" and vid not in vendor_cache:
                            vendor_cache[vid] = {
                                "store_name": r.get("store_name", ""),
                                "company_name": cname,
                                "ceo_name": r.get("ceo_name", ""),
                                "business_number": bnum,
                                "phone": r.get("phone", ""),
                                "email": r.get("email", ""),
                                "address": r.get("address", ""),
                                "ecommerce_report_number": r.get("ecommerce_report_number", ""),
                            }
                            loaded_seed_count += 1
            except Exception:
                pass

        if loaded_seed_count > 0:
            print(f"  [시드 로드] 기존 수집 파일에서 검증된 고유 판매자 {loaded_seed_count}개사 사전 탑재 완료!", flush=True)


        for idx, itm in enumerate(product_targets, 1):
            p_id = itm["id"]
            p_url = itm["url"]
            p_title = itm["title"]

            # 25건 조회 시마다 주기적 세션 로테이션 (WAF 레이트 리밋 예방)
            if req_count_in_session >= ROTATION_BATCH_SIZE:
                print(f"\n  [회선 자동 순환] {req_count_in_session}건 도달 -> 새로운 주거용 회선 세션으로 교체...", flush=True)
                await session_mgr.open_new_session()
                print(f"  -> 새 세션 활성화 완료: {session_mgr.current_sid}", flush=True)
                req_count_in_session = 0

            # 상품 상세 조회 (차단 감지 시 세션 교체 후 재시도)
            record = None
            clean_url = f"https://ko.aliexpress.com/item/{p_id}.html"

            for attempt in range(1, MAX_ITEM_RETRIES + 1):
                pdp_json_data = {}
                pdp_received = False

                async def handle_response(response):
                    nonlocal pdp_json_data, pdp_received
                    if "mtop.aliexpress.pdp.pc.query" in response.url and response.status == 200:
                        try:
                            b = await response.body()
                            txt = b.decode("utf-8", errors="ignore")
                            if "PRODUCT_PROP_PC" in txt or "SHOP_CARD_PC" in txt:
                                m = re.search(r'^[^{]*(\{.*\})[^}]*$', txt, re.DOTALL)
                                if m:
                                    pdp_json_data = json.loads(m.group(1))
                                    pdp_received = True
                        except Exception:
                            pass

                session_mgr.page.on("response", handle_response)

                try:
                    await session_mgr.page.goto(clean_url, wait_until="domcontentloaded", timeout=25000)
                    for _ in range(35):
                        if pdp_received:
                            break
                        await session_mgr.page.wait_for_timeout(100)
                except Exception:
                    await session_mgr.page.wait_for_timeout(500)

                session_mgr.page.remove_listener("response", handle_response)
                req_count_in_session += 1

                # punish/차단 여부 확인
                is_blocked = "punish" in session_mgr.page.url or "_____tmd_____" in session_mgr.page.url

                if is_blocked:
                    print(f"  [{idx}/{total_targets}] WAF 챌린지 감지 -> 새 회선 세션으로 즉시 교체 (재시도 {attempt}/{MAX_ITEM_RETRIES})...", flush=True)
                    await session_mgr.open_new_session()
                    req_count_in_session = 0
                    continue

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

                # 만약 이 판매자가 이미 수집된 판매자라면 캐시에서 즉시 정보 보완
                if vendor_id in vendor_cache:
                    cached_biz = vendor_cache[vendor_id]
                    record = {
                        "vendor_id": vendor_id,
                        "url": p_url,
                        "store_name": store_name or cached_biz["store_name"],
                        "company_name": cached_biz["company_name"],
                        "ceo_name": cached_biz["ceo_name"],
                        "business_number": cached_biz["business_number"],
                        "phone": cached_biz["phone"],
                        "email": cached_biz["email"],
                        "address": cached_biz["address"],
                        "ecommerce_report_number": cached_biz["ecommerce_report_number"],
                        "power_seller": "TRUE" if itm["is_top_seller"] else "FALSE",
                        "power_seller_title": "AliExpress TOP셀러" if itm["is_top_seller"] else "",
                        "rating_count": itm.get("orders", ""),
                        "thumb_up_ratio": store_rating,
                        "product_title": p_title,
                        "price": itm.get("price", ""),
                        "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    }
                    break

                # 7대 핵심 정보 파싱
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
                        if val and not company_name and "참조" not in val:
                            company_name = val
                    elif name in ("대표자", "대표자명", "대표", "성명"):
                        ceo_name = val
                    elif name in ("사업자번호", "사업자등록번호"):
                        business_number = val
                    elif name in ("소비자상담전화번호", "전화번호", "연락처", "고객센터"):
                        if "참조" not in val:
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
                    if name in ("회사 이름", "상호명") and not company_name and "참조" not in val: company_name = val
                    if name in ("소비자상담전화번호", "고객센터") and not phone and "참조" not in val: phone = val
                    if name == "사업장소재지" and not address: address = val
                    if name == "통신판매업신고번호" and not ecommerce_report_number: ecommerce_report_number = val

                if not company_name and not store_name:
                    company_name = "(상세 미기재)"

                # 판매자 캐시 등록
                if company_name and company_name != "(상세 미기재)":
                    vendor_cache[vendor_id] = {
                        "store_name": store_name,
                        "company_name": company_name,
                        "ceo_name": ceo_name,
                        "business_number": business_number,
                        "phone": phone,
                        "email": email,
                        "address": address,
                        "ecommerce_report_number": ecommerce_report_number,
                    }

                record = {
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
                break

            if record:
                collected_records.append(record)

                # 실시간 내구성 저장 (Gmarket 체크포인트 규율)
                with open(csv_file, "a", encoding="utf-8-sig", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=COUPANG_DATASET_FIELDS)
                    writer.writerow(record)

                with open(json_file, "w", encoding="utf-8") as f:
                    json.dump(collected_records, f, ensure_ascii=False, indent=2)

                biz_summary = f"[{record['company_name'] or record['store_name']}] 대표: {record['ceo_name'] or '-'} | 사업자: {record['business_number'] or '-'} | 이메일: {record['email'] or '-'}"
                print(f"  [{idx}/{total_targets}] 수집: {biz_summary} (누적: {len(collected_records)}건, 고유판매자: {len(vendor_cache)}명)", flush=True)

            # 안전 딜레이
            await session_mgr.page.wait_for_timeout(2000)

        await session_mgr.close()

    print("\n" + "=" * 80)
    print("알리익스프레스 전량 수집 최종 완료!")
    print(f"- 수집 총 건수: {len(collected_records)} 건")
    print(f"- 고유 판매자 수: {len(vendor_cache)} 명")
    has_email = sum(1 for r in collected_records if r.get("email"))
    has_biz = sum(1 for r in collected_records if r.get("business_number"))
    has_ceo = sum(1 for r in collected_records if r.get("ceo_name"))
    print(f"- 이메일 확보: {has_email} 건 ({has_email / max(1, len(collected_records)) * 100:.1f}%)")
    print(f"- 사업자번호 확보: {has_biz} 건 ({has_biz / max(1, len(collected_records)) * 100:.1f}%)")
    print(f"- 대표자명 확보: {has_ceo} 건 ({has_ceo / max(1, len(collected_records)) * 100:.1f}%)")
    print(f"- 최종 CSV: {csv_file}")
    print(f"- 최종 JSON: {json_file}")
    print("=" * 80, flush=True)


if __name__ == "__main__":
    max_p = 30
    if len(sys.argv) > 1:
        try:
            max_p = int(sys.argv[1])
        except ValueError:
            pass
    asyncio.run(run_full_crawler(max_pages=max_p))
