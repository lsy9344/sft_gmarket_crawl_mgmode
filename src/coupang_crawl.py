"""
Coupang 판매자 사업자정보 수집기 - 4단계 파이프라인
Stage A: OMP/카테고리 리스팅 스캔 → 상품 목록 추출
Stage B: 판매자 중복 제거 (정규화된 store_name 기준)
Stage C: 상품 상세페이지에서 사업자정보 파싱 (판매자당 대표 상품 1개)
Stage D: 사업자등록번호 기준 중복 제거 + 출력 (JSON/CSV)

안티봇 전략: Scrapling StealthySession (Camoufox 엔진) + 행동 시뮬레이션
"""

import sys
sys.stdout.reconfigure(line_buffering=True)

import json
import re
import time
import random
import csv
import argparse
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional

OUTPUT_DIR = Path(__file__).parent.parent / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ─── 상태 파일 경로 ───────────────────────────────────────────────────────────
STATE_COLLECTED_IDS = OUTPUT_DIR / "coupang_collected_ids.json"
STATE_SELLERS = OUTPUT_DIR / "coupang_sellers.json"
STATE_BUDGET = OUTPUT_DIR / "coupang_budget.json"
STATE_BUSINESS_INDEX = OUTPUT_DIR / "coupang_business_index.json"

# ─── 차단 감지 키워드 ─────────────────────────────────────────────────────────
BLOCK_KEYWORDS = [
    "자동화된 테스트 소프트웨어",
    "접근이 제한",
    "비정상적인 접근",
    "보안 절차",
    "Access Denied",
    "captcha",
]

# ─── 차단 감지 상태 코드 ──────────────────────────────────────────────────────
BLOCK_STATUS_CODES = {403, 418, 429}

# ─── 소프트 블록 기준 (200이지만 비정상적으로 작은 응답) ─────────────────────
SOFT_BLOCK_MAX_BYTES = 1000

# ─── 세션 설정 ────────────────────────────────────────────────────────────────
SESSION_MIN_ITEMS = 3
SESSION_MAX_ITEMS = 5
SESSION_COOLDOWN_MIN = 600  # 초
SESSION_COOLDOWN_MAX = 900  # 초
DECOY_VISIT_CHANCE = 0.30

# ─── Coupang URL 패턴 ─────────────────────────────────────────────────────────
COUPANG_HOME = "https://www.coupang.com"
COUPANG_PRODUCT_URL = "https://www.coupang.com/vp/products/{product_id}"
COUPANG_CATEGORY_URL = "https://www.coupang.com/np/categories/{category_id}?page={page}"
COUPANG_SEARCH_URL = "https://www.coupang.com/np/search?q={keyword}&page={page}"
COUPANG_OMP_URL = "https://www.coupang.com/np/omp"

# ─── 카테고리 예시 (Coupang 주요 카테고리 ID) ────────────────────────────────
COUPANG_CATEGORIES = [
    {"name": "식품", "id": "194273"},
    {"name": "생활용품", "id": "502994"},
    {"name": "가전디지털", "id": "498294"},
    {"name": "주방용품", "id": "290546"},
    {"name": "뷰티", "id": "684282"},
    {"name": "출산/유아동", "id": "502993"},
    {"name": "패션의류", "id": "5000000005"},
    {"name": "스포츠/레저", "id": "502996"},
    {"name": "반려동물용품", "id": "394436"},
    {"name": "가구/홈인테리어", "id": "502995"},
]


# ─── 데이터 모델 ──────────────────────────────────────────────────────────────

@dataclass
class CoupangProduct:
    """Stage A에서 추출하는 상품 정보"""
    product_id: str = ""
    item_id: str = ""
    vendor_item_id: str = ""
    url: str = ""
    title: str = ""
    store_name: str = ""
    price: int = 0
    rating: float = 0.0
    review_count: int = 0
    rocket_delivery: bool = False
    power_seller: bool = False
    seller_grade: str = ""
    source: str = ""
    scanned_at: str = ""


@dataclass
class SellerProfile:
    """Stage C에서 수집하는 판매자 프로필"""
    seller_key: str = ""
    store_name: str = ""
    company_name: str = ""
    ceo_name: str = ""
    phone: str = ""
    business_number: str = ""
    address: str = ""
    power_seller: bool = False
    seller_grade: str = ""
    resolved: bool = False
    parse_method: str = ""  # "dom", "xhr", "failed"
    resolved_at: str = ""


@dataclass
class BusinessRecord:
    """Stage D 최종 출력 레코드"""
    business_number: str = ""
    company_name: str = ""
    ceo_name: str = ""
    email: str = ""
    phone: str = ""
    address: str = ""
    power_seller: bool = False
    seller_grade: str = ""
    store_names: list = field(default_factory=list)
    store_count: int = 0
    product_count: int = 0
    sample_product_id: str = ""
    sample_url: str = ""
    source: str = ""
    collected_at: str = ""


# ─── 사업자등록번호 검증 ──────────────────────────────────────────────────────

def normalize_business_number(raw: str) -> str:
    """사업자등록번호를 10자리 숫자로 정규화 (대시/공백 제거)"""
    digits = re.sub(r'[^0-9]', '', raw)
    if len(digits) == 10:
        return digits
    return raw.strip()


def validate_business_number(number: str) -> bool:
    """
    국세청 사업자등록번호 체크섬 검증
    weights = [1,3,7,1,3,7,1,3,5]
    check = (10 - (sum + (weights[8]*digits[8])//10) % 10) % 10 == digits[9]
    """
    digits_str = re.sub(r'[^0-9]', '', number)
    if len(digits_str) != 10:
        return False

    digits = [int(d) for d in digits_str]
    weights = [1, 3, 7, 1, 3, 7, 1, 3, 5]

    total = sum(w * d for w, d in zip(weights, digits[:9]))
    check_digit = (10 - (total + (weights[8] * digits[8]) // 10) % 10) % 10

    return check_digit == digits[9]


# ─── 상태 관리 ────────────────────────────────────────────────────────────────

def load_json_state(path: Path, default=None):
    """JSON 상태 파일 로드 (손상 시 기본값 반환)"""
    if default is None:
        default = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data
        except (json.JSONDecodeError, OSError) as e:
            print(f"  [경고] 상태 파일 손상 ({path.name}): {e}")
            return default
    return default


def save_json_state(path: Path, data):
    """JSON 상태 파일 저장"""
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"  [오류] 상태 파일 저장 실패 ({path.name}): {e}")


def load_collected_ids() -> set:
    """이미 수집한 product_id 집합 로드"""
    data = load_json_state(STATE_COLLECTED_IDS, default=[])
    if isinstance(data, list):
        return set(data)
    return set()


def save_collected_ids(ids: set):
    """수집된 product_id 집합 저장"""
    save_json_state(STATE_COLLECTED_IDS, sorted(ids))


def load_sellers_state() -> dict:
    """판매자 상태 로드 (seller_key → SellerProfile dict)"""
    data = load_json_state(STATE_SELLERS, default={})
    if isinstance(data, dict):
        return data
    return {}


def save_sellers_state(sellers: dict):
    """판매자 상태 저장"""
    save_json_state(STATE_SELLERS, sellers)


def load_budget_state() -> dict:
    """세션 예산/쿨다운 상태 로드"""
    return load_json_state(STATE_BUDGET, default={
        "session_count": 0,
        "total_items_processed": 0,
        "last_session_time": None,
        "cooldown_multiplier": 1.0,
        "consecutive_blocks": 0,
    })


def save_budget_state(budget: dict):
    """세션 예산/쿨다운 상태 저장"""
    save_json_state(STATE_BUDGET, budget)


def load_business_index() -> dict:
    """사업자번호 인덱스 로드 (크로스런 중복 방지)"""
    data = load_json_state(STATE_BUSINESS_INDEX, default={})
    if isinstance(data, dict):
        return data
    return {}


def save_business_index(index: dict):
    """사업자번호 인덱스 저장"""
    save_json_state(STATE_BUSINESS_INDEX, index)


# ─── 출력 저장 ────────────────────────────────────────────────────────────────

def save_results(results: list[dict], label: str = "", prefix: str = "coupang_business"):
    """JSON + CSV 저장 (utf-8-sig)"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = re.sub(r'[/\\:*?"<>|]', '_', label) if label else ""
    suffix = f"_{safe_label}" if safe_label else ""
    json_path = OUTPUT_DIR / f"{prefix}{suffix}_{ts}.json"
    csv_path = OUTPUT_DIR / f"{prefix}{suffix}_{ts}.csv"

    json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    if results:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=results[0].keys())
            writer.writeheader()
            writer.writerows(results)

    return json_path, csv_path


def save_unresolved(sellers: list[dict]):
    """미해결 판매자 저장"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = OUTPUT_DIR / f"coupang_unresolved_{ts}.json"
    csv_path = OUTPUT_DIR / f"coupang_unresolved_{ts}.csv"

    json_path.write_text(json.dumps(sellers, ensure_ascii=False, indent=2), encoding="utf-8")

    if sellers:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=sellers[0].keys())
            writer.writeheader()
            writer.writerows(sellers)

    return json_path, csv_path


# ─── 차단 감지 ────────────────────────────────────────────────────────────────

def is_blocked(response) -> tuple[bool, str]:
    """
    응답에서 차단 여부 감지
    반환: (차단여부, 사유)
    """
    # 상태 코드 확인
    status = getattr(response, 'status', None) or getattr(response, 'status_code', 0)
    if status in BLOCK_STATUS_CODES:
        return True, f"HTTP {status}"

    # 소프트 블록: 200이지만 비정상적으로 작은 응답
    html = getattr(response, 'html_content', '') or ''
    if status == 200 and len(html.encode('utf-8')) < SOFT_BLOCK_MAX_BYTES:
        return True, f"소프트 블록 (응답 {len(html.encode('utf-8'))} bytes)"

    # 차단 키워드 확인
    for keyword in BLOCK_KEYWORDS:
        if keyword in html:
            return True, f"차단 키워드 감지: {keyword}"

    return False, ""


# ─── 행동 시뮬레이션 (page_action 콜백) ──────────────────────────────────────

def warmup_page_action(page):
    """
    홈 페이지 웜업용 page_action 콜백.
    _abck 센서가 의미 있는 텔레메트리를 수집하도록 실제 상호작용 주입.
    (BYPASS_TECHNICAL_GUIDE §11)
    """
    try:
        # 마우스를 viewport 중앙으로 이동 (Camoufox C++ 베지에 궤적 자동 생성)
        page.mouse.move(random.randint(400, 800), random.randint(300, 500))
        time.sleep(random.uniform(0.2, 0.5))

        # 2~4회 소폭 스크롤 (keyboard — JS scrollTo 아님)
        for _ in range(random.randint(2, 4)):
            page.keyboard.press("PageDown")
            time.sleep(random.uniform(0.3, 1.0))

        # 검색창 포커스 + 타이핑 시뮬레이션 (키스트로크 다이나믹스)
        page.keyboard.type("쿠팡", delay=random.randint(80, 200))
        time.sleep(random.uniform(0.5, 1.5))
        page.keyboard.press("Escape")

        # 1회 랜덤 클릭 (비상품 영역 — 좌표만, 링크 클릭 회피)
        page.mouse.click(random.randint(50, 200), random.randint(600, 750))
        time.sleep(random.uniform(1.0, 2.0))

        # 상단 복귀
        page.keyboard.press("Home")
        time.sleep(random.uniform(0.3, 0.8))
    except Exception:
        pass


def listing_page_action(page):
    """리스팅 페이지용 page_action: 가벼운 스크롤 + 마우스 이동"""
    try:
        page.mouse.move(random.randint(300, 900), random.randint(200, 500))
        time.sleep(random.uniform(0.2, 0.4))
        for _ in range(random.randint(2, 4)):
            page.keyboard.press("PageDown")
            time.sleep(random.uniform(0.3, 0.7))
        page.mouse.move(random.randint(300, 900), random.randint(200, 500))
        time.sleep(random.uniform(0.2, 0.4))
    except Exception:
        pass


def detail_page_action(page):
    """상세페이지용 page_action: 하단까지 스크롤 (사업자정보 란 접근)"""
    try:
        # 마우스 이동
        page.mouse.move(random.randint(400, 800), random.randint(300, 500))
        time.sleep(random.uniform(0.2, 0.4))
        # End + PageDown 반복으로 최하단 도달
        for _ in range(random.randint(6, 12)):
            page.keyboard.press("End")
            time.sleep(random.uniform(0.2, 0.4))
            page.keyboard.press("PageDown")
            time.sleep(random.uniform(0.3, 0.6))
        # 하단에서 잠시 대기 (사업자정보 란 렌더링)
        time.sleep(random.uniform(1.0, 2.0))
        # 자연스럽게 조금 위로
        page.keyboard.press("PageUp")
        time.sleep(random.uniform(0.3, 0.6))
    except Exception:
        pass


# ─── 세션 관리 ────────────────────────────────────────────────────────────────

def create_stealthy_session(headless: bool = True, proxy: Optional[str] = None):
    """StealthySession 생성 (Camoufox 엔진)
    주의: block_ads=True는 Akamai 센서 엔드포인트를 차단할 수 있으므로 사용하지 않음
    """
    from scrapling.fetchers import StealthySession

    kwargs = {
        "headless": headless,
    }
    if proxy:
        kwargs["proxy"] = proxy

    session = StealthySession(**kwargs)
    return session


def warmup_session(session, delay_mult: float = 1.0):
    """
    세션 웜업: coupang.com 홈 페이지 방문 + page_action으로 행동 시뮬레이션 주입
    google_search=True로 자연스러운 Google referer 체인 생성 (_abck 검증에 필수)
    """
    print("  [웜업] coupang.com 홈 페이지 방문 중...")
    response = session.fetch(
        COUPANG_HOME,
        network_idle=True,
        google_search=True,
        page_action=warmup_page_action,
        timeout=45000,
    )

    blocked, reason = is_blocked(response)
    if blocked:
        print(f"  [경고] 웜업 중 차단 감지: {reason}")
        return False

    # _abck 센서 POST + 서버 검증에 충분한 대기 (최소 25초, delay_multiplier 미적용)
    wait = random.uniform(25.0, 35.0)
    print(f"  [웜업] _abck 처리 대기 {wait:.0f}초...")
    time.sleep(wait)
    print("  [웜업] 완료")
    return True


def should_start_new_session(budget: dict) -> bool:
    """새 세션을 시작해야 하는지 판단"""
    items_in_session = budget.get("items_in_current_session", 0)
    batch_size = budget.get("current_batch_size", SESSION_MAX_ITEMS)
    return items_in_session >= batch_size


def get_session_cooldown(budget: dict, delay_mult: float = 1.0) -> float:
    """세션 간 쿨다운 시간 계산 (적응형 백오프 포함)"""
    base_cooldown = random.uniform(SESSION_COOLDOWN_MIN, SESSION_COOLDOWN_MAX)
    multiplier = budget.get("cooldown_multiplier", 1.0)
    return base_cooldown * multiplier * delay_mult


def apply_adaptive_backoff(budget: dict, blocked: bool):
    """차단 시 적응형 백오프 적용"""
    if blocked:
        budget["consecutive_blocks"] = budget.get("consecutive_blocks", 0) + 1
        # 연속 차단 시 쿨다운 배율 증가 (최대 5배)
        budget["cooldown_multiplier"] = min(
            5.0,
            1.0 + budget["consecutive_blocks"] * 0.5
        )
        print(f"  [백오프] 연속 차단 {budget['consecutive_blocks']}회, "
              f"쿨다운 배율: {budget['cooldown_multiplier']:.1f}x")
    else:
        # 성공 시 점진적 회복
        if budget.get("consecutive_blocks", 0) > 0:
            budget["consecutive_blocks"] = max(0, budget["consecutive_blocks"] - 1)
            budget["cooldown_multiplier"] = max(
                1.0,
                budget.get("cooldown_multiplier", 1.0) - 0.25
            )


def maybe_decoy_visit(session, delay_mult: float = 1.0):
    """30% 확률로 디코이 방문 (봇 탐지 회피)"""
    if random.random() < DECOY_VISIT_CHANCE:
        decoy_urls = [
            "https://www.coupang.com/np/campaigns/82",
            "https://www.coupang.com/np/goldbox",
            "https://www.coupang.com/np/categories/186764",
        ]
        url = random.choice(decoy_urls)
        print(f"  [디코이] {url} 방문...")
        try:
            session.fetch(url, network_idle=True, google_search=False, timeout=15000)
            time.sleep(random.uniform(2.0, 5.0) * delay_mult)
        except Exception:
            pass


# ─── Stage A: 리스팅 스캔 ───────────────────────────────────────────────────

def parse_listing_products(html: str, source: str) -> list[CoupangProduct]:
    """
    리스팅 페이지 HTML에서 상품 정보 추출
    product_id, store_name, itemId, vendorItemId, price, rating, review_count, power_seller
    """
    products = []
    now = datetime.now().isoformat()

    # product_id 추출: /vp/products/{id} 패턴
    product_ids = re.findall(r'/vp/products/(\d+)', html)
    product_ids = list(dict.fromkeys(product_ids))  # 순서 유지 중복 제거

    # vendorItemId 추출
    vendor_item_ids = re.findall(r'vendorItemId[=:]["\s]*(\d+)', html)
    vendor_item_ids = list(dict.fromkeys(vendor_item_ids))

    # itemId 추출
    item_ids = re.findall(r'itemId[=:]["\s]*(\d+)', html)
    item_ids = list(dict.fromkeys(item_ids))

    for idx, pid in enumerate(product_ids):
        product = CoupangProduct(
            product_id=pid,
            item_id=item_ids[idx] if idx < len(item_ids) else "",
            vendor_item_id=vendor_item_ids[idx] if idx < len(vendor_item_ids) else "",
            url=COUPANG_PRODUCT_URL.format(product_id=pid),
            title="",
            store_name="",
            price=0,
            rating=0.0,
            review_count=0,
            rocket_delivery=False,
            power_seller=False,
            seller_grade="",
            source=source,
            scanned_at=now,
        )
        products.append(product)

    # store_name 추출 시도 (JSON 데이터 또는 DOM에서)
    store_names = re.findall(
        r'"(?:sellerName|storeName|vendorName)"\s*:\s*"([^"]+)"', html
    )
    for idx, name in enumerate(store_names):
        if idx < len(products):
            products[idx].store_name = name

    # 가격 추출
    prices = re.findall(r'"(?:price|salePrice)"\s*:\s*(\d+)', html)
    for idx, p in enumerate(prices):
        if idx < len(products):
            products[idx].price = int(p)

    # 평점 추출
    ratings = re.findall(r'"(?:rating|averageRating)"\s*:\s*([\d.]+)', html)
    for idx, r in enumerate(ratings):
        if idx < len(products):
            products[idx].rating = float(r)

    # 리뷰 수 추출
    review_counts = re.findall(r'"(?:reviewCount|commentCount)"\s*:\s*(\d+)', html)
    for idx, rc in enumerate(review_counts):
        if idx < len(products):
            products[idx].review_count = int(rc)

    # 로켓배송 여부
    if "로켓배송" in html or "rocket" in html.lower():
        rocket_sections = re.findall(r'로켓배송.*?/vp/products/(\d+)', html)
        rocket_ids = set(rocket_sections)
        for product in products:
            if product.product_id in rocket_ids:
                product.rocket_delivery = True

    # 파워셀러 배지
    power_seller_patterns = [
        r'파워셀러.*?/vp/products/(\d+)',
        r'"powerSeller"\s*:\s*true',
    ]
    for pattern in power_seller_patterns:
        matches = re.findall(pattern, html)
        for m in matches:
            for product in products:
                if product.product_id == m:
                    product.power_seller = True

    return products


def scan_listing_page(session, url: str, source: str, delay_mult: float = 1.0) -> list[CoupangProduct]:
    """리스팅 페이지 1개 스캔"""
    try:
        response = session.fetch(
            url, network_idle=True, google_search=False,
            page_action=listing_page_action, timeout=25000,
        )
    except Exception as e:
        print(f"  [오류] 리스팅 접근 실패: {e}")
        return []

    blocked, reason = is_blocked(response)
    if blocked:
        print(f"  [차단] 리스팅 접근 차단: {reason}")
        return []

    html = getattr(response, 'html_content', '') or ''
    if not html:
        print("  [경고] 리스팅 응답 비어있음")
        return []

    products = parse_listing_products(html, source)
    return products


def run_stage_a(pages: int = 3, source: str = "category",
                category_id: str = "", keyword: str = "",
                headless: bool = True, proxy: Optional[str] = None,
                delay_mult: float = 1.0) -> list[CoupangProduct]:
    """
    Stage A: OMP/카테고리 리스팅 스캔
    상품 목록에서 product_id, store_name, itemId, vendorItemId, price, rating, review_count, power_seller 추출
    """
    print(f"\n{'='*60}")
    print(f"[Stage A] 리스팅 스캔 시작 (소스: {source}, 페이지: {pages})")
    print(f"{'='*60}")

    all_products = []
    collected_ids = load_collected_ids()
    budget = load_budget_state()

    # 소스별 URL 목록 생성
    urls_to_scan = []
    if source == "category":
        cat_id = category_id or COUPANG_CATEGORIES[0]["id"]
        cat_name = category_id or COUPANG_CATEGORIES[0]["name"]
        for page_num in range(1, pages + 1):
            url = COUPANG_CATEGORY_URL.format(category_id=cat_id, page=page_num)
            urls_to_scan.append((url, f"category/{cat_name}/p{page_num}"))
    elif source == "search":
        kw = keyword or "생필품"
        for page_num in range(1, pages + 1):
            url = COUPANG_SEARCH_URL.format(keyword=kw, page=page_num)
            urls_to_scan.append((url, f"search/{kw}/p{page_num}"))
    elif source == "omp":
        for page_num in range(1, pages + 1):
            url = f"{COUPANG_OMP_URL}?page={page_num}"
            urls_to_scan.append((url, f"omp/p{page_num}"))
    else:
        print(f"  [오류] 알 수 없는 소스: {source}")
        return []

    # 세션 생성 및 웜업
    session = create_stealthy_session(headless=headless, proxy=proxy)
    try:
        with session:
            warmup_ok = warmup_session(session, delay_mult)
            if not warmup_ok:
                print("  [경고] 웜업 실패, 계속 진행...")

            for url, label in urls_to_scan:
                print(f"\n  [스캔] {label}: {url}")

                products = scan_listing_page(session, url, label, delay_mult)
                print(f"  [결과] 상품 {len(products)}개 추출")

                # 이미 수집한 상품 제외
                new_products = [p for p in products if p.product_id not in collected_ids]
                print(f"  [신규] {len(new_products)}개 (기수집 제외: {len(products) - len(new_products)})")

                all_products.extend(new_products)

                # 페이지 간 딜레이
                time.sleep(random.uniform(3.0, 7.0) * delay_mult)

                # 디코이 방문 (첫 페이지 성공 후, 30% 확률)
                if products:
                    maybe_decoy_visit(session, delay_mult)

    except Exception as e:
        print(f"  [오류] Stage A 세션 오류: {e}")

    # 중복 제거 (product_id 기준)
    seen = set()
    unique_products = []
    for p in all_products:
        if p.product_id not in seen:
            seen.add(p.product_id)
            unique_products.append(p)

    print(f"\n[Stage A 완료] 총 {len(unique_products)}개 상품 추출")
    return unique_products


# ─── Stage B: 판매자 중복 제거 ────────────────────────────────────────────────

def normalize_store_name(name: str) -> str:
    """스토어명 정규화 (공백, 특수문자, 대소문자 통일)"""
    if not name:
        return ""
    # 소문자 변환
    normalized = name.lower().strip()
    # 연속 공백 제거
    normalized = re.sub(r'\s+', ' ', normalized)
    # 특수문자 제거 (영문/한글/숫자/공백만 유지)
    normalized = re.sub(r'[^a-z0-9가-힣\s]', '', normalized)
    # 앞뒤 공백 제거
    normalized = normalized.strip()
    return normalized


def run_stage_b(products: list[CoupangProduct]) -> dict[str, dict]:
    """
    Stage B: 판매자 중복 제거
    수천 개 상품 → 고유 판매자 (정규화된 store_name 기준)
    반환: {seller_key: {"store_name": ..., "products": [...], "representative": CoupangProduct}}
    """
    print(f"\n{'='*60}")
    print(f"[Stage B] 판매자 중복 제거 시작 (상품 {len(products)}개)")
    print(f"{'='*60}")

    sellers = {}

    for product in products:
        # store_name이 없으면 product_id를 키로 사용 (개별 판매자 취급)
        raw_name = product.store_name or f"unknown_{product.product_id}"
        seller_key = normalize_store_name(raw_name)

        if not seller_key:
            seller_key = f"unknown_{product.product_id}"

        if seller_key not in sellers:
            sellers[seller_key] = {
                "store_name": product.store_name or raw_name,
                "products": [],
                "representative": None,
                "power_seller": False,
                "seller_grade": "",
            }

        sellers[seller_key]["products"].append(product)

        # 대표 상품 선정: 리뷰 수가 가장 많은 상품
        current_rep = sellers[seller_key]["representative"]
        if current_rep is None or product.review_count > current_rep.review_count:
            sellers[seller_key]["representative"] = product

        # 파워셀러 배지 통합
        if product.power_seller:
            sellers[seller_key]["power_seller"] = True

        if product.seller_grade:
            sellers[seller_key]["seller_grade"] = product.seller_grade

    # 상태 저장
    sellers_state = load_sellers_state()
    for key, info in sellers.items():
        if key not in sellers_state:
            sellers_state[key] = {
                "store_name": info["store_name"],
                "product_count": len(info["products"]),
                "representative_product_id": info["representative"].product_id if info["representative"] else "",
                "power_seller": info["power_seller"],
                "resolved": False,
            }
        else:
            sellers_state[key]["product_count"] = len(info["products"])
    save_sellers_state(sellers_state)

    print(f"[Stage B 완료] 고유 판매자 {len(sellers)}명 (상품 {len(products)}개 → 판매자 {len(sellers)}명)")
    return sellers


# ─── Stage C: 상세페이지 사업자정보 파싱 ─────────────────────────────────────

def parse_business_info_dom(html: str) -> tuple[dict, str]:
    """
    Priority 1: DOM 직접 파싱 (사업자정보 란)
    여러 fallback 셀렉터 시도 (셀렉터 미검증 상태)
    반환: (파싱된 정보 dict, 파싱 방법)
    """
    info = {
        "company_name": "",
        "ceo_name": "",
        "business_number": "",
        "address": "",
        "phone": "",
    }

    # 라벨 기반 매핑 테이블
    label_map = {
        "상호": "company_name",
        "상호명": "company_name",
        "대표자": "ceo_name",
        "대표자명": "ceo_name",
        "사업자등록번호": "business_number",
        "사업자 번호": "business_number",
        "소재지": "address",
        "주소": "address",
        "사업장 소재지": "address",
        "전화": "phone",
        "전화번호": "phone",
        "연락처": "phone",
        "대표전화": "phone",
    }

    # 방법 1: class 기반 셀렉터 패턴으로 JSON 데이터 추출
    # Coupang은 주로 script 태그 내 JSON에 사업자정보를 포함
    json_patterns = [
        r'"(?:companyName|sellerName|vendorName)"\s*:\s*"([^"]*)"',
        r'"(?:ceoName|representativeName|ownerName)"\s*:\s*"([^"]*)"',
        r'"(?:businessNumber|bizRegNumber|businessRegistrationNumber)"\s*:\s*"([^"]*)"',
        r'"(?:address|businessAddress|companyAddress)"\s*:\s*"([^"]*)"',
        r'"(?:phone|phoneNumber|tel|contactNumber)"\s*:\s*"([^"]*)"',
    ]

    field_order = ["company_name", "ceo_name", "business_number", "address", "phone"]
    for pattern, field_name in zip(json_patterns, field_order):
        match = re.search(pattern, html)
        if match:
            info[field_name] = match.group(1).strip()

    # 방법 2: HTML 라벨-값 쌍 파싱
    # 사업자정보 섹션의 다양한 DOM 구조 대응
    section_patterns = [
        r'class="[^"]*seller-business[^"]*"(.*?)(?:</div>\s*</div>|</section>)',
        r'class="[^"]*vendor-info[^"]*"(.*?)(?:</div>\s*</div>|</section>)',
        r'class="[^"]*business-info[^"]*"(.*?)(?:</div>\s*</div>|</section>)',
        r'사업자정보(.*?)(?:</div>\s*</div>|</section>|</table>)',
    ]

    for section_pattern in section_patterns:
        section_match = re.search(section_pattern, html, re.DOTALL)
        if not section_match:
            continue

        section_html = section_match.group(1)

        # 라벨: 값 패턴 (dt/dd, span/span, th/td 등)
        pair_patterns = [
            r'<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>',
            r'<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>',
            r'<span[^>]*class="[^"]*label[^"]*"[^>]*>(.*?)</span>\s*<span[^>]*class="[^"]*value[^"]*"[^>]*>(.*?)</span>',
            r'<span[^>]*>(.*?)</span>\s*[:：]\s*<span[^>]*>(.*?)</span>',
            r'([\uac00-\ud7a3]+)\s*[:：]\s*([^<\n]+)',
        ]

        for pair_pattern in pair_patterns:
            pairs = re.findall(pair_pattern, section_html, re.DOTALL)
            for label_raw, value_raw in pairs:
                # HTML 태그 제거
                label = re.sub(r'<[^>]+>', '', label_raw).strip()
                value = re.sub(r'<[^>]+>', '', value_raw).strip()

                if not value:
                    continue

                for key, field_name in label_map.items():
                    if key in label and not info[field_name]:
                        info[field_name] = value
                        break

    # 방법 3: 페이지 전체에서 라벨 기반 추출 (fallback)
    for label, field_name in label_map.items():
        if info[field_name]:
            continue

        # "라벨: 값" 또는 "라벨 값" 패턴
        patterns = [
            rf'{label}\s*[:：]\s*([^<\n"{{]+)',
            rf'{label}[^<]*</[^>]+>\s*<[^>]+>([^<]+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                value = match.group(1).strip()
                # 너무 긴 값은 제외 (다른 콘텐츠일 가능성)
                if value and len(value) < 100:
                    info[field_name] = value
                    break

    # 사업자등록번호 정규화
    if info["business_number"]:
        info["business_number"] = normalize_business_number(info["business_number"])

    # 파싱 성공 여부 판단
    filled = sum(1 for v in info.values() if v)
    if filled > 0:
        return info, "dom"

    return info, ""


def parse_business_info_xhr(html: str) -> tuple[dict, str]:
    """
    Priority 2: XHR 응답 데이터에서 사업자정보 추출
    페이지 내 임베드된 JSON/XHR 응답 캡처 데이터 파싱
    """
    info = {
        "company_name": "",
        "ceo_name": "",
        "business_number": "",
        "address": "",
        "phone": "",
    }

    # XHR 응답 패턴: script 태그 내 JSON 또는 window.__INITIAL_STATE__ 등
    xhr_patterns = [
        r'window\.__INITIAL_STATE__\s*=\s*({.*?});',
        r'window\.__NEXT_DATA__\s*=\s*({.*?});',
        r'"sellerInfo"\s*:\s*({[^}]+})',
        r'"businessInfo"\s*:\s*({[^}]+})',
        r'"vendorInfo"\s*:\s*({[^}]+})',
    ]

    for pattern in xhr_patterns:
        match = re.search(pattern, html, re.DOTALL)
        if not match:
            continue

        try:
            json_str = match.group(1)
            # 너무 큰 JSON은 파싱 실패할 수 있으므로 제한
            if len(json_str) > 50000:
                continue
            data = json.loads(json_str)
        except (json.JSONDecodeError, ValueError):
            continue

        # 재귀적으로 사업자정보 필드 탐색
        _extract_business_fields(data, info)

        filled = sum(1 for v in info.values() if v)
        if filled > 0:
            if info["business_number"]:
                info["business_number"] = normalize_business_number(info["business_number"])
            return info, "xhr"

    # XHR 엔드포인트 응답 패턴 (fetch/ajax 응답이 HTML에 인라인된 경우)
    inline_xhr_patterns = [
        r'"(?:companyName|company)"\s*:\s*"([^"]+)".*?"(?:businessNumber|bizNo)"\s*:\s*"([^"]+)"',
        r'"(?:bizRegNo|businessRegistrationNo)"\s*:\s*"([^"]+)".*?"(?:companyName|company)"\s*:\s*"([^"]+)"',
    ]

    for pattern in inline_xhr_patterns:
        match = re.search(pattern, html, re.DOTALL)
        if match:
            groups = match.groups()
            if "businessNumber" in pattern or "bizNo" in pattern or "bizRegNo" in pattern:
                info["business_number"] = normalize_business_number(groups[1] if len(groups) > 1 else groups[0])
                info["company_name"] = groups[0] if len(groups) > 1 else groups[1]
            else:
                info["company_name"] = groups[0]
                info["business_number"] = normalize_business_number(groups[1])

            filled = sum(1 for v in info.values() if v)
            if filled > 0:
                return info, "xhr"

    return info, ""


def _extract_business_fields(data, info: dict, depth: int = 0):
    """JSON 데이터에서 사업자정보 필드 재귀 추출"""
    if depth > 5:
        return

    field_mapping = {
        "companyname": "company_name",
        "company_name": "company_name",
        "sellername": "company_name",
        "vendorname": "company_name",
        "ceoname": "ceo_name",
        "ceo_name": "ceo_name",
        "representativename": "ceo_name",
        "ownername": "ceo_name",
        "businessnumber": "business_number",
        "business_number": "business_number",
        "bizregnumber": "business_number",
        "bizno": "business_number",
        "businessregistrationnumber": "business_number",
        "address": "address",
        "businessaddress": "address",
        "companyaddress": "address",
        "phone": "phone",
        "phonenumber": "phone",
        "tel": "phone",
        "contactnumber": "phone",
        "representativephone": "phone",
    }

    if isinstance(data, dict):
        for key, value in data.items():
            key_lower = key.lower().replace("_", "").replace("-", "")
            if key_lower in field_mapping:
                field_name = field_mapping[key_lower]
                if isinstance(value, str) and value.strip() and not info[field_name]:
                    info[field_name] = value.strip()
            elif isinstance(value, (dict, list)):
                _extract_business_fields(value, info, depth + 1)
    elif isinstance(data, list):
        for item in data[:10]:  # 처음 10개만 탐색
            if isinstance(item, (dict, list)):
                _extract_business_fields(item, info, depth + 1)


def scrape_seller_detail(session, product: CoupangProduct, delay_mult: float = 1.0) -> tuple[dict, str, bool]:
    """
    상품 상세페이지에서 사업자정보 파싱
    page_action으로 하단 스크롤 → 사업자정보 란 렌더링 대기
    반환: (info_dict, parse_method, blocked)
    """
    url = product.url
    print(f"    [상세] {url}")

    try:
        response = session.fetch(
            url, network_idle=True, google_search=False,
            page_action=detail_page_action, timeout=30000,
        )
    except Exception as e:
        print(f"    [오류] 상세페이지 접근 실패: {e}")
        return {}, "", False

    blocked, reason = is_blocked(response)
    if blocked:
        print(f"    [차단] 상세페이지 차단: {reason}")
        return {}, "", True

    html = getattr(response, 'html_content', '') or ''
    if not html:
        print(f"    [경고] 상세페이지 응답 비어있음")
        return {}, "", False

    # Priority 1: DOM 직접 파싱
    info, method = parse_business_info_dom(html)
    if method:
        print(f"    [파싱] DOM 파싱 성공: {sum(1 for v in info.values() if v)}/5 필드")
        return info, method, False

    # Priority 2: XHR 캡처 fallback
    info, method = parse_business_info_xhr(html)
    if method:
        print(f"    [파싱] XHR 파싱 성공: {sum(1 for v in info.values() if v)}/5 필드")
        return info, method, False

    print(f"    [미스] 사업자정보 파싱 실패")
    return info, "failed", False


def run_stage_c(sellers: dict[str, dict], max_sellers: int = 50,
                headless: bool = True, proxy: Optional[str] = None,
                delay_mult: float = 1.0) -> list[SellerProfile]:
    """
    Stage C: 상세페이지 스크래핑
    판매자당 대표 상품 1개 방문 → 사업자정보 파싱
    세션 관리: 3-5개 항목 per 세션, 600-900초 쿨다운
    """
    print(f"\n{'='*60}")
    print(f"[Stage C] 상세페이지 사업자정보 수집 시작 (최대 {max_sellers}명)")
    print(f"{'='*60}")

    # 이미 해결된 판매자 제외
    sellers_state = load_sellers_state()
    targets = []
    for key, info in sellers.items():
        state = sellers_state.get(key, {})
        if state.get("resolved", False):
            continue
        rep = info.get("representative")
        if rep:
            targets.append((key, info, rep))

    targets = targets[:max_sellers]
    print(f"  대상 판매자: {len(targets)}명 (이미 해결: {len(sellers) - len(targets)})")

    if not targets:
        print("  수집할 판매자 없음.")
        return []

    profiles = []
    budget = load_budget_state()
    budget["items_in_current_session"] = 0
    budget["current_batch_size"] = random.randint(SESSION_MIN_ITEMS, SESSION_MAX_ITEMS)

    session_idx = 0
    item_idx = 0

    while item_idx < len(targets):
        # 새 세션 시작
        session_idx += 1
        batch_size = budget.get("current_batch_size", SESSION_MAX_ITEMS)
        batch_end = min(item_idx + batch_size, len(targets))
        batch = targets[item_idx:batch_end]

        print(f"\n  ─── 세션 {session_idx} (배치 {len(batch)}개, "
              f"쿨다운 배율: {budget.get('cooldown_multiplier', 1.0):.1f}x) ───")

        session = create_stealthy_session(headless=headless, proxy=proxy)
        session_blocked = False

        try:
            with session:
                # 웜업
                warmup_ok = warmup_session(session, delay_mult)
                if not warmup_ok:
                    print("  [경고] 웜업 중 차단, 세션 스킵")
                    session_blocked = True
                    apply_adaptive_backoff(budget, True)
                    save_budget_state(budget)
                    # 쿨다운 대기
                    cooldown = get_session_cooldown(budget, delay_mult)
                    print(f"  [쿨다운] {cooldown:.0f}초 대기...")
                    time.sleep(cooldown)
                    item_idx = batch_end
                    continue

                for seller_key, seller_info, rep_product in batch:
                    print(f"\n  [{item_idx + 1}/{len(targets)}] "
                          f"판매자: {seller_info['store_name']}")

                    # 디코이 방문 (30% 확률)
                    maybe_decoy_visit(session, delay_mult)

                    # 상세페이지 스크래핑
                    info, method, blocked = scrape_seller_detail(
                        session, rep_product, delay_mult
                    )

                    if blocked:
                        session_blocked = True
                        apply_adaptive_backoff(budget, True)
                        print(f"    [중단] 세션 차단으로 남은 항목 스킵")
                        break

                    apply_adaptive_backoff(budget, False)

                    # SellerProfile 생성
                    profile = SellerProfile(
                        seller_key=seller_key,
                        store_name=seller_info["store_name"],
                        company_name=info.get("company_name", ""),
                        ceo_name=info.get("ceo_name", ""),
                        phone=info.get("phone", ""),
                        business_number=info.get("business_number", ""),
                        address=info.get("address", ""),
                        power_seller=seller_info.get("power_seller", False),
                        seller_grade=seller_info.get("seller_grade", ""),
                        resolved=bool(info.get("company_name") or info.get("business_number")),
                        parse_method=method or "failed",
                        resolved_at=datetime.now().isoformat() if method and method != "failed" else "",
                    )
                    profiles.append(profile)

                    # 상태 즉시 저장 (판매자마다)
                    sellers_state[seller_key] = {
                        "store_name": profile.store_name,
                        "company_name": profile.company_name,
                        "ceo_name": profile.ceo_name,
                        "business_number": profile.business_number,
                        "phone": profile.phone,
                        "address": profile.address,
                        "power_seller": profile.power_seller,
                        "resolved": profile.resolved,
                        "parse_method": profile.parse_method,
                        "resolved_at": profile.resolved_at,
                    }
                    save_sellers_state(sellers_state)

                    # 수집된 ID에 대표 상품 추가
                    collected_ids = load_collected_ids()
                    collected_ids.add(rep_product.product_id)
                    save_collected_ids(collected_ids)

                    # 예산 업데이트
                    budget["items_in_current_session"] = budget.get("items_in_current_session", 0) + 1
                    budget["total_items_processed"] = budget.get("total_items_processed", 0) + 1
                    save_budget_state(budget)

                    # 항목 간 딜레이
                    time.sleep(random.uniform(5.0, 12.0) * delay_mult)

        except Exception as e:
            print(f"  [오류] 세션 {session_idx} 오류: {e}")
            session_blocked = True

        # 세션 종료 후 처리
        budget["session_count"] = budget.get("session_count", 0) + 1
        budget["last_session_time"] = datetime.now().isoformat()
        budget["items_in_current_session"] = 0
        budget["current_batch_size"] = random.randint(SESSION_MIN_ITEMS, SESSION_MAX_ITEMS)
        save_budget_state(budget)

        item_idx = batch_end

        # 세션 간 쿨다운 (마지막 세션이 아니면)
        if item_idx < len(targets) and not session_blocked:
            cooldown = get_session_cooldown(budget, delay_mult)
            print(f"\n  [쿨다운] 다음 세션까지 {cooldown:.0f}초 대기...")
            time.sleep(cooldown)
        elif session_blocked and item_idx < len(targets):
            # 차단 시 더 긴 쿨다운
            cooldown = get_session_cooldown(budget, delay_mult) * 1.5
            print(f"\n  [차단 쿨다운] {cooldown:.0f}초 대기...")
            time.sleep(cooldown)

    resolved_count = sum(1 for p in profiles if p.resolved)
    print(f"\n[Stage C 완료] {len(profiles)}명 처리, "
          f"해결: {resolved_count}명, 미해결: {len(profiles) - resolved_count}명")

    return profiles


# ─── Stage D: 사업자번호 중복 제거 + 출력 ─────────────────────────────────────

def run_stage_d(profiles: list[SellerProfile], sellers: dict[str, dict],
                source: str = "coupang") -> list[dict]:
    """
    Stage D: 사업자등록번호 기준 중복 제거 + 최종 출력
    여러 스토어가 같은 사업자번호를 공유하면 병합
    """
    print(f"\n{'='*60}")
    print(f"[Stage D] 사업자번호 중복 제거 + 출력")
    print(f"{'='*60}")

    business_index = load_business_index()
    records_by_biz = {}  # business_number → BusinessRecord
    no_biz_records = []  # 사업자번호 없는 경우

    for profile in profiles:
        if not profile.resolved:
            continue

        biz_num = profile.business_number

        if biz_num and validate_business_number(biz_num):
            # 유효한 사업자번호: 병합
            if biz_num not in records_by_biz:
                # seller_info에서 상품 수 조회
                seller_info = sellers.get(profile.seller_key, {})
                product_count = len(seller_info.get("products", []))
                rep_product_id = ""
                rep_url = ""
                if seller_info.get("representative"):
                    rep = seller_info["representative"]
                    rep_product_id = rep.product_id if hasattr(rep, 'product_id') else ""
                    rep_url = rep.url if hasattr(rep, 'url') else ""

                records_by_biz[biz_num] = BusinessRecord(
                    business_number=biz_num,
                    company_name=profile.company_name,
                    ceo_name=profile.ceo_name,
                    email="",
                    phone=profile.phone,
                    address=profile.address,
                    power_seller=profile.power_seller,
                    seller_grade=profile.seller_grade,
                    store_names=[profile.store_name] if profile.store_name else [],
                    store_count=1,
                    product_count=product_count,
                    sample_product_id=rep_product_id,
                    sample_url=rep_url,
                    source=source,
                    collected_at=datetime.now().isoformat(),
                )
            else:
                # 기존 레코드에 병합
                existing = records_by_biz[biz_num]
                if profile.store_name and profile.store_name not in existing.store_names:
                    existing.store_names.append(profile.store_name)
                    existing.store_count = len(existing.store_names)
                # 빈 필드 보충
                if not existing.company_name and profile.company_name:
                    existing.company_name = profile.company_name
                if not existing.ceo_name and profile.ceo_name:
                    existing.ceo_name = profile.ceo_name
                if not existing.phone and profile.phone:
                    existing.phone = profile.phone
                if not existing.address and profile.address:
                    existing.address = profile.address
                if profile.power_seller:
                    existing.power_seller = True

                seller_info = sellers.get(profile.seller_key, {})
                existing.product_count += len(seller_info.get("products", []))

        elif biz_num:
            # 사업자번호가 있지만 체크섬 검증 실패
            print(f"  [경고] 사업자번호 검증 실패: {biz_num} ({profile.store_name})")
            # 그래도 기록은 유지 (검증 실패 표시)
            no_biz_records.append(profile)
        else:
            # 사업자번호 없음
            no_biz_records.append(profile)

    # 사업자번호 인덱스 업데이트 (크로스런 중복 방지)
    for biz_num in records_by_biz:
        if biz_num not in business_index:
            business_index[biz_num] = {
                "company_name": records_by_biz[biz_num].company_name,
                "first_seen": datetime.now().isoformat(),
                "store_names": records_by_biz[biz_num].store_names,
            }
        else:
            # 기존 인덱스에 새 스토어명 추가
            existing_stores = set(business_index[biz_num].get("store_names", []))
            existing_stores.update(records_by_biz[biz_num].store_names)
            business_index[biz_num]["store_names"] = list(existing_stores)
    save_business_index(business_index)

    # 최종 출력 레코드 생성
    final_records = []
    for biz_num, record in records_by_biz.items():
        final_records.append(asdict(record))

    # 사업자번호 없는 판매자도 별도 기록 (참고용)
    for profile in no_biz_records:
        if profile.company_name or profile.phone:
            final_records.append({
                "business_number": profile.business_number or "(미확인)",
                "company_name": profile.company_name,
                "ceo_name": profile.ceo_name,
                "email": "",
                "phone": profile.phone,
                "address": profile.address,
                "power_seller": profile.power_seller,
                "seller_grade": profile.seller_grade,
                "store_names": [profile.store_name] if profile.store_name else [],
                "store_count": 1,
                "product_count": 0,
                "sample_product_id": "",
                "sample_url": "",
                "source": source,
                "collected_at": datetime.now().isoformat(),
            })

    # 저장
    if final_records:
        jp, cp = save_results(final_records, label="ALL")
        print(f"\n  [저장] JSON: {jp}")
        print(f"  [저장] CSV:  {cp}")

    # 미해결 판매자 저장
    unresolved = [asdict(p) for p in profiles if not p.resolved]
    if unresolved:
        uj, uc = save_unresolved(unresolved)
        print(f"  [미해결] JSON: {uj}")
        print(f"  [미해결] CSV:  {uc}")

    print(f"\n[Stage D 완료] 최종 사업자 레코드: {len(final_records)}개 "
          f"(사업자번호 유효: {len(records_by_biz)}, 미확인: {len(no_biz_records)})")

    return final_records


# ─── 연결 테스트 ─────────────────────────────────────────────────────────────

def run_connectivity_test(headless: bool = True, proxy: Optional[str] = None):
    """Coupang 접근 가능 여부 최소 테스트 (1페이지)"""
    print(f"\n{'='*60}")
    print(f"[연결 테스트] Coupang 접근 확인")
    print(f"{'='*60}")

    session = create_stealthy_session(headless=headless, proxy=proxy)
    try:
        with session:
            response = session.fetch(
                COUPANG_HOME,
                network_idle=True,
                google_search=False,
                page_action=warmup_page_action,
                timeout=30000,
            )
            status = getattr(response, 'status', 0)
            html = getattr(response, 'html_content', '') or ''
            print(f"  상태: {status}")
            print(f"  응답 크기: {len(html):,} bytes")

            if status == 200 and len(html) > 5000:
                print("  결과: ✅ 접근 성공")
                # 검색 페이지도 테스트
                time.sleep(random.uniform(3, 5))
                r2 = session.fetch(
                    "https://www.coupang.com/np/search?q=테스트&page=1",
                    network_idle=True,
                    google_search=False,
                    page_action=listing_page_action,
                    timeout=25000,
                )
                s2 = getattr(r2, 'status', 0)
                h2 = getattr(r2, 'html_content', '') or ''
                print(f"  검색 페이지: {s2}, {len(h2):,} bytes")
                if s2 == 200:
                    pids = re.findall(r'/vp/products/(\d+)', h2)
                    print(f"  상품 ID 추출: {len(set(pids))}개")
                    if pids:
                        print("  결과: ✅ 리스팅 파싱 가능")
                    return True
                else:
                    print("  결과: ⚠️ 검색 페이지 차단 (홈은 접근 가능)")
                    return True
            else:
                print("  결과: ❌ 접근 차단 (IP 차단 또는 Akamai 감지)")
                print("  조치: 15~30분 후 재시도, 또는 프록시 사용")
                return False
    except Exception as e:
        print(f"  오류: {e}")
        return False


# ─── 전체 파이프라인 ──────────────────────────────────────────────────────────

def run_pipeline(args):
    """전체 4단계 파이프라인 실행"""
    print(f"\n{'#'*60}")
    print(f"# Coupang 판매자 사업자정보 수집기")
    print(f"# Stage: {args.stage} | Pages: {args.pages} | Max Sellers: {args.max_sellers}")
    print(f"# Source: {args.source} | Headless: {args.headless}")
    print(f"# Delay Multiplier: {args.delay_multiplier}")
    if args.proxy:
        print(f"# Proxy: {args.proxy}")
    print(f"{'#'*60}")

    delay_mult = args.delay_multiplier
    products = []
    sellers = {}
    profiles = []

    # Stage A
    if args.stage in ("a", "all"):
        products = run_stage_a(
            pages=args.pages,
            source=args.source,
            category_id=args.category_id or "",
            keyword=args.keyword or "",
            headless=args.headless,
            proxy=args.proxy,
            delay_mult=delay_mult,
        )

    # Stage B
    if args.stage in ("b", "all"):
        if not products and args.stage == "b":
            # Stage B 단독 실행 시 상태에서 복원 시도
            print("  [경고] Stage A 결과 없음. 상태에서 복원 시도...")
            sellers_state = load_sellers_state()
            if sellers_state:
                # 최소한의 sellers dict 재구성
                for key, state in sellers_state.items():
                    sellers[key] = {
                        "store_name": state.get("store_name", ""),
                        "products": [],
                        "representative": None,
                        "power_seller": state.get("power_seller", False),
                        "seller_grade": "",
                    }
                print(f"  [복원] 판매자 {len(sellers)}명 로드")
        else:
            sellers = run_stage_b(products)

    # Stage C
    if args.stage in ("c", "all"):
        if not sellers and args.stage == "c":
            # Stage C 단독 실행 시 상태에서 복원
            print("  [경고] Stage B 결과 없음. 상태에서 복원 시도...")
            sellers_state = load_sellers_state()
            if sellers_state:
                for key, state in sellers_state.items():
                    rep_pid = state.get("representative_product_id", "")
                    rep = CoupangProduct(
                        product_id=rep_pid,
                        url=COUPANG_PRODUCT_URL.format(product_id=rep_pid) if rep_pid else "",
                    ) if rep_pid else None
                    sellers[key] = {
                        "store_name": state.get("store_name", ""),
                        "products": [],
                        "representative": rep,
                        "power_seller": state.get("power_seller", False),
                        "seller_grade": "",
                    }
                print(f"  [복원] 판매자 {len(sellers)}명 로드")

        if sellers:
            profiles = run_stage_c(
                sellers=sellers,
                max_sellers=args.max_sellers,
                headless=args.headless,
                proxy=args.proxy,
                delay_mult=delay_mult,
            )

    # Stage D
    if args.stage in ("d", "all"):
        if not profiles and args.stage == "d":
            # Stage D 단독 실행 시 상태에서 복원
            print("  [경고] Stage C 결과 없음. 상태에서 복원 시도...")
            sellers_state = load_sellers_state()
            for key, state in sellers_state.items():
                if state.get("resolved", False):
                    profile = SellerProfile(
                        seller_key=key,
                        store_name=state.get("store_name", ""),
                        company_name=state.get("company_name", ""),
                        ceo_name=state.get("ceo_name", ""),
                        phone=state.get("phone", ""),
                        business_number=state.get("business_number", ""),
                        address=state.get("address", ""),
                        power_seller=state.get("power_seller", False),
                        resolved=True,
                        parse_method=state.get("parse_method", ""),
                        resolved_at=state.get("resolved_at", ""),
                    )
                    profiles.append(profile)
            print(f"  [복원] 프로필 {len(profiles)}개 로드")

        if profiles:
            run_stage_d(profiles, sellers, source=args.source)

    print(f"\n{'#'*60}")
    print(f"# 파이프라인 완료")
    print(f"{'#'*60}")


# ─── CLI 엔트리포인트 ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Coupang 판매자 사업자정보 수집기 (4단계 파이프라인)"
    )
    parser.add_argument(
        "--stage", type=str, default="all",
        choices=["a", "b", "c", "d", "all"],
        help="실행할 단계 (a: 리스팅 스캔, b: 판매자 중복제거, c: 상세 수집, d: 사업자 중복제거+출력, all: 전체)"
    )
    parser.add_argument(
        "--pages", type=int, default=3,
        help="스캔할 리스팅 페이지 수 (기본: 3)"
    )
    parser.add_argument(
        "--max-sellers", type=int, default=50,
        help="Stage C에서 처리할 최대 판매자 수 (기본: 50)"
    )
    parser.add_argument(
        "--proxy", type=str, default=None,
        help="프록시 URL (선택, 예: http://127.0.0.1:8080)"
    )
    parser.add_argument(
        "--headless", action="store_true", default=True,
        help="헤드리스 모드 (기본: True)"
    )
    parser.add_argument(
        "--no-headless", action="store_true", default=False,
        help="브라우저 표시 모드"
    )
    parser.add_argument(
        "--delay-multiplier", type=float, default=1.0,
        help="딜레이 배율 (기본: 1.0, 1보다 크면 더 안전하게)"
    )
    parser.add_argument(
        "--source", type=str, default="category",
        choices=["omp", "category", "search"],
        help="리스팅 소스 타입 (기본: category)"
    )
    parser.add_argument(
        "--category-id", type=str, default="",
        help="카테고리 ID (category 소스 사용 시)"
    )
    parser.add_argument(
        "--keyword", type=str, default="",
        help="검색 키워드 (search 소스 사용 시)"
    )
    parser.add_argument(
        "--test", action="store_true", default=False,
        help="연결 테스트만 수행 (Coupang 접근 확인)"
    )

    args = parser.parse_args()

    # --no-headless 처리
    if args.no_headless:
        args.headless = False

    # --test 모드
    if args.test:
        success = run_connectivity_test(headless=args.headless, proxy=args.proxy)
        sys.exit(0 if success else 1)

    run_pipeline(args)


if __name__ == "__main__":
    main()
