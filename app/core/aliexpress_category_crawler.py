"""AliExpress 카테고리 탭 — Phase A(리스팅) + Phase B(판매자정보) 엔진 (Qt 비의존).

검증된 2단계 파이프라인 및 자가 개선 규율 적용:
- Phase A: 대상 카테고리 1~max_pages 페이지네이션 순회하여 상품 목록 확보
- Plan: 스마트 판매자 중복 제거 및 수집 계획 확정 (시드 캐시 활용)
- Phase B: Decodo 한국 주거용 고정 회선(Sticky 1440min Session) 기반 브라우저 세션 운용
  - 25건 단위 자동 회선 순환(Session Rotation) + WAF punish 감지 시 즉시 세션 교체
  - 비동기 mtop API 인터셉트를 통한 공정위 7대 사업자 정보 추출
  - 실시간 CSV/JSON 동시 저장 (중간 유실 0건)
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from patchright.async_api import async_playwright

from app.core import config, decodo
from app.core.aliexpress_resume_store import (
    STATUS_FINISHED,
    STATUS_LISTING_DONE,
    AliexpressResumeStore,
    ali_category_run_dir,
)
from app.core.base import CancelledError, Control
from app.models.records import CORE_CONTENT_FIELDS

LogFn = Callable[[str], None]
ProgressFn = Callable[[str, int, int], None]  # (label, current, total)
PhaseFn = Callable[[str, int, int], None]     # (phase_name, current_phase, total_phases)
CollectedFn = Callable[[dict], None]
StatsFn = Callable[[object], None]
ErrorFn = Callable[[str], None]


def _noop(*_a, **_k) -> None:
    pass


HOME_WARMUP_URL = "https://ko.aliexpress.com/?spm=a2g0o.home.logo.1.6c2f52d1NGQ4SZ"

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


@dataclass(frozen=True)
class AliexpressCategoryRunConfig:
    """AliExpress 카테고리 탭 실행 설정."""

    output_dir: Path
    category_name: str
    category_url: str
    max_pages: int = 30
    rotation_batch_size: int = 25
    delay: float = 2.0
    use_proxy: bool = True
    start_fresh: bool = False

    def __post_init__(self) -> None:
        if not self.category_name.strip():
            raise ValueError("카테고리 이름이 비어 있습니다.")
        if not self.category_url.strip():
            raise ValueError("카테고리 URL이 비어 있습니다.")
        if not 1 <= self.max_pages <= 100:
            raise ValueError("max_pages 는 1~100 사이여야 합니다.")


@dataclass
class AliexpressCrawlSummary:
    """수집 결과 요약 통계."""

    total_items: int = 0
    collected_items: int = 0
    unique_vendors: int = 0
    has_email: int = 0
    has_business_number: int = 0
    has_ceo_name: int = 0
    csv_file: Path | None = None
    json_file: Path | None = None
    elapsed_seconds: float = 0.0
    error: str | None = None


class AliexpressCategoryCrawler:
    """AliExpress 카테고리 2단계 수집 엔진."""

    def __init__(
        self,
        config: AliexpressCategoryRunConfig,
        control: Control | None = None,
        on_log: LogFn = _noop,
        on_phase: PhaseFn = _noop,
        on_progress: ProgressFn = _noop,
        on_collected: CollectedFn = _noop,
        on_stats: StatsFn = _noop,
        on_error: ErrorFn = _noop,
    ) -> None:
        self.config = config
        self.control = control
        self.on_log = on_log
        self.on_phase = on_phase
        self.on_progress = on_progress
        self.on_collected = on_collected
        self.on_stats = on_stats
        self.on_error = on_error

    def crawl(self) -> AliexpressCrawlSummary:
        """동기 인터페이스: 비동기 코루틴을 이벤트 루프에서 실행."""
        return asyncio.run(self._crawl_async())

    def _checkpoint(self) -> None:
        """일시정지/취소 체크포인트."""
        if self.control:
            self.control.checkpoint()

    async def _crawl_async(self) -> AliexpressCrawlSummary:
        start_time = time.time()
        safe_name = re.sub(r'[\\/*?:"<>| ]', "_", self.config.category_name)
        run_dir = ali_category_run_dir(
            self.config.output_dir, self.config.category_name, self.config.category_url
        )
        run_dir.mkdir(parents=True, exist_ok=True)

        csv_file = run_dir / f"ali_category_{safe_name}.csv"
        json_file = run_dir / f"ali_category_{safe_name}.json"

        summary = AliexpressCrawlSummary(csv_file=csv_file, json_file=json_file)

        store = AliexpressResumeStore(run_dir)
        if self.config.start_fresh:
            archived = store.archive()
            if archived:
                self.on_log(f"[아카이브] 이전 진행 기록 보관 완료: {archived.name}")

        store.open()
        try:
            mismatch = store.check_config(
                self.config.category_name, self.config.category_url, self.config.max_pages
            )
            if mismatch:
                self.on_log(f"[설정 안내] {mismatch}")

            if store.has_state() and not self.config.start_fresh:
                self.on_log(
                    f"[작업 이어하기] 이전 진행 기록 발견: 1~{store.last_completed_page}페이지 완료 "
                    f"(상품 {store.product_count:,}건 확보 / 판매자 {store.confirmed_seller_count:,}개사 수집 완료) "
                    f"→ {store.last_completed_page + 1}페이지부터 이어 수집합니다."
                )

            self.on_log(f"[Ali 카테고리] 수집 시작: '{self.config.category_name}' (최대 {self.config.max_pages}페이지)")
            self.on_log(f"[설정] 저장 폴더: {run_dir}")

            async with async_playwright() as p:
                session_counter = 0
                current_browser = None
                current_context = None
                current_page = None

                async def close_session() -> None:
                    nonlocal current_browser, current_context, current_page
                    if current_page:
                        try:
                            await current_page.close()
                        except Exception:
                            pass
                        current_page = None
                    if current_context:
                        try:
                            await current_context.close()
                        except Exception:
                            pass
                        current_context = None
                    if current_browser:
                        try:
                            await current_browser.close()
                        except Exception:
                            pass
                        current_browser = None

                async def open_new_session() -> None:
                    nonlocal session_counter, current_browser, current_context, current_page
                    await close_session()

                    session_counter += 1
                    sid = f"ali_cat_{int(time.time()) % 100000}_{session_counter}"
                    proxy = decodo.sticky_proxy_dict(session_id=sid) if self.config.use_proxy else None

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

                    current_browser = await p.chromium.launch(**launch_args)
                    current_context = await current_browser.new_context(
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                        locale="ko-KR",
                        viewport={"width": 1440, "height": 900}
                    )
                    await current_context.add_cookies([
                        {"name": "aep_usuc_f", "value": "region=KR&b_locale=ko_KR&site=kor&c_tp=KRW", "domain": ".aliexpress.com", "path": "/"},
                        {"name": "xman_us_f", "value": "x_locale=ko_KR&x_l=0", "domain": ".aliexpress.com", "path": "/"}
                    ])
                    current_page = await current_context.new_page()

                    # 홈 웜업
                    try:
                        await current_page.goto(HOME_WARMUP_URL, wait_until="domcontentloaded", timeout=35000)
                        await current_page.wait_for_timeout(2000)
                    except Exception:
                        pass

                # ── Phase 1: 카테고리 리스팅 순회 ────────────────────────────
                self.on_phase("Phase 1: 카테고리 목록 탐색", 1, 2)
                unique_products: dict[str, dict] = {}
                empty_streak = store.empty_streak

                if store.status == STATUS_LISTING_DONE:
                    for prod in store.load_products():
                        unique_products[prod["id"]] = prod
                    self.on_log(f"[Phase 1 건너뛰기] 이미 카테고리 목록 수집 완료됨 (총 {len(unique_products):,}개 상품 복원)")
                else:
                    if store.last_completed_page > 0:
                        for prod in store.load_products():
                            unique_products[prod["id"]] = prod
                        self.on_log(
                            f"  [목록 복원] 1~{store.last_completed_page}페이지 기확보 상품 {len(unique_products):,}개 복원"
                        )

                    start_page = store.last_completed_page + 1
                    if start_page <= self.config.max_pages:
                        if current_page is None:
                            await open_new_session()

                        for p_num in range(start_page, self.config.max_pages + 1):
                            self._checkpoint()
                            self.on_progress("목록 탐색", p_num, self.config.max_pages)

                            sep = "&" if "?" in self.config.category_url else "?"
                            p_url = f"{self.config.category_url}{sep}page={p_num}"
                            self.on_log(f"  [페이지 {p_num}/{self.config.max_pages}] 로드 중...")

                            try:
                                await current_page.goto(p_url, wait_until="domcontentloaded", timeout=35000)
                                await current_page.wait_for_timeout(1800)
                            except Exception as e:
                                self.on_log(f"    페이지 로드 지연 ({p_num}): {e}")
                                continue

                            await current_page.evaluate("window.scrollBy(0, 1500)")
                            await current_page.wait_for_timeout(800)
                            await current_page.evaluate("window.scrollBy(0, 1500)")
                            await current_page.wait_for_timeout(800)

                            items = await current_page.evaluate("""() => {
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

                            new_count = 0
                            for itm in items:
                                if itm["id"] not in unique_products:
                                    unique_products[itm["id"]] = itm
                                    new_count += 1

                            store.record_page(p_num, items)
                            self.on_log(f"    -> 발견 {len(items)}개 (신규 {new_count}개 / 누적 {len(unique_products)}개)")

                            if len(items) == 0 or new_count == 0:
                                empty_streak += 1
                                if empty_streak >= 2:
                                    self.on_log(f"    -> 연속 빈 페이지 감지: 목록 순회 종료 (총 {len(unique_products)}개 확보)")
                                    break
                            else:
                                empty_streak = 0

                            await current_page.wait_for_timeout(1000)

                        store.mark_listing_done(end_reason="empty" if empty_streak >= 2 else "max_pages")
                    else:
                        store.mark_listing_done(end_reason="max_pages")

                total_items = len(unique_products)
                summary.total_items = total_items
                product_targets = list(unique_products.values())

                if total_items == 0:
                    self.on_log("[오류] 상품 목록을 찾을 수 없습니다. 카테고리 URL을 확인하세요.")
                    await close_session()
                    return summary

                self.on_log(f"[Phase 1 완료] 총 {total_items:,}개 상품 목록 확보 완료.")

                # ── Phase 2: 판매자 상세 사업자 정보 수집 ─────────────────────
                self.on_phase("Phase 2: 판매자 상세 수집", 2, 2)
                self.on_log(f"[Phase 2 시작] {total_items:,}개 상품 대상 사업자정보(이메일, 대표자, 사업자번호 등) 수집...")

                collected_records: list[dict] = store.load_item_results()
                processed_items: set[str] = store.processed_item_ids()
                vendor_cache: dict[str, dict] = store.confirmed_sellers()
                req_count_in_session = 0

                # 기존 저장소에서 고유 판매자 시드 자동 로드
                for f_path in list(run_dir.glob("*.json")) + list(self.config.output_dir.glob("*.json")):
                    if f_path.name == json_file.name:
                        continue
                    try:
                        with open(f_path, "r", encoding="utf-8") as f:
                            prev_records = json.load(f)
                            if isinstance(prev_records, list):
                                for pr in prev_records:
                                    vid = pr.get("vendor_id")
                                    cname = pr.get("company_name")
                                    bnum = pr.get("business_number")
                                    if vid and cname and bnum and cname != "(상세 미기재)" and vid not in vendor_cache:
                                        vendor_cache[vid] = {
                                            "store_name": pr.get("store_name", ""),
                                            "company_name": cname,
                                            "ceo_name": pr.get("ceo_name", ""),
                                            "business_number": bnum,
                                            "phone": pr.get("phone", ""),
                                            "email": pr.get("email", ""),
                                            "address": pr.get("address", ""),
                                            "ecommerce_report_number": pr.get("ecommerce_report_number", ""),
                                        }
                    except Exception:
                        pass

                if collected_records:
                    self.on_log(
                        f"  [진행 복원] 기확보 상품 {len(collected_records):,}건, 판매자 {len(vendor_cache):,}개사 결과 복원 완료."
                    )
                    for r in collected_records:
                        self.on_collected(r)
                    summary.collected_items = len(collected_records)
                    summary.unique_vendors = len(vendor_cache)
                    summary.has_email = sum(1 for r in collected_records if r.get("email"))
                    summary.has_business_number = sum(1 for r in collected_records if r.get("business_number"))
                    summary.has_ceo_name = sum(1 for r in collected_records if r.get("ceo_name"))
                    self.on_stats(summary)
                elif vendor_cache:
                    self.on_log(f"  [시드 로드] 기존 검증된 판매자 {len(vendor_cache)}개사 정보 자동 탑재 완료.")

                # CSV 헤더 작성 및 복원 데이터 기록
                with open(csv_file, "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=COUPANG_DATASET_FIELDS)
                    writer.writeheader()
                    for r in collected_records:
                        writer.writerow(r)

                with open(json_file, "w", encoding="utf-8") as f:
                    json.dump(collected_records, f, ensure_ascii=False, indent=2)

                unprocessed = [itm for itm in product_targets if itm["id"] not in processed_items]
                if unprocessed and current_page is None:
                    await open_new_session()

                for idx, itm in enumerate(product_targets, 1):
                    self._checkpoint()
                    self.on_progress("상세 수집", idx, total_items)

                    p_id = itm["id"]
                    p_url = itm["url"]
                    p_title = itm["title"]

                    # 이미 수집 완료된 상품은 고속 스킵
                    if p_id in processed_items:
                        continue

                    # 25건 주기 회선 자동 순환 (프록시 사용 시에만)
                    if self.config.use_proxy and req_count_in_session >= self.config.rotation_batch_size:
                        self.on_log(f"  [회선 자동 순환] {req_count_in_session}건 도달 -> 새 주거용 회선 세션 교체...")
                        await open_new_session()
                        req_count_in_session = 0

                    record = None
                    clean_url = f"https://ko.aliexpress.com/item/{p_id}.html"

                    for attempt in range(1, 4):
                        self._checkpoint()
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

                        current_page.on("response", handle_response)

                        try:
                            await current_page.goto(clean_url, wait_until="domcontentloaded", timeout=25000)
                            for _ in range(35):
                                if pdp_received:
                                    break
                                await current_page.wait_for_timeout(100)
                        except Exception:
                            await current_page.wait_for_timeout(500)

                        current_page.remove_listener("response", handle_response)
                        req_count_in_session += 1

                        is_blocked = "punish" in current_page.url or "_____tmd_____" in current_page.url

                        if is_blocked:
                            if self.config.use_proxy:
                                self.on_log(f"  [{idx}/{total_items}] 보안 신호 감지 -> 새 회선 세션으로 자가 교체 (재시도 {attempt}/3)...")
                                await open_new_session()
                                req_count_in_session = 0
                            else:
                                self.on_log(f"  [{idx}/{total_items}] 보안 신호 감지 -> 안전 쿨다운 대기 후 재시도 (재시도 {attempt}/3)...")
                                await current_page.wait_for_timeout(int(self.config.delay * 1000) + 2000)
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

                        # 캐시 적중 확인
                        if vendor_id in vendor_cache:
                            c_info = vendor_cache[vendor_id]
                            record = {
                                "vendor_id": vendor_id,
                                "url": p_url,
                                "store_name": store_name or c_info.get("store_name", ""),
                                "company_name": c_info["company_name"],
                                "ceo_name": c_info["ceo_name"],
                                "business_number": c_info["business_number"],
                                "phone": c_info["phone"],
                                "email": c_info["email"],
                                "address": c_info["address"],
                                "ecommerce_report_number": c_info["ecommerce_report_number"],
                                "power_seller": "TRUE" if itm.get("is_top_seller") else "FALSE",
                                "power_seller_title": "AliExpress TOP셀러" if itm.get("is_top_seller") else "",
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

                        v_data = {
                            "store_name": store_name,
                            "company_name": company_name,
                            "ceo_name": ceo_name,
                            "business_number": business_number,
                            "phone": phone,
                            "email": email,
                            "address": address,
                            "ecommerce_report_number": ecommerce_report_number,
                        }
                        if company_name and company_name != "(상세 미기재)":
                            vendor_cache[vendor_id] = v_data
                        store.record_seller(vendor_id, v_data)

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
                            "power_seller": "TRUE" if itm.get("is_top_seller") else "FALSE",
                            "power_seller_title": "AliExpress TOP셀러" if itm.get("is_top_seller") else "",
                            "rating_count": itm.get("orders", ""),
                            "thumb_up_ratio": store_rating,
                            "product_title": p_title,
                            "price": itm.get("price", ""),
                            "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        }
                        break

                    if record:
                        collected_records.append(record)
                        processed_items.add(p_id)
                        store.record_item_result(p_id, record["vendor_id"], record)

                        # 실시간 CSV 및 JSON 저장
                        with open(csv_file, "a", encoding="utf-8-sig", newline="") as f:
                            writer = csv.DictWriter(f, fieldnames=COUPANG_DATASET_FIELDS)
                            writer.writerow(record)

                        with open(json_file, "w", encoding="utf-8") as f:
                            json.dump(collected_records, f, ensure_ascii=False, indent=2)

                        # 콜백 및 통계
                        self.on_collected(record)

                        summary.collected_items = len(collected_records)
                        summary.unique_vendors = len(vendor_cache)
                        summary.has_email = sum(1 for r in collected_records if r.get("email"))
                        summary.has_business_number = sum(1 for r in collected_records if r.get("business_number"))
                        summary.has_ceo_name = sum(1 for r in collected_records if r.get("ceo_name"))
                        self.on_stats(summary)

                        biz_desc = f"[{record['company_name'] or record['store_name']}] 대표: {record['ceo_name'] or '-'} | 사업자: {record['business_number'] or '-'} | 이메일: {record['email'] or '-'}"
                        self.on_log(f"  [{idx}/{total_items}] 수집: {biz_desc} (누적 {len(collected_records)}건, 고유판매자 {len(vendor_cache)}개사)")

                    await current_page.wait_for_timeout(int(self.config.delay * 1000))

                store.mark_finished()
                await close_session()

        finally:
            store.close()

        summary.elapsed_seconds = round(time.time() - start_time, 1)
        self.on_log(f"\n[Ali 카테고리 완료] 총 {summary.collected_items:,}건 수집 완료 (고유 판매자 {summary.unique_vendors}개사, 소요 {summary.elapsed_seconds}초)")
        self.on_log(f"  -> 이메일 확보: {summary.has_email:,}건 | 사업자번호: {summary.has_business_number:,}건")
        self.on_log(f"  -> 파일: {csv_file}")
        return summary
