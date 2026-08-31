"""Coupang /np/omp v3 엔진 — Qt 비의존 코어 (WORK_ORDER §7).

v3 CLI의 검증된 3-API 파이프라인을 callback/Control/browser-factory 주입 가능한
형태로 추출한다. 네트워크, 파일 저장, 브라우저 생성은 모두 이 계층에서 일어나며
UI 스레드와 무관하다.
"""

from __future__ import annotations

import json
import logging
import random
import time
import traceback
from collections.abc import Callable
from typing import Any

from app.core.base import CancelledError, Control
from app.core.config import DEFAULT_COUPANG_PROFILE_DIR
from app.core.storage import acquire_output_lock
from app.models.coupang_records import (
    CoupangRecord,
    CoupangRunConfig,
    CoupangRunSummary,
    CoupangStatsSnapshot,
)

logger = logging.getLogger(__name__)

OMP_URL = "https://www.coupang.com/np/omp"
COUPANG_HOME = "https://www.coupang.com/"
SHOP_SESSION_URL = "https://shop.coupang.com/A00067881"

PHASES = ("warmup", "capture_template", "feed_pagination", "vendor_mapping", "business_info", "save")


def _safe_callback(cb: Callable | None, name: str, on_log: Callable | None, *args: Any) -> None:
    if cb is None:
        return
    try:
        cb(*args)
    except Exception as e:  # noqa: BLE001 - 사용자 callback 경계에서 격리
        msg = f"  [callback 오류] {name}: {e}"
        if on_log is not None:
            try:
                on_log(msg)
                return
            except Exception as log_error:  # noqa: BLE001 - fallback logger가 최종 경계
                logger.warning("%s; on_log callback도 실패: %s", msg, log_error)
                return
        logger.warning(msg)


class CoupangCrawler:
    """v3 3-API 파이프라인 엔진."""

    def __init__(
        self,
        config: CoupangRunConfig,
        control: Control,
        browser_factory: Callable | None = None,
        on_phase: Callable[[str, int, int], None] | None = None,
        on_progress: Callable[[str, int, int], None] | None = None,
        on_log: Callable[[str], None] | None = None,
        on_record: Callable[[dict], None] | None = None,
        on_stats: Callable[[CoupangStatsSnapshot], None] | None = None,
    ) -> None:
        self.config = config
        self.control = control
        self._browser_factory = browser_factory
        self._on_phase = on_phase
        self._on_progress = on_progress
        self._on_log = on_log
        self._on_record = on_record
        self._on_stats = on_stats

    def _log(self, msg: str) -> None:
        if self._on_log is None:
            return
        try:
            self._on_log(msg)
        except Exception as e:  # noqa: BLE001 - 로그 callback 실패는 수집을 중단하지 않음
            logger.warning("  [callback 오류] on_log: %s", e)

    def _phase(self, name: str) -> None:
        idx = PHASES.index(name) + 1
        _safe_callback(self._on_phase, "on_phase", self._on_log, name, idx, len(PHASES))

    def _progress(self, kind: str, current: int, total: int) -> None:
        _safe_callback(self._on_progress, "on_progress", self._on_log, kind, current, total)

    def _emit_stats(self, summary: CoupangRunSummary) -> None:
        """Send a frozen numeric-only snapshot to avoid Qt queued-signal aliasing."""
        if self._on_stats is None:
            return
        snapshot = CoupangStatsSnapshot.from_summary(summary)
        _safe_callback(self._on_stats, "on_stats", self._on_log, snapshot)

    def run(self) -> CoupangRunSummary:
        summary = CoupangRunSummary()
        browser = None
        cm = None  # Camoufox context manager (real browser only)
        try:
            self.config.output_dir.mkdir(parents=True, exist_ok=True)
            acquire_output_lock(self.config.output_dir)
            browser, cm = self._create_browser()
            page = browser.new_page()
            self._attach_http_status_logger(page)
            self._run_pipeline(page, summary)
        except CancelledError:
            summary.cancelled = True
            summary.termination_reason = "cancelled"
            self._log("취소됨 — 부분 결과를 저장합니다.")
            self._save_results(summary, partial=True)
        except _RunError as e:
            summary.error = str(e)
            summary.termination_reason = e.reason
            self._log(f"오류: {e}")
            if e.reason in ("no_items", "template_not_captured"):
                self._save_results(summary, partial=False)
            elif summary.records:
                self._save_results(summary, partial=True)
        except Exception as e:  # noqa: BLE001 - 엔진 최상위 실패를 summary로 구조화
            summary.error = f"{type(e).__name__}: {e}"
            summary.termination_reason = "error"
            self._log(f"예상치 못한 오류 ({type(e).__name__}): {e}")
            self._log(traceback.format_exc())
            if summary.records:
                self._save_results(summary, partial=True)
        finally:
            cleanup_failed = False
            if cm is not None:
                try:
                    cm.__exit__(None, None, None)
                except Exception as e:  # noqa: BLE001 - 외부 브라우저 cleanup 경계
                    self._log(f"  브라우저 종료 실패, 재시도: {e}")
                    if browser is None:
                        summary.cleanup_error = str(e)
                        cleanup_failed = True
                    else:
                        try:
                            browser.close()
                        except Exception as e2:  # noqa: BLE001 - cleanup fallback 경계
                            summary.cleanup_error = str(e2)
                            cleanup_failed = True
            elif browser is not None:
                try:
                    browser.close()
                except Exception as e:  # noqa: BLE001 - 외부 브라우저 cleanup 경계
                    summary.cleanup_error = str(e)
                    cleanup_failed = True
            if cleanup_failed:
                self._log(f"  [cleanup 실패] 잔류 프로세스 가능성: {summary.cleanup_error}")
        self._emit_stats(summary)
        return summary

    def _create_browser(self):
        """Returns (browser, context_manager). cm is None for injected factories.

        영속 프로필(persistent_context)로 쿠키·방문 이력을 실행 간 누적해 세션
        신뢰를 축적한다(SEARCH_POC_FINDINGS 가설 A — 매 실행 신규 세션 + 즉시
        카테고리 진입 패턴은 Akamai 점수에 불리). 프로필 락·손상 등으로 기동이
        실패하면 기존의 신규 세션 방식으로 폴백한다 — 회귀 없음.
        """
        if self._browser_factory is not None:
            return self._browser_factory(), None
        try:
            from camoufox.sync_api import Camoufox
        except ImportError as e:
            raise _RunError(
                "Camoufox 패키지가 설치되지 않았습니다. "
                "명령 프롬프트에서 SellerCollector.exe --setup-runtime 을 먼저 실행하세요.",
                reason="error",
            ) from e
        if self.config.use_persistent_profile:
            profile_dir = self.config.profile_dir or DEFAULT_COUPANG_PROFILE_DIR
            try:
                cm = Camoufox(
                    headless=False, geoip=True, locale="ko-KR", humanize=True,
                    persistent_context=True, user_data_dir=str(profile_dir),
                )
                browser = cm.__enter__()
            except Exception as e:  # noqa: BLE001 - 프로필 락 등 기동 실패 → 폴백
                self._log(f"  영속 프로필 기동 실패({type(e).__name__}: {e}) — 신규 세션으로 진행")
            else:
                self._log(f"  영속 프로필 사용: {profile_dir}")
                return browser, cm
        cm = Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True)
        browser = cm.__enter__()
        return browser, cm

    def _attach_http_status_logger(self, page) -> None:
        """문서(document) 탐색의 4xx/5xx 응답 상태를 로그에 남긴다 (차단 진단용).

        진입 차단(403 등)은 문서 탐색 응답 상태에 직접 드러난다. 리소스/XHR
        요청은 제외 — 해당 URL 의 상태만으로 IP 차단 여부를 판단할 수 있게
        하는 것이 목적이다(2026-08-30 차단 사례: 상태 코드가 로그에 없어
        원인 분류가 어려웠음). 진단 기능이므로 개별 실패는 무시한다.
        """

        def _on_response(response) -> None:
            try:
                if response.request.resource_type != "document":
                    return
                status = response.status
                if status >= 400:
                    self._log(f"  [HTTP {status}] {response.url[:160]}")
            except Exception:  # noqa: BLE001 - 진단 로그 경계 격리
                pass

        try:
            page.on("response", _on_response)
        except Exception as e:  # noqa: BLE001 - 진단 로거 부착 실패는 무시
            self._log(f"  HTTP 상태 로거 부착 실패: {e}")

    @property
    def _wait_scale(self) -> float:
        return min(1.0, self.config.warmup_time / 20.0) if self.config.warmup_time > 0 else 0.0

    def _run_pipeline(self, page, summary: CoupangRunSummary) -> None:
        # Phase 1: Warmup
        self._phase("warmup")
        self._log("Phase 1: 웜업 시작...")
        page.goto(COUPANG_HOME, wait_until="domcontentloaded", timeout=30000)
        self.control.sleep(2 * self._wait_scale)
        self._natural_interaction(page, self.config.warmup_time)
        self.control.checkpoint()
        self._log("  Akamai 검증 완료.")

        # Phase 2: Capture request template
        self._phase("capture_template")
        self._log("Phase 2: /np/omp 로드 및 요청 템플릿 캡처...")
        request_template = [None]

        def on_request(request):
            if "getPromotion" in request.url and request_template[0] is None:
                try:
                    request_template[0] = request.post_data
                except Exception as e:  # noqa: BLE001 - Playwright request 객체 경계
                    self._log(f"  getPromotion 요청 본문을 읽지 못했습니다: {type(e).__name__}: {e}")

        page.on("request", on_request)
        page.goto(OMP_URL, wait_until="domcontentloaded", timeout=30000)
        self.control.sleep(4 * self._wait_scale)
        self._natural_interaction(page, 8 * self._wait_scale)
        self.control.checkpoint()

        if not request_template[0]:
            raise _RunError(
                "Coupang 피드 요청을 확인하지 못했습니다. "
                "브라우저/네트워크 상태를 확인한 뒤 다시 실행하세요.",
                reason="template_not_captured",
            )

        template = json.loads(request_template[0])
        self._log("  템플릿 캡처 성공.")

        # Phase 3: Feed pagination
        self._phase("feed_pagination")
        self._log("Phase 3: getPromotion 페이지네이션...")
        all_items = self._paginate_feed(page, template, summary)
        self.control.checkpoint()

        viids = list(all_items.keys())
        summary.products_seen = len(all_items)
        self._emit_stats(summary)
        self._log(f"  고유 vendorItemId: {len(viids)}개")

        if not viids:
            raise _RunError("수집된 상품이 없습니다.", reason="no_items")

        # Phase 4: Vendor mapping
        self._phase("vendor_mapping")
        self._log("Phase 4: vendor 매핑...")
        all_vendors = self._map_vendors(page, viids, summary)
        self.control.checkpoint()

        unique_vendor_ids = sorted(all_vendors.keys())
        summary.unique_vendors = len(unique_vendor_ids)
        self._emit_stats(summary)
        self._log(f"  고유 판매자: {len(unique_vendor_ids)}명")

        # Phase 5: Business info
        self._phase("business_info")
        self._log(f"Phase 5: 사업자정보 수집 ({len(unique_vendor_ids)}명)...")
        self._fetch_business_info(page, unique_vendor_ids, all_vendors, summary)
        self.control.checkpoint()

        # Phase 6: Save
        self._phase("save")
        self._log("Phase 6: 결과 저장...")
        self._save_results(summary, partial=bool(summary.error))

    def _natural_interaction(self, page, duration: float) -> None:
        end_time = time.monotonic() + duration
        while time.monotonic() < end_time:
            self.control.checkpoint()
            action = random.choice(["move", "scroll", "pause", "move", "scroll"])
            if action == "move":
                page.mouse.move(random.randint(100, 900), random.randint(100, 600))
                self.control.sleep(random.uniform(0.1, 0.4))
            elif action == "scroll":
                page.mouse.wheel(0, random.randint(50, 300))
                self.control.sleep(random.uniform(0.3, 0.8))
            else:
                self.control.sleep(random.uniform(0.5, 1.5))

    def _paginate_feed(self, page, template: dict, summary: CoupangRunSummary) -> dict:
        all_items: dict[str, dict] = {}
        token = template.get("query", {}).get("continuationToken", "seemore=CGs=")
        feed_errors = 0

        for page_num in range(self.config.max_scroll_pages):
            self.control.checkpoint()
            items, next_token, had_error = self._fetch_promotion_page(page, template, token)

            if had_error:
                feed_errors += 1
                summary.request_errors += 1
                if not all_items:
                    raise _RunError(
                        f"getPromotion API 실패 (page {page_num + 1}). "
                        "네트워크/차단 상태를 확인하세요.",
                        reason="error",
                    )
                self._log(f"  Page {page_num + 1}: API 오류 — 부분 데이터로 계속")
                summary.error = (
                    f"getPromotion API 오류 (page {page_num + 1}) — "
                    "부분 데이터만 수집됨"
                )
                summary.termination_reason = "error"
                break

            new_count = 0
            for item in items:
                viid = item.get("vendorItemId")
                if viid and viid not in all_items:
                    all_items[viid] = {
                        "itemId": item.get("itemId"),
                        "title": item.get("title", ""),
                        "categoryId": item.get("categoryId"),
                    }
                    new_count += 1

            self._progress("feed", page_num + 1, self.config.max_scroll_pages)
            self._log(f"  Page {page_num + 1}: {len(items)}건, 신규 {new_count}, 누적 {len(all_items)}")

            if not items or new_count == 0 or not next_token or next_token == token:
                if not items or new_count == 0:
                    summary.termination_reason = "no_new_items"
                elif not next_token or next_token == token:
                    summary.termination_reason = "token_exhausted"
                else:
                    summary.termination_reason = "page_limit"
                break

            token = next_token
            self.control.sleep(random.uniform(1.0, 2.0) * self._wait_scale)
        else:
            summary.termination_reason = "page_limit"

        return all_items

    def _fetch_promotion_page(self, page, template: dict, token: str) -> tuple[list, str | None, bool]:
        """Returns (items, next_token, had_error)."""
        template["query"]["continuationToken"] = token
        template["query"]["nextPageKey"] = token
        body_json = json.dumps(template)

        result = page.evaluate("""async (bodyStr) => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 30000);
            try {
                const r = await fetch("https://www.coupang.com/np/omp/api/getPromotion", {
                    method: "POST",
                    credentials: "include",
                    headers: {"Content-Type": "application/json"},
                    body: bodyStr,
                    signal: controller.signal
                });
                const text = await r.text();
                return {status: r.status, body: text};
            } catch(e) {
                return {error: e.name === "AbortError" ? "요청 시간 초과(30초)" : e.message};
            } finally { clearTimeout(timer); }
        }""", body_json)

        if result.get("error"):
            self._log(f"  getPromotion 네트워크 오류: {result['error']}")
            return [], None, True

        status_code = result.get("status", 0)
        if status_code != 200:
            self._log(f"  getPromotion HTTP {status_code}")
            return [], None, True

        body = result.get("body", "")
        if not body or body.strip().startswith("<!"):
            self._log("  getPromotion: HTML 응답 (차단 가능성)")
            return [], None, True

        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self._log("  getPromotion: JSON 파싱 실패")
            return [], None, True

        if str(data.get("ret")) == "0":
            d = data.get("data", {})
            return d.get("promotionData", []) or [], d.get("token"), False

        self._log(f"  getPromotion: 비정상 ret={data.get('ret')}")
        return [], None, True

    def _map_vendors(self, page, viids: list[str], summary: CoupangRunSummary) -> dict:
        page.goto(SHOP_SESSION_URL, wait_until="domcontentloaded", timeout=30000)
        self.control.sleep(3 * self._wait_scale)
        self._natural_interaction(page, 5 * self._wait_scale)
        self.control.checkpoint()

        all_vendors: dict[str, dict] = {}
        batch_size = self.config.batch_size
        total_batches = (len(viids) + batch_size - 1) // batch_size
        batch_errors = 0

        for batch_idx in range(total_batches):
            self.control.checkpoint()
            start = batch_idx * batch_size
            batch = viids[start:start + batch_size]
            vendors, had_error = self._get_vendors_for_items(page, batch)
            if had_error:
                batch_errors += 1
                summary.request_errors += 1
            all_vendors.update(vendors)
            self._progress("vendor_mapping", batch_idx + 1, total_batches)
            self._log(f"  Batch {batch_idx + 1}/{total_batches}: {len(batch)}건 → {len(vendors)}명")
            self.control.sleep(random.uniform(1.0, 2.0) * self._wait_scale)

        if not all_vendors and batch_errors > 0:
            raise _RunError(
                f"individualInfo API 전량 실패 ({batch_errors}/{total_batches} 배치 오류). "
                "세션/네트워크 상태를 확인하세요.",
                reason="error",
            )

        return all_vendors

    def _get_vendors_for_items(self, page, vendor_item_ids: list[str]) -> tuple[dict, bool]:
        """Returns (vendors_dict, had_error)."""
        result = page.evaluate("""async (args) => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 30000);
            try {
                const r = await fetch("https://shop.coupang.com/api/v2/store/individualInfo/products", {
                    method: "POST",
                    credentials: "include",
                    headers: {"Content-Type": "application/json"},
                    signal: controller.signal,
                    body: JSON.stringify({
                        vendorItemIds: args.viids,
                        isVIBased: true,
                        storeId: args.storeId,
                        vendorId: args.vendorId,
                        ignoreAdultCheck: false,
                        pageType: 3
                    })
                });
                const text = await r.text();
                return {status: r.status, body: text};
            } catch(e) {
                return {error: e.name === "AbortError" ? "요청 시간 초과(30초)" : e.message};
            } finally { clearTimeout(timer); }
        }""", {"viids": vendor_item_ids, "storeId": 109671, "vendorId": "A00067881"})

        if result.get("error"):
            self._log(f"  individualInfo 오류: {result['error']}")
            return {}, True

        body = result.get("body", "")
        if result.get("status") != 200 or not body or body.strip().startswith("<!"):
            blocked = " (HTML 응답 — 차단/세션 만료 가능성)" if body.strip().startswith("<!") else ""
            self._log(f"  individualInfo 실패: HTTP {result.get('status')}{blocked}")
            return {}, True

        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self._log(f"  individualInfo 실패: 응답 JSON 파싱 불가 (HTTP {result.get('status')})")
            return {}, True

        if data.get("code") != 200 or not data.get("data", {}).get("products"):
            self._log(f"  individualInfo 실패: 응답 code={data.get('code')}, products 없음")
            return {}, True

        vendors = {}
        for product in data["data"]["products"]:
            store_info = product.get("storeInfoArea", {})
            vid = store_info.get("vendorId")
            if vid:
                vendors[vid] = {
                    "vendorId": vid,
                    "storeId": store_info.get("storeId"),
                    "displayName": store_info.get("displayName"),
                    "productId": product.get("productId"),
                    "itemId": product.get("itemId"),
                    "vendorItemId": product.get("vendorItemId"),
                }
        return vendors, False

    def _fetch_business_info(
        self, page, vendor_ids: list[str], all_vendors: dict, summary: CoupangRunSummary
    ) -> None:
        total = len(vendor_ids)
        for i, vendor_id in enumerate(vendor_ids):
            self.control.checkpoint()
            data = self._get_store_review(page, vendor_id)

            if data and data.get("name"):
                vinfo = all_vendors.get(vendor_id, {})
                record = self._build_record(vendor_id, data, vinfo)
                summary.records.append(record.to_dict())
                summary.business_info_success += 1
                if record.power_seller:
                    summary.power_sellers += 1
                if record.store_name:
                    summary.store_name_present += 1
                else:
                    summary.store_name_missing += 1
                _safe_callback(self._on_record, "on_record", self._on_log, record.to_dict())
                power = " [POWER]" if record.power_seller else ""
                self._log(f"  [{i + 1:3d}/{total}] {vendor_id}: {record.company_name}{power}")
            elif data:
                summary.brand_seller_skipped += 1
                self._log(f"  [{i + 1:3d}/{total}] {vendor_id}: (brand seller - 스킵)")
            else:
                summary.request_errors += 1
                self._log(f"  [{i + 1:3d}/{total}] {vendor_id}: (오류)")

            self._progress("business_info", i + 1, total)
            self._emit_stats(summary)
            self.control.sleep(random.uniform(self.config.delay_min, self.config.delay_max))

        summary.duplicate_business_numbers_observed = self._count_duplicate_business_numbers(
            summary.records
        )

    def _get_store_review(self, page, vendor_id: str) -> dict | None:
        result = page.evaluate("""async (vid) => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 30000);
            try {
                const params = new URLSearchParams({vendorId: vid, urlName: vid});
                const r = await fetch('https://shop.coupang.com/api/v1/store/getStoreReview?' + params.toString(), {credentials: 'include', signal: controller.signal});
                const text = await r.text();
                return {status: r.status, body: text};
            } catch(e) {
                return {error: e.name === "AbortError" ? "요청 시간 초과(30초)" : e.message};
            } finally { clearTimeout(timer); }
        }""", vendor_id)

        if result.get("error"):
            self._log(f"  getStoreReview({vendor_id}) 네트워크 오류: {result['error']}")
            return None

        body = result.get("body", "")
        if result.get("status") != 200 or not body or body.strip().startswith("<!"):
            blocked = " (HTML 응답 — 차단/세션 만료 가능성)" if body.strip().startswith("<!") else ""
            self._log(f"  getStoreReview({vendor_id}) 실패: HTTP {result.get('status')}{blocked}")
            return None

        try:
            return json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self._log(f"  getStoreReview({vendor_id}) 실패: 응답 JSON 파싱 불가")
            return None

    def _build_record(self, vendor_id: str, data: dict, vinfo: dict) -> CoupangRecord:
        pid = vinfo.get("productId")
        iid = vinfo.get("itemId")
        viid = vinfo.get("vendorItemId")
        url = (
            f"https://www.coupang.com/vp/products/{pid}?itemId={iid}&vendorItemId={viid}"
            if pid and iid and viid
            else ""
        )
        badge = data.get("qualitySellerBadgeDto")
        return CoupangRecord(
            vendor_id=vendor_id,
            url=url,
            store_name=vinfo.get("displayName", ""),
            company_name=data.get("name", ""),
            ceo_name=data.get("repPersonName", ""),
            business_number=data.get("businessNumber", ""),
            phone=data.get("repPhoneNum", ""),
            email=data.get("repEmail", ""),
            address=f"{data.get('repAddr1', '')} {data.get('repAddr2', '')}".strip(),
            ecommerce_report_number=data.get("eCommerceReportNumber", ""),
            power_seller=bool(badge),
            power_seller_title=(badge or {}).get("qualityTitle", ""),
            rating_count=data.get("ratingCount", 0),
            thumb_up_ratio=data.get("thumbUpRatio", 0),
        )

    @staticmethod
    def _count_duplicate_business_numbers(records: list[dict]) -> int:
        import re

        bn_to_vendors: dict[str, set[str]] = {}
        for rec in records:
            raw = rec.get("business_number", "")
            digits = re.sub(r"\D", "", raw)
            if len(digits) != 10:
                continue
            vid = rec.get("vendor_id", "")
            bn_to_vendors.setdefault(digits, set()).add(vid)

        return sum(1 for vendors in bn_to_vendors.values() if len(vendors) >= 2)

    def _save_results(self, summary: CoupangRunSummary, partial: bool) -> None:
        from app.core.coupang.exporter import CoupangExporter, CsvWriteError

        exporter = CoupangExporter(self.config)
        try:
            json_path, csv_path = exporter.save(summary.records, partial=partial)
            summary.json_path = json_path
            summary.csv_path = csv_path
            label = "부분 저장" if partial else "저장"
            self._log(f"  {label}: {json_path}")
        except CsvWriteError as e:
            summary.json_path = e.json_path
            summary.save_error = str(e)
            self._log(f"  저장 실패: {e} (JSON 저장됨: {e.json_path})")
        except Exception as e:  # noqa: BLE001 - 모든 exporter 실패를 구조화
            summary.save_error = f"저장 실패: {type(e).__name__}: {e}"
            self._log(f"  저장 실패: {type(e).__name__}: {e}")


class _RunError(Exception):
    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason
