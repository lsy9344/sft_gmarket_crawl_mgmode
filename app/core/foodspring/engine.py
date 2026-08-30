"""Foodspring(식봄) 전국 택배 배송 기획전 수집 엔진 — Qt 비의존 코어.

대상: https://www.foodspring.co.kr/special/wcpd

수집 흐름:
  1. session : Scrapling StealthyFetcher 로 게스트 세션 쿠키(FS_TOKEN 등) 확보
  2. products: GraphQL API(api.foodspring.co.kr/v2/graphql) cursor 페이지네이션
               전체 상품 수집 (10,000개, 중복 제거)
  3. sellers : 고유 셀러별 대표 상품의 상세 HTML(__NEXT_DATA__ 의 vendor_{id})에서
               사업자등록번호/연락처/소재지/통신판매신고번호/고객센터전화 추출
  4. save    : Excel(상품목록 시트 + 셀러정보 시트) 원자적 저장

참고: /seller/* 페이지는 AWS WAF 챌린지로 차단되지만, 상품 상세 페이지는
차단 없이 동일한 판매자 사업자정보를 서버렌더링하므로 이를 사용한다.

취소/오류 정책:
- 취소(CancelledError) 시에도 지금까지 수집한 상품·셀러를 부분 엑셀로 저장한다.
- 목록 연속 오류/판매자 다수 오류는 summary.error + termination_reason 로
  '완료'가 아닌 실패로 판정하되, 수집된 분량은 부분 저장한다.
- 엑셀 저장 자체 실패는 summary.save_error 로 별도 매핑(SAVE_ERROR).
"""

from __future__ import annotations

import json
import random
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from app.core.base import CancelledError, Control
from app.core.foodspring.exporter import FoodSpringExporter
from app.models.foodspring_records import (
    FoodSpringRunConfig,
    FoodSpringRunSummary,
    FoodSpringSellerRecord,
)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
GRAPHQL_URL = "https://api.foodspring.co.kr/v2/graphql"
LIST_URL = "https://www.foodspring.co.kr/special/wcpd"
DETAIL_URL = "https://www.foodspring.co.kr/goods/detail/{pid}"

PHASES = ("session", "products", "sellers", "save")
QUERY_RESOURCE = "goods_list_query.graphql"

# 목록 페이지 연속 실패 시 조기 종료 임계값
MAX_CONSECUTIVE_LIST_ERRORS = 5
# 판매자 요청 연속 실패 시 조기 종료 임계값
MAX_CONSECUTIVE_SELLER_ERRORS = 10
# 판매자 요청 실패율 상한 (초과 시 실패로 판정)
MAX_SELLER_ERROR_RATIO = 0.3

# 목록 종료 사유
LIST_END_NATURAL = "natural_end"      # hasNextPage=false 또는 신규 0 반복(카탈로그 끝)
LIST_END_PAGE_LIMIT = "page_limit"    # max_pages 도달
LIST_END_ERRORS = "errors"            # 연속 오류로 중단

# 셀러 사업자정보(이메일/대표자 포함) 단건 조회 쿼리
# 셀러 페이지 '배송 정보 및 판매자 정보 더보기' 모달과 동일한 node(id) 조회.
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


def _noop(*_a, **_k) -> None:  # pragma: no cover
    pass


class FoodSpringCrawler:
    def __init__(
        self,
        config: FoodSpringRunConfig,
        control: Control,
        fetcher: Callable | None = None,
        exporter: FoodSpringExporter | None = None,
        on_phase: Callable[[str, int, int], None] | None = None,
        on_progress: Callable[[str, int, int], None] | None = None,
        on_log: Callable[[str], None] | None = None,
        on_record: Callable[[dict], None] | None = None,
        on_stats: Callable[[FoodSpringRunSummary], None] | None = None,
    ) -> None:
        self.config = config
        self.control = control
        self._fetcher = fetcher
        self._exporter = exporter or FoodSpringExporter()
        self.on_phase = on_phase or _noop
        self.on_progress = on_progress or _noop
        self.on_log = on_log or _noop
        self.on_record = on_record or _noop
        self.on_stats = on_stats or _noop
        # 목록(GraphQL)용 세션: 단일 스레드에서만 사용
        self._session = requests.Session()
        self._session.headers["User-Agent"] = UA
        # 판매자 병렬 fetch용 스레드 로컬 세션 (스레드별 독립)
        self._session_local = threading.local()
        self._seller_request_errors = 0
        self._seller_parse_errors = 0
        self._error_lock = threading.Lock()
        self._partial_products: dict = {}
        self._partial_infos: dict = {}
        self._retry_delay = 3.0

    # ── 유틸 ──────────────────────────────────────────────────────
    @staticmethod
    def _load_query() -> str:
        from importlib import resources

        try:
            return resources.files("app.core.foodspring").joinpath(QUERY_RESOURCE).read_text(
                encoding="utf-8"
            )
        except Exception:  # noqa: BLE001
            try:
                import os

                here = Path(os.path.dirname(os.path.abspath(__file__)))
                return (here / QUERY_RESOURCE).read_text(encoding="utf-8")
            except OSError as exc:
                raise RuntimeError(
                    f"GraphQL 쿼리 파일({QUERY_RESOURCE})을 찾을 수 없습니다."
                ) from exc

    def _cookie_header(self, cookies: tuple | list) -> str:
        parts = []
        for c in cookies:
            name = c.get("name")
            value = c.get("value")
            if name and value is not None:
                parts.append(f"{name}={value}")
        return "; ".join(parts)

    def _emit_phase(self, name: str, current: int, total: int) -> None:
        self.on_phase(name, current, total)
        self.on_log(f"[단계 {current}/{total}] {name}")

    def _worker_session(self) -> requests.Session:
        """스레드별 독립 세션 — 요청 직전 헤더(쿠키 포함) 복사."""
        s = getattr(self._session_local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update(self._session.headers)
            self._session_local.session = s
        return s

    # ── 1단계: 세션 ───────────────────────────────────────────────
    def _establish_session(self) -> str:
        if self._fetcher is not None:
            page = self._fetcher(LIST_URL)
        else:
            from scrapling.fetchers import StealthyFetcher

            page = StealthyFetcher.fetch(LIST_URL, network_idle=True, timeout=90000)

        cookies = getattr(page, "cookies", None)
        if not cookies:
            raise RuntimeError(
                "foodspring 사이트에서 세션 쿠키를 받지 못했습니다. "
                "잠시 후 다시 시도하세요."
            )
        header = self._cookie_header(cookies)
        if "FS_TOKEN" not in header:
            raise RuntimeError("게스트 세션(FS_TOKEN)을 확보하지 못했습니다.")
        self._session.headers["Cookie"] = header
        self._session.headers["Origin"] = "https://www.foodspring.co.kr"
        self._session.headers["Referer"] = LIST_URL
        return header

    # ── 2단계: 상품 목록 ──────────────────────────────────────────
    def _graphql_goods_page(self, query: str, after: str | None, first: int = 80) -> tuple:
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
        resp = self._session.post(
            GRAPHQL_URL,
            data=body.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=40,
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data and not data.get("data"):
            raise RuntimeError(
                "GraphQL 오류: "
                + json.dumps(data["errors"], ensure_ascii=False)[:400]
            )
        gl = (data.get("data") or {}).get("goodsList") or {}
        return gl.get("edges", []), gl.get("pageInfo", {}) or {}

    def _crawl_products(self, query: str) -> tuple[dict, str]:
        """전체 상품 수집. 반환: (products, 종료사유). 취소 시 부분분을 보존 후 예외."""
        products: dict = {}
        after: str | None = None
        page_no = 0
        limit = self.config.limit_products
        consecutive_errors = 0
        zero_new_pages = 0
        end_reason = LIST_END_NATURAL
        try:
            while page_no < self.config.max_pages:
                page_no += 1
                self.control.checkpoint()
                try:
                    edges, pi = self._graphql_goods_page(query, after)
                except Exception as e:  # noqa: BLE001
                    consecutive_errors += 1
                    self.on_log(f"  [목록 {page_no}페이지] 오류: {e} — 3초 후 재시도")
                    if consecutive_errors >= MAX_CONSECUTIVE_LIST_ERRORS:
                        self.on_log(
                            "  [목록] 연속 5회 실패로 중단 — 네트워크/세션 상태를 확인하세요."
                        )
                        end_reason = LIST_END_ERRORS
                        break
                    self.control.sleep(self._retry_delay)
                    continue
                consecutive_errors = 0

                if limit is not None:
                    remaining = limit - len(products)
                    if remaining <= 0:
                        break
                    edges = edges[:remaining]

                new = 0
                for e in edges:
                    node = e.get("node") or {}
                    nid = node.get("nid")
                    if nid is None:
                        continue
                    if nid not in products:
                        products[nid] = node
                        new += 1
                self.on_log(
                    f"  [목록 {page_no}페이지] +{new} (누적 {len(products)}, "
                    f"hasNext={pi.get('hasNextPage')})"
                )
                if limit is not None and len(products) >= limit:
                    break
                after = pi.get("endCursor")
                if not edges or not pi.get("hasNextPage"):
                    break
                if new == 0:
                    # 반복 구간 진입(카탈로그 끝) — 새 상품 없는 페이지가 연속 2회면 종료
                    zero_new_pages += 1
                    if zero_new_pages >= 2:
                        self.on_log(
                            f"  [목록] {page_no}페이지에서 신규 상품 없음 2회 — 전체 카탈로그 종료"
                        )
                        break
                else:
                    zero_new_pages = 0
                self.control.sleep(
                    random.uniform(self.config.delay_min, self.config.delay_max)
                )

            if end_reason == LIST_END_NATURAL and page_no >= self.config.max_pages:
                end_reason = LIST_END_PAGE_LIMIT
                self.on_log("  [목록] 최대 페이지 수에 도달해 중단")
            return products, end_reason
        except CancelledError:
            self._partial_products = products
            raise

    # ── 3단계: 셀러 사업자정보 ─────────────────────────────────────
    def _graphql_vendor_info(self, node_id: str) -> dict | None:
        """GraphQL node(id) 조회로 셀러 사업자정보(이메일/대표자 포함) 확보."""
        if self.control.is_cancelled():
            raise CancelledError()
        session = self._worker_session()
        body = json.dumps({"query": VENDOR_QUERY, "variables": {"id": node_id}})
        resp = session.post(
            GRAPHQL_URL,
            data=body.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=40,
        )
        resp.raise_for_status()
        data = resp.json()
        node = (data.get("data") or {}).get("node") or {}
        if not node or node.get("__typename") != "Vendor":
            return None
        return {
            "seller_id": node.get("nid"),
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
        }

    @staticmethod
    def _fetch_goods_html(session: requests.Session, pid: str) -> str:
        resp = session.get(DETAIL_URL.format(pid=pid), timeout=40)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return resp.text

    @staticmethod
    def _extract_vendor(html: str, seller_nid: str) -> dict | None:
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return None
        cache = data.get("props", {}).get("pageProps", {}).get("initialRecords", {})
        vendor = cache.get(f"vendor_{seller_nid}")
        if not vendor:
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
            "ecommerce_report_number": (
                vendor.get("mailOrderRegistrationNumber") or ""
            ).strip(),
            "customer_service_number": (
                vendor.get("customerServiceNumber") or ""
            ).strip(),
        }

    def _fetch_one_seller(self, sid: str, pid: str, alt_pids: list[str]) -> tuple:
        """셀러 정보 1건 수집.

        1) GraphQL node(id) 조회 우선 — 이메일/대표자명 포함, 경량.
        2) 실패 시 상세 페이지 HTML 파싱으로 폴백(이메일/대표자명은 빈 값).

        반환: (info_or_None, outcome) — outcome은
        "found"/"missing"/"parse_error"/"request_error" 중 하나.
        취소 시 CancelledError 를 raise 한다.
        """
        session = self._worker_session()

        # 1) GraphQL node 쿼리 (가장 가볍고 이메일/대표자 포함)
        try:
            info = self._graphql_vendor_info(f"vendor_{sid}")
        except CancelledError:
            raise
        except Exception:  # noqa: BLE001
            with self._error_lock:
                self._seller_request_errors += 1
            info = None
        if info is not None:
            return info, "found"

        # 2) 상세 페이지 HTML 폴백
        def _try_one(pid_str: str) -> tuple:
            if self.control.is_cancelled():
                raise CancelledError()
            try:
                html = self._fetch_goods_html(session, pid_str)
            except CancelledError:
                raise
            except Exception:  # noqa: BLE001
                with self._error_lock:
                    self._seller_request_errors += 1
                return None, "request_error"
            info = self._extract_vendor(html, str(sid))
            if info is None:
                with self._error_lock:
                    self._seller_parse_errors += 1
                return None, "parse_error"
            return info, "found"

        info, outcome = _try_one(str(pid))
        if info is None and alt_pids:
            for ap in alt_pids[:3]:
                info, outcome = _try_one(str(ap))
                if info is not None:
                    break
        return info, outcome

    def _crawl_sellers(self, products: dict) -> tuple[dict, int]:
        """고유 셀러별 사업자정보 수집. 반환: (infos, 요청오류총합)."""
        # 셀러별 대표 상품(최초 등장) 매핑
        sellers: dict = {}
        for nid in sorted(products.keys(), key=lambda x: int(x)):
            node = products[nid]
            sid = (node.get("vendor") or {}).get("nid")
            if sid is None:
                continue
            sid = str(sid)
            if sid not in sellers:
                sellers[sid] = {"pid": str(nid)}
        # 대체 상품 후보 (대표 상품 제외, 최대 3개)
        for nid in sorted(products.keys(), key=lambda x: int(x)):
            node = products[nid]
            sid = (node.get("vendor") or {}).get("nid")
            if sid is None:
                continue
            entry = sellers.get(str(sid))
            if (
                entry is not None
                and str(nid) != entry["pid"]
                and len(entry.get("alts", [])) < 3
            ):
                entry.setdefault("alts", []).append(str(nid))

        total = len(sellers)
        self.on_log(f"  셀러 수: {total}")
        infos: dict = {}
        pending = list(sellers.items())
        done = 0
        consecutive_errors = 0
        batch = max(1, self.config.workers * 2)
        ended_early = None
        try:
            for start in range(0, len(pending), batch):
                self.control.checkpoint()
                chunk = pending[start:start + batch]
                with ThreadPoolExecutor(max_workers=self.config.workers) as ex:
                    futs = {
                        ex.submit(
                            self._fetch_one_seller,
                            sid,
                            entry["pid"],
                            entry.get("alts", []),
                        ): sid
                        for sid, entry in chunk
                    }
                    for fut in as_completed(futs):
                        sid = futs[fut]
                        done += 1
                        try:
                            info, outcome = fut.result()
                        except CancelledError:
                            ended_early = "cancelled"
                            break
                        except Exception:  # noqa: BLE001
                            info, outcome = None, "request_error"
                        if info is not None:
                            infos[sid] = info
                            consecutive_errors = 0
                            self.on_log(
                                f"  [{done}/{total}] 셀러 {sid} "
                                f"({info.get('store_name')}) "
                                f"사업자등록번호={info.get('business_number') or '∅'}"
                            )
                        else:
                            infos[sid] = {
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
                            if outcome == "request_error":
                                consecutive_errors += 1
                                self.on_log(
                                    f"  [{done}/{total}] 셀러 {sid}: 요청 오류 "
                                    f"(연속 {consecutive_errors})"
                                )
                                if consecutive_errors >= MAX_CONSECUTIVE_SELLER_ERRORS:
                                    ended_early = "seller_errors"
                                    break
                            elif outcome == "parse_error":
                                self.on_log(
                                    f"  [{done}/{total}] 셀러 {sid}: 상세 페이지 구조 파싱 실패"
                                )
                            else:
                                self.on_log(f"  [{done}/{total}] 셀러 {sid}: 정보 없음")
                        self.on_progress("seller", done, total)
                        self.on_record(dict(infos[sid]))
                    if ended_early:
                        break
                if ended_early:
                    break
                if self.control.is_cancelled():
                    raise CancelledError()
                self.control.sleep(
                    random.uniform(self.config.delay_min, self.config.delay_max)
                )
        except CancelledError:
            ended_early = "cancelled"
        if ended_early:
            self.on_log(f"  [셀러] 조기 종료: {ended_early} (수집 {len(infos)}/{total})")
        if ended_early == "cancelled":
            self._partial_infos = infos
            raise CancelledError()

        # 상품수 집계 + 대표 상품 URL (ID는 문자열로 일원화)
        counts: dict = {}
        for node in products.values():
            sid = (node.get("vendor") or {}).get("nid")
            if sid is not None:
                key = str(sid)
                counts[key] = counts.get(key, 0) + 1
        for sid, info in infos.items():
            info["_product_count"] = int(counts.get(sid, 0))
            info["_representative_pid"] = sellers.get(sid, {}).get("pid", "")
        return infos, self._seller_request_errors

    # ── 저장 ──────────────────────────────────────────────────────
    def _save_partial(self, summary: FoodSpringRunSummary,
                      products: dict, infos: dict) -> None:
        """수집된 분량을 부분 엑셀로 저장 (취소/오류 시 포함)."""
        if not products and not infos:
            return
        try:
            path = self._exporter.save(products, infos, self.config)
            summary.xlsx_path = str(path)
            self.on_log(f"  엑셀 저장: {path}")
        except Exception as e:  # noqa: BLE001
            summary.save_error = str(e) or e.__class__.__name__
            summary.termination_reason = "save_error"
            self.on_log(f"  엑셀 저장 실패: {summary.save_error}")

    def _build_records(self, infos: dict) -> list:
        records = []
        for v in infos.values():
            pid = v.get("_representative_pid", "")
            records.append(
                FoodSpringSellerRecord(
                    seller_id=v.get("seller_id", ""),
                    url=f"{DETAIL_URL.format(pid=pid)}" if pid else "",
                    store_name=v.get("store_name", ""),
                    owner_name=v.get("owner_name", ""),
                    business_number=v.get("business_number", ""),
                    phone=v.get("phone", ""),
                    email=v.get("email", ""),
                    address=v.get("address", ""),
                    ecommerce_report_number=v.get("ecommerce_report_number", ""),
                    customer_service_number=v.get("customer_service_number", ""),
                    product_count=int(v.get("_product_count", 0)),
                ).to_dict()
            )
        return records

    # ── 실행 ──────────────────────────────────────────────────────
    def run(self) -> FoodSpringRunSummary:
        summary = FoodSpringRunSummary()
        query = self._load_query()
        products: dict = {}
        infos: dict = {}
        try:
            # 1. 세션
            self._emit_phase("세션 확보", 1, 4)
            self._establish_session()

            # 2. 상품
            self._emit_phase("상품 목록 수집", 2, 4)
            products, list_end = self._crawl_products(query)
            summary.products_seen = len(products)
            summary.unique_products = len(products)
            self.on_log(f"총 상품(중복 제거): {len(products)} (종료: {list_end})")
            if list_end == LIST_END_PAGE_LIMIT:
                self.on_log(
                    "  참고: 목록 최대 페이지(max_pages) 도달 — 카탈로그가 늘어난 경우 "
                    "설정에서 최대 페이지를 늘리세요."
                )
            self.on_stats(summary)
            if list_end == LIST_END_ERRORS:
                summary.error = (
                    "목록 수집 중 네트워크/세션 오류가 연속 발생해 중단되었습니다. "
                    "지금까지 수집된 상품만 부분 처리됩니다."
                )
                summary.termination_reason = "list_error"
            elif not products:
                summary.termination_reason = "no_items"

            # 3. 셀러 (상품이 있으면 진행)
            if products:
                self._emit_phase("판매자 정보 수집", 3, 4)
                infos, req_errors = self._crawl_sellers(products)
                summary.request_errors = req_errors
                summary.unique_vendors = len(infos)
                summary.business_info_success = sum(
                    1 for v in infos.values() if v and v.get("business_number")
                )
                summary.email_success = sum(
                    1 for v in infos.values() if v and v.get("email")
                )
                summary.missing_info_sellers = len(infos) - summary.business_info_success
                self.on_log(
                    f"  셀러 {len(infos)}명 — 사업자등록번호 확보 "
                    f"{summary.business_info_success}명, 이메일 확보 {summary.email_success}명, "
                    f"요청 오류 {req_errors}건"
                )
                self.on_stats(summary)

                # 장애 판정: 요청 실패율/연속 실패 초과 시 실패로 표시
                if summary.error is None and req_errors > 0:
                    ratio = req_errors / max(1, len(infos) + req_errors)
                    if ratio >= MAX_SELLER_ERROR_RATIO:
                        summary.error = (
                            f"판매자 상세 요청 실패율이 높아({ratio:.0%}) "
                            "결과가 불완전합니다. 확보된 데이터는 저장됩니다."
                        )
                        summary.termination_reason = "seller_error"
                if summary.error is None and summary.missing_info_sellers > 0:
                    self.on_log(
                        f"  참고: 사업자정보 없는 셀러 {summary.missing_info_sellers}명 "
                        "(빈 셀러 행으로 기록)"
                    )
                if summary.error is None and infos and summary.business_info_success == 0:
                    # 전부 파싱 실패/정보 없음 → 페이지 구조 변경 가능성
                    summary.error = (
                        "사업자정보를 전혀 확보하지 못했습니다 "
                        "(상세 페이지 구조 변경 가능성)."
                    )
                    summary.termination_reason = "seller_error"

            # 4. 저장
            if products or infos:
                self._emit_phase("엑셀 저장", 4, 4)
                self._save_partial(summary, products, infos)
                summary.records = self._build_records(infos)
                if summary.error is None and summary.save_error is None:
                    if not products and not infos:
                        summary.termination_reason = "no_items"
                    elif summary.termination_reason in ("", "complete"):
                        summary.termination_reason = "complete"
        except CancelledError:
            summary.cancelled = True
            summary.termination_reason = "cancelled"
            if not products:
                products = self._partial_products
            if not infos:
                infos = self._partial_infos
            if products:
                summary.products_seen = len(products)
                summary.unique_products = len(products)
            if infos:
                summary.unique_vendors = len(infos)
                summary.business_info_success = sum(
                    1 for v in infos.values() if v and v.get("business_number")
                )
                summary.email_success = sum(
                    1 for v in infos.values() if v and v.get("email")
                )
                summary.missing_info_sellers = (
                    len(infos) - summary.business_info_success
                )
            if products or infos:
                self.on_log("[취소] 수집된 분량을 부분 엑셀로 저장합니다...")
                self._save_partial(summary, products, infos)
                summary.records = self._build_records(infos)
            else:
                self.on_log("[취소] 수집된 결과가 없습니다.")
        except Exception as e:  # noqa: BLE001
            if isinstance(e, requests.exceptions.HTTPError):
                summary.error = f"HTTP {e.response.status_code}: {e}"
            else:
                summary.error = str(e) or e.__class__.__name__
            summary.termination_reason = summary.termination_reason or "error"
            if products or infos:
                self.on_log("[오류] 수집된 분량을 부분 엑셀로 저장합니다...")
                self._save_partial(summary, products, infos)
                summary.records = self._build_records(infos)
        return summary