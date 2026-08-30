"""
Foodspring (식봄) 전국 택배 배송 기획전 상품·판매자 정보 크롤러
================================================================
대상      : https://www.foodspring.co.kr/special/wcpd
봇차단    : AWS WAF (CloudFront) - /seller/* 경로만 challenge 차단
            /goods/detail/*, /special/* 는 차단 없음
수집 방법 :
  1. Chromium 세션에서 쿠키 획득 (게스트 세션 필수)
  2. GraphQL API(api.foodspring.co.kr/v2/graphql) cursor 페이지네이션으로
     전체 상품(10,000개) 수집 - 중복 제거
  3. 고유 셀러별 GraphQL node(id) 조회로 판매자 사업자정보 추출
     (셀러명/대표자명/사업자등록번호/연락처/이메일/소재지/통신판매신고번호/고객센터전화)
  4. 엑셀 출력 (상품목록 시트 + 셀러정보 시트)
"""
import argparse
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
STATE_DIR = os.path.join(BASE_DIR, "state")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
GRAPHQL_URL = "https://api.foodspring.co.kr/v2/graphql"
LIST_URL = "https://www.foodspring.co.kr/special/wcpd"
DETAIL_URL = "https://www.foodspring.co.kr/goods/detail/{pid}"

# 셀러 사업자정보(이메일/대표자 포함) 단건 조회 쿼리
VENDOR_QUERY = """query VendorBusinessInfo($id: ID!) {
  node(id: $id) {
    __typename
    ... on Vendor {
      nid
      name
      ownerName
      businessRegistrationNumber
      mailOrderRegistrationNumber
      businessAddress
      contact
      email
      customerServiceNumber
      mainGoodsDescription
    }
    id
  }
}"""

# 셀러 시트 컬럼 (쿠팡 수집 항목에 맞춰 매핑)
SELLER_COLS = [
    ("seller_id", "셀러ID"),
    ("store_name", "셀러명(스토어)"),
    ("owner_name", "대표자명"),
    ("business_number", "사업자등록번호"),
    ("phone", "연락처"),
    ("email", "이메일"),
    ("address", "사업장 소재지"),
    ("ecommerce_report_number", "통신판매 신고번호"),
    ("customer_service_number", "고객센터 전화"),
    ("product_count", "판매 상품수"),
    ("representative_pid", "대표 상품ID"),
]

# 상품 시트 컬럼
PRODUCT_COLS = [
    ("url", "상품 URL"),
    ("name", "상품명"),
    ("sale_price", "판매가(원)"),
    ("discount_rate", "할인율(%)"),
    ("image_url", "이미지 URL"),
    ("seller_id", "셀러ID"),
    ("seller_name", "셀러명(스토어)"),
    ("owner_name", "대표자명"),
    ("business_number", "사업자등록번호"),
    ("phone", "연락처"),
    ("email", "이메일"),
    ("address", "사업장 소재지"),
    ("ecommerce_report_number", "통신판매 신고번호"),
    ("customer_service_number", "고객센터 전화"),
]


def load_graphql_query():
    """SimpleGoodsListPageNationQuery (전체 필드는 브라우저 캡처본에서 추출)"""
    q_path = os.path.join(BASE_DIR, "goods_list_query.graphql")
    if os.path.exists(q_path):
        return open(q_path, encoding="utf-8").read()
    return QUERY_FALLBACK


QUERY_FALLBACK = ""


def detect_chromium():
    """설치된 playwright chromium 실행파일 자동 탐지."""
    base = os.path.expanduser("~/.cache/ms-playwright")
    if os.path.isdir(base):
        for d in sorted(os.listdir(base), reverse=True):
            if not d.startswith("chromium-") and not d.startswith("chromium_headless"):
                continue
            for root, _, files in os.walk(os.path.join(base, d)):
                if "chrome" in files:
                    cand = os.path.join(root, "chrome")
                    if os.access(cand, os.X_OK):
                        return cand
    return None


def get_session_cookies(chromium_path=None):
    """Chromium으로 /special/wcpd 방문 후 게스트 세션 쿠키 획득."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            executable_path=chromium_path or detect_chromium(),
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(user_agent=UA, locale="ko-KR")
        page = ctx.new_page()
        page.goto(LIST_URL, wait_until="networkidle", timeout=60000)
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies())
        browser.close()
    return cookie_str


def graphql_goods_list(cookie_str, after, query, first=80):
    body = json.dumps({
        "query": query,
        "variables": {
            "after": after,
            "areaId": None,
            "first": first,
            "input": {
                "categoryId": None,
                "delivery": "PARCEL",
                "sort": "POPULAR_DESC",
                "terms": "",
            },
        },
    })
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=body.encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": UA,
            "Origin": "https://www.foodspring.co.kr",
            "Referer": LIST_URL,
            "Cookie": cookie_str,
        },
    )
    resp = urllib.request.urlopen(req, timeout=40)
    data = json.loads(resp.read().decode("utf-8"))
    if "errors" in data and not data.get("data"):
        raise RuntimeError(f"GraphQL errors: {json.dumps(data['errors'], ensure_ascii=False)[:400]}")
    gl = (data.get("data") or {}).get("goodsList") or {}
    return gl.get("edges", []), gl.get("pageInfo", {}) or {}


def crawl_all_products(cookie_str, query, max_pages=200, delay=(0.6, 1.2)):
    """전체 상품 수집. 중복 제거보장: cursor는 0~9999까지 유니크, 이후 반복."""
    products = {}
    after = None
    page_no = 0
    while page_no < max_pages:
        page_no += 1
        try:
            edges, pi = graphql_goods_list(cookie_str, after, query)
        except Exception as e:  # noqa: BLE001
            print(f"  [page {page_no}] 오류: {e} (3초 후 재시도)")
            time.sleep(3)
            if page_no >= max_pages:
                break
            continue
        if not edges:
            print(f"  [page {page_no}] 빈 응답 - 종료")
            break
        new = 0
        for e in edges:
            node = e["node"]
            nid = node.get("nid")
            if nid and nid not in products:
                products[nid] = node
                new += 1
        print(f"  [page {page_no}] +{new} (누적 {len(products)}) "
              f"hasNext={pi.get('hasNextPage')}")
        after = pi.get("endCursor")
        if not pi.get("hasNextPage"):
            break
        time.sleep(random.uniform(*delay))
    return products


def fetch_goods_detail_html(pid, cookie_str):
    req = urllib.request.Request(
        DETAIL_URL.format(pid=pid),
        headers={"User-Agent": UA, "Cookie": cookie_str},
    )
    resp = urllib.request.urlopen(req, timeout=40)
    html = resp.read().decode("utf-8", "replace")
    return html


def fetch_vendor_graphql(seller_nid, cookie_str):
    """GraphQL node(id) 조회 — 이메일/대표자명 포함 셀러 사업자정보."""
    body = json.dumps({
        "query": VENDOR_QUERY,
        "variables": {"id": f"vendor_{seller_nid}"},
    })
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=body.encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": UA,
            "Origin": "https://www.foodspring.co.kr",
            "Referer": f"https://www.foodspring.co.kr/seller/{seller_nid}",
            "Cookie": cookie_str,
        },
    )
    resp = urllib.request.urlopen(req, timeout=40)
    data = json.loads(resp.read().decode("utf-8"))
    node = (data.get("data") or {}).get("node") or {}
    if not node or node.get("__typename") != "Vendor":
        return None
    return {
        "seller_id": node.get("nid") or seller_nid,
        "store_name": (node.get("name") or "").strip(),
        "owner_name": (node.get("ownerName") or "").strip(),
        "business_number": (node.get("businessRegistrationNumber") or "").strip(),
        "phone": (node.get("contact") or "").strip(),
        "email": (node.get("email") or "").strip(),
        "address": (node.get("businessAddress") or "").strip(),
        "ecommerce_report_number": (
            node.get("mailOrderRegistrationNumber") or ""
        ).strip(),
        "customer_service_number": (
            node.get("customerServiceNumber") or ""
        ).strip(),
        "refund_delivery_fee": "",
        "exchange_delivery_fee": "",
    }


def extract_vendor_info(html, seller_nid):
    """상품 상세 __NEXT_DATA__ 에서 vendor_{nid} 객체 추출 (이메일/대표자 없음)."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    cache = data.get("props", {}).get("pageProps", {}).get("initialRecords", {})
    vkey = f"vendor_{seller_nid}"
    vendor = cache.get(vkey)
    if not vendor:
        # 동일 nid를 가진 다른 vendor 키 탐색
        for k, v in cache.items():
            if k.startswith("vendor_") and v.get("nid") == seller_nid:
                vendor = v
                break
    if not vendor:
        return None
    return {
        "seller_id": vendor.get("nid") or seller_nid,
        "store_name": vendor.get("name") or "",
        "owner_name": "",
        "business_number": (vendor.get("businessRegistrationNumber") or "").strip(),
        "phone": (vendor.get("contact") or "").strip(),
        "email": "",
        "address": (vendor.get("businessAddress") or "").strip(),
        "ecommerce_report_number": (vendor.get("mailOrderRegistrationNumber") or "").strip(),
        "customer_service_number": (vendor.get("customerServiceNumber") or "").strip(),
        "refund_delivery_fee": (vendor.get("refundDeliveryFee") or "").strip(),
        "exchange_delivery_fee": (vendor.get("exchangeDeliveryFee") or "").strip(),
    }


def collect_seller_infos(products, cookie_str, seller_product_map,
                         delay=(1.0, 2.0), state_path=None, workers=3):
    """고유 셀러별 대표 상품 1개에서 사업자정보 수집 (resume+병렬 가능)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    infos = {}
    if state_path and os.path.exists(state_path):
        infos = json.load(open(state_path, encoding="utf-8"))
    total = len(seller_product_map)
    print(f"\n[셀러 정보 수집] 총 {total}개 셀러 (이미 수집: {len(infos)})")

    # 미수집 셀러 목록
    pending = [(sid, pid) for sid, pid in sorted(seller_product_map.items())
               if str(sid) not in {str(k) for k in infos}]

    def fetch_one(sid, pid):
        # 1) GraphQL node 조회 (이메일/대표자 포함, 경량)
        try:
            info = fetch_vendor_graphql(sid, cookie_str)
        except Exception:  # noqa: BLE001
            info = None
        # 2) 실패 시 상세 페이지 HTML 폴백
        if not info:
            try:
                html = fetch_goods_detail_html(pid, cookie_str)
                info = extract_vendor_info(html, sid)
            except Exception:  # noqa: BLE001
                info = None
        if not info:
            alt_pids = [p for p, s in seller_product_map.items() if str(s) == str(sid)][1:4]
            for ap in alt_pids:
                try:
                    html = fetch_goods_detail_html(ap, cookie_str)
                    info = extract_vendor_info(html, sid)
                except Exception:  # noqa: BLE001
                    continue
                if info:
                    break
        return info

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_one, sid, pid): (sid, pid) for sid, pid in pending}
        for fut in as_completed(futs):
            sid, pid = futs[fut]
            done += 1
            try:
                info = fut.result()
            except Exception:  # noqa: BLE001
                info = None
            if info:
                infos[sid] = info
                print(f"  [{done}/{total}] 셀러 {sid} ({info.get('store_name')}) "
                      f"사업자등록번호={info.get('business_number') or '∅'} "
                      f"이메일={info.get('email') or '∅'}")
            else:
                infos[str(sid)] = {
                    "seller_id": sid,
                    "store_name": "",
                    "owner_name": "",
                    "business_number": "",
                    "phone": "",
                    "email": "",
                    "address": "",
                    "ecommerce_report_number": "",
                    "customer_service_number": "",
                }
                print(f"  [{done}/{total}] 셀러 {sid}: 정보 없음 (대표상품 {pid})")
            if state_path and done % 25 == 0:
                json.dump(infos, open(state_path, "w", encoding="utf-8"),
                          ensure_ascii=False, indent=1)
            time.sleep(random.uniform(*delay))
    if state_path:
        json.dump(infos, open(state_path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    return infos


def to_excel(products, seller_infos, out_path):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="2F5496")

    # --- 셀러 시트 ---
    ws_seller = wb.active
    ws_seller.title = "셀러정보"
    for c, (_, label) in enumerate(SELLER_COLS, 1):
        cell = ws_seller.cell(row=1, column=c, value=label)
        cell.font = header_font
        cell.fill = header_fill
    for r, (sid, info) in enumerate(sorted(seller_infos.items()), 2):
        info = info or {}
        row = [
            info.get("seller_id") or sid,
            info.get("store_name", ""),
            info.get("owner_name", ""),
            info.get("business_number", ""),
            info.get("phone", ""),
            info.get("email", ""),
            info.get("address", ""),
            info.get("ecommerce_report_number", ""),
            info.get("customer_service_number", ""),
            info.get("_product_count", ""),
            info.get("_representative_pid", ""),
        ]
        for c, val in enumerate(row, 1):
            ws_seller.cell(row=r, column=c, value=val)
    for c in range(1, len(SELLER_COLS) + 1):
        ws_seller.column_dimensions[get_column_letter(c)].width = 22

    # --- 상품 시트 ---
    ws_prod = wb.create_sheet("상품목록")
    for c, (_, label) in enumerate(PRODUCT_COLS, 1):
        cell = ws_prod.cell(row=1, column=c, value=label)
        cell.font = header_font
        cell.fill = header_fill

    r = 2
    for nid in sorted(products.keys(), key=lambda x: int(x)):
        node = products[nid]
        sid = (node.get("vendor") or {}).get("nid")
        info = seller_infos.get(str(sid)) or {}
        price = node.get("price") or {}
        img = (node.get("images") or {}).get("primaryUrl") or ""
        row = [
            f"https://www.foodspring.co.kr/goods/detail/{nid}",
            node.get("name", ""),
            price.get("salePrice"),
            price.get("discountRate"),
            img,
            sid,
            (node.get("vendor") or {}).get("name", ""),
            info.get("owner_name", "") if info else "",
            info.get("business_number", "") if info else "",
            info.get("phone", "") if info else "",
            info.get("email", "") if info else "",
            info.get("address", "") if info else "",
            info.get("ecommerce_report_number", "") if info else "",
            info.get("customer_service_number", "") if info else "",
        ]
        for c, val in enumerate(row, 1):
            ws_prod.cell(row=r, column=c, value=val)
        r += 1
    for c in range(1, len(PRODUCT_COLS) + 1):
        width = 60 if c == 2 else (45 if c in (1, 5) else 14)
        ws_prod.column_dimensions[get_column_letter(c)].width = width
    ws_prod.freeze_panes = "A2"
    ws_seller.freeze_panes = "A2"

    wb.save(out_path)
    return r - 2, len(seller_infos)


def main():
    ap = argparse.ArgumentParser(description="Foodspring 상품/셀러 크롤러")
    ap.add_argument("--max-pages", type=int, default=200)
    ap.add_argument("--chromium", default=None,
                    help="Chromium 실행파일 경로 (기본: 자동 감지)")
    ap.add_argument("--skip-session", action="store_true", help="기존 쿠키 재사용")
    ap.add_argument("--skip-sellers", action="store_true",
                    help="셀러 정보 수집 생략(상품만)")
    ap.add_argument("--workers", type=int, default=3, help="셀러 수집 동시 작업수")
    args = ap.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = f"foodspring_wcpd_{ts}"
    products_path = os.path.join(STATE_DIR, f"{prefix}_products.json")
    sellers_state = os.path.join(STATE_DIR, f"{prefix}_sellers.json")
    cookie_path = os.path.join(STATE_DIR, f"{prefix}_cookie.txt")

    print("=" * 60)
    print("Foodspring(식봄) /special/wcpd 상품·셀러 정보 크롤러")
    print(f"시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    query = load_graphql_query()
    if not QUERY_FALLBACK and not query:
        print("ERROR: goods_list_query.graphql 필요")
        sys.exit(1)

    # 1. 세션
    if args.skip_session and os.path.exists(cookie_path):
        cookie_str = open(cookie_path).read().strip()
        print("\n[1] 기존 세션 쿠키 재사용")
    else:
        print("\n[1] Chromium 세션 쿠키 획득 중...")
        cookie_str = get_session_cookies(args.chromium)
        open(cookie_path, "w").write(cookie_str)
        print("    쿠키 저장 완료")

    # 2. 상품 전체 수집
    print("\n[2] GraphQL 전체 상품 수집 중...")
    products = crawl_all_products(cookie_str, query, max_pages=args.max_pages)
    print(f"    총 상품 수: {len(products)}")
    if not products:
        print("ERROR: 상품 수집 실패")
        sys.exit(1)
    json.dump({k: v for k, v in products.items()},
              open(products_path, "w", encoding="utf-8"),
              ensure_ascii=False)

    # 셀러 매핑: 최초 등장 상품을 대표 상품으로
    seller_product_map = {}
    for nid in sorted(products.keys(), key=lambda x: int(x)):
        node = products[nid]
        sid = (node.get("vendor") or {}).get("nid")
        if sid is None:
            continue
        sid = str(sid)
        if sid not in seller_product_map:
            seller_product_map[sid] = nid
    print(f"    고유 셀러 수: {len(seller_product_map)}")

    # 3. 셀러 정보 수집
    seller_infos = {}
    if not args.skip_sellers:
        print("\n[3] 셀러 사업자정보 수집 중...")
        seller_infos = collect_seller_infos(products, cookie_str,
                                            seller_product_map,
                                            state_path=sellers_state,
                                            workers=args.workers)
        print(f"    정보 수집 완료: {len(seller_infos)} 셀러")

        # 상품수 집계
        counts = {}
        for node in products.values():
            sid = (node.get("vendor") or {}).get("nid")
            if sid is not None:
                counts[sid] = counts.get(sid, 0) + 1
        for sid, info in seller_infos.items():
            info["_product_count"] = counts.get(int(sid) if str(sid).isdigit() else sid, 0)
            info["_representative_pid"] = seller_product_map.get(str(sid), "")
    else:
        products_path = products_path.replace("_products", "_products")

    # 4. 엑셀 출력
    print("\n[4] 엑셀 출력 중...")
    xlsx = os.path.join(OUTPUT_DIR, f"{prefix}.xlsx")
    n_prod, n_seller = to_excel(products, seller_infos, xlsx)
    print(f"    상품 {n_prod}개 / 셀러 {n_seller}개 → {xlsx}")

    print("\n" + "=" * 60)
    print("요약")
    print(f"  상품(중복제거): {len(products)}")
    print(f"  셀러: {len(seller_infos)}")
    print(f"  엑셀: {xlsx}")
    if seller_infos:
        with_info = sum(1 for v in seller_infos.values()
                        if v and v.get("business_number"))
        print(f"  사업자등록번호 확보 셀러: {with_info}/{len(seller_infos)}")
    print("=" * 60)


if __name__ == "__main__":
    main()