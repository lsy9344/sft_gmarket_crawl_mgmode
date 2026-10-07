"""Patchright 상위 데이터셋 수집 — 2단계 판매자 공개 사업자정보 수집.

1단계(patchright_top_thousand)가 저장한 3P 상품을 판매자와 연결하고, 고유
판매자별 공개 사업자정보(상호·대표자·사업자번호·전화·이메일·주소)를
중간 저장한다. 세션 봉투는 과일 캠페인에서 검증한 값(한 번에 상품 130개,
요청 사이 3초, HTTP 503은 3시간 뒤 한 번만 재시도)을 그대로 쓴다.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

from app.core.base import CancelledError, Control
from app.core.coupang.patchright_canary import (
    HOME_URL,
    claim_live_attempt,
    patchright_browser,
    profile_dir,
    record_block,
    settle_live_attempt,
)
from app.core.coupang.patchright_full_fruit import (
    PRODUCT_SELLER_FIELDS,
    SELLER_FIELDS,
)
from app.core.coupang.patchright_full_sellers import (
    MAPPING_BATCH_SIZE,
    MAPPING_DELAY_MS,
    MIN_HTTP_503_RETRY_SECONDS,
    SELLER_DELAY_MS,
    _mapping_rows,
    _seller_row,
    seller_work_queue,
)
from app.core.coupang.patchright_sample import (
    FETCH_STORE_REVIEW_JS,
    FETCH_VENDORS_JS,
    SHOP_SESSION_URL,
    _api_block,
    _checkpoint,
    _decode_api,
    _navigate,
    _wait,
)
from app.core.coupang.patchright_top_thousand import (
    PRODUCT_FIELDS,
    TopThousandStore,
)
from app.core.coupang.store_files import (
    UNCONFIRMED_SELLER_ERROR,
    clear_unconfirmed_sellers,
    item_key,
    merge_by_key,
    merge_failed_records,
    read_csv,
    saved_supersedes_failed,
    seller_key,
    validate_mapping_row,
    validate_seller_row,
    write_csv,
    write_json,
)

TOP_SELLER_CONTROL_FILENAME = "top_seller_control.json"
TOP_SELLER_RUNS_FILENAME = "top_seller_runs.jsonl"
MAX_SESSION_PRODUCTS = 600


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class TopSellerStore:
    """판매자 단계 결과 파일을 검증하고 덮어쓰기 없이 합친다."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.products_path = output_dir / "top_products.csv"
        self.sellers_path = output_dir / "top_sellers.csv"
        self.product_seller_path = output_dir / "top_product_seller.csv"
        self.failed_sellers_path = output_dir / "top_failed_sellers.json"
        self.failed_mappings_path = output_dir / "top_failed_mappings.json"

    def ensure_files(self) -> None:
        if not self.sellers_path.exists():
            write_csv(self.sellers_path, SELLER_FIELDS, [])
        if not self.product_seller_path.exists():
            write_csv(self.product_seller_path, PRODUCT_SELLER_FIELDS, [])
        if not self.failed_sellers_path.exists():
            write_json(self.failed_sellers_path, [])
        if not self.failed_mappings_path.exists():
            write_json(self.failed_mappings_path, [])

    def validate(self) -> None:
        sellers = read_csv(self.sellers_path, SELLER_FIELDS)
        if any(
            row.get("status") not in ("saved", "no_public_info", "failed")
            for row in sellers
        ):
            raise ValueError(f"{self.sellers_path.name} 판매자 상태가 잘못됐습니다.")
        read_csv(self.product_seller_path, PRODUCT_SELLER_FIELDS)
        for path in (self.failed_sellers_path, self.failed_mappings_path):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise ValueError(
                    f"{path.name}을 읽지 못했습니다: {error}"
                ) from error
            if not isinstance(value, list):
                raise ValueError(  # noqa: TRY004 - 저장 파일 형식 오류 계약
                    f"{path.name} 형식이 잘못됐습니다."
                )

    def merge_sellers(self, rows: list[dict]) -> tuple[int, int]:
        return merge_by_key(
            self.sellers_path,
            SELLER_FIELDS,
            rows,
            key_of=seller_key,
            validate=validate_seller_row,
            should_replace=saved_supersedes_failed,
        )

    def merge_product_sellers(self, rows: list[dict]) -> tuple[int, int]:
        return merge_by_key(
            self.product_seller_path,
            PRODUCT_SELLER_FIELDS,
            rows,
            key_of=item_key,
            validate=validate_mapping_row,
        )

    def record_failed_seller(self, row: dict) -> None:
        merge_failed_records(self.failed_sellers_path, [row], "vendor_id")

    def record_failed_mappings(self, rows: list[dict]) -> None:
        merge_failed_records(self.failed_mappings_path, rows, "vendor_item_id")

    def requeue_unconfirmed_sellers(self) -> int:
        """요청 표시만 남은 판매자 행을 대기열로 되돌린다(반환: 되돌린 수)."""
        return clear_unconfirmed_sellers(self.sellers_path, SELLER_FIELDS)


def _control_path(output_dir: Path) -> Path:
    return output_dir / TOP_SELLER_CONTROL_FILENAME


def _work(
    store: TopSellerStore,
    limit: int,
    *,
    products_paths: list[Path] | None = None,
    slice_index: int | None = None,
    slice_count: int = 1,
) -> tuple[list[tuple[str, dict]], list[dict]]:
    """이번 세션에 처리할 (판매자, 상품) 대기열 — 공용 규칙의 상품 스키마만 다른 래퍼.

    products_paths/slice_* 는 판매자 조각(볼륨 인지 분할 §5.2) 확장.
    """
    return seller_work_queue(
        store,
        PRODUCT_FIELDS,
        limit,
        products_paths=products_paths,
        slice_index=slice_index,
        slice_count=slice_count,
    )


def read_seller_control(output_dir: Path) -> dict:
    try:
        value = json.loads(_control_path(output_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "status": "ready"}
    except (OSError, ValueError) as error:
        raise ValueError(f"판매자 실행 기록을 읽지 못했습니다: {error}") from error
    # cancelled/in_progress 는 끊긴 실행의 흔적이다 — 다음 실행이 이어서
    # 수집한다(자동 재개). halted 만 데이터 사유로 예약을 멈춘다.
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or value.get("status")
        not in ("ready", "in_progress", "cancelled", "halted", "completed")
    ):
        raise ValueError("판매자 실행 기록 형식이 잘못됐습니다.")
    return value


def _save_control(output_dir: Path, status: str, **changes: object) -> None:
    value = {"version": 1, "status": status, "updated_at": _now_text(), **changes}
    write_json(_control_path(output_dir), value)


def authorize_http_503_retry(
    output_dir: Path, *, now: float | None = None
) -> tuple[bool, str]:
    """HTTP 503 뒤 3시간 이상 기다린 예약 작업 한 번만 다시 연다."""
    control = read_seller_control(output_dir)
    if control["status"] != "halted" or "HTTP 503" not in str(
        control.get("reason") or ""
    ):
        return False, "HTTP 503으로 멈춘 판매자 작업이 아닙니다."
    try:
        halted_at = time.mktime(
            time.strptime(str(control["updated_at"]), "%Y-%m-%d %H:%M:%S")
        )
    except (KeyError, TypeError, ValueError):
        return False, "판매자 중단 시각 기록이 잘못됐습니다."
    current = time.time() if now is None else now
    remaining = MIN_HTTP_503_RETRY_SECONDS - (current - halted_at)
    if remaining > 0:
        minutes = int(remaining // 60) + 1
        return False, f"HTTP 503 재시도까지 {minutes}분 남았습니다."
    _save_control(
        output_dir,
        "ready",
        resumed_at=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(current)),
        resumed_from="HTTP 503",
    )
    return True, ""


# 완성형 판매자 데이터셋 열 — 2026-09-28 반찬 캠페인 산출물 형식과 같은 순서.
FINAL_SELLER_FIELDS = (
    "판매자ID",
    "URL",
    "스토어명",
    "상호",
    "대표자",
    "사업자번호",
    "전화",
    "이메일",
    "주소",
    "통신판매업신고번호",
    "우수판매자여부",
    "우수판매자등급",
    "평가수",
    "추천비율",
    "소속카테고리목록",
    "상품수",
)
_SELLER_FIELD_MAP = {
    "vendor_id": "판매자ID",
    "url": "URL",
    "store_name": "스토어명",
    "company_name": "상호",
    "ceo_name": "대표자",
    "business_number": "사업자번호",
    "phone": "전화",
    "email": "이메일",
    "address": "주소",
    "ecommerce_report_number": "통신판매업신고번호",
    "power_seller": "우수판매자여부",
    "power_seller_title": "우수판매자등급",
    "rating_count": "평가수",
    "thumb_up_ratio": "추천비율",
}


def build_seller_final(output_dir: Path) -> Path:
    """확보한 판매자 사업자정보로 완성형 데이터셋 파일을 만든다.

    사업자정보를 저장한(saved) 판매자만 들어가고, 각 판매자가 어떤
    카테고리 상품으로 연결됐는지 소속 카테고리 목록과 상품 수를 함께 남긴다.
    """
    store = TopSellerStore(output_dir)
    products = read_csv(store.products_path, PRODUCT_FIELDS)
    mappings = read_csv(store.product_seller_path, PRODUCT_SELLER_FIELDS)
    products_by_item = {row["vendor_item_id"]: row for row in products}
    categories: dict[str, list[str]] = {}
    product_counts: dict[str, int] = {}
    for mapping in mappings:
        vendor_id = mapping["vendor_id"]
        product = products_by_item.get(mapping["vendor_item_id"], {})
        name = product.get("category_name") or ""
        names = categories.setdefault(vendor_id, [])
        if name and name not in names:
            names.append(name)
        product_counts[vendor_id] = product_counts.get(vendor_id, 0) + 1

    rows = []
    for seller in read_csv(store.sellers_path, SELLER_FIELDS):
        if seller.get("status") != "saved":
            continue
        row = {
            _SELLER_FIELD_MAP.get(field, field): seller.get(field, "")
            for field in SELLER_FIELDS
            if field in _SELLER_FIELD_MAP
        }
        row["소속카테고리목록"] = " > ".join(
            categories.get(seller["vendor_id"], [])
        )
        row["상품수"] = str(product_counts.get(seller["vendor_id"], 0))
        rows.append(row)
    rows.sort(key=lambda row: -int(row["평가수"] or 0))
    path = output_dir / f"coupang_판매자_{len(rows)}명.csv"
    write_csv(path, FINAL_SELLER_FIELDS, rows)
    return path


def run_top_seller_batch(
    *,
    output_dir: Path,
    limit: int = MAX_SESSION_PRODUCTS,
    control: Control | None = None,
    on_event: Callable[[dict], None] | None = None,
    state_root: Path | None = None,
    browser_profile_root: Path | None = None,
    browser_scope_factory: Callable | None = None,
    proxy: dict | None = None,
    products_paths: list[Path] | None = None,
    slice_index: int | None = None,
    slice_count: int = 1,
) -> dict:
    """저장된 상품을 판매자와 연결하고 공개 사업자정보를 중간 저장한다.

    products_paths/slice_index/slice_count 를 주면 판매자 조각(볼륨 인지
    분할 §5.2)으로 동작한다 — 상품은 경로들의 top_products.csv 를 합쳐
    읽고, 그중 vendor_item_id 해시 슬라이스에 속한 상품만 이 세션의
    일감으로 삼는다. 매핑·판매자 결과는 output_dir(조각 폴더)에 쌓인다.
    """
    if not 1 <= limit <= MAX_SESSION_PRODUCTS:
        raise ValueError(f"limit는 1~{MAX_SESSION_PRODUCTS}여야 합니다.")
    output_dir.mkdir(parents=True, exist_ok=True)
    store = TopSellerStore(output_dir)
    TopThousandStore(output_dir).ensure_files()
    store.ensure_files()
    store.validate()
    current_control = read_seller_control(output_dir)
    if current_control["status"] == "halted":
        # 데이터 사유(실패 매핑·503·검증 실패 등)로 남은 중단만 막는다.
        # cancelled/in_progress 는 끊긴 실행 — 아래에서 마커를 되돌리고 이어서.
        return {
            "event": "seller_halted",
            "reason": current_control.get("reason")
            or "앞선 판매자 작업이 완료되지 않았습니다.",
        }

    # 끊긴 실행의 흔적(in_progress/cancelled)이면 요청 표시만 남은 판매자를
    # 대기열로 되돌린다 — 그래야 이어하기가 그 판매자를 다시 수집한다.
    requeued = store.requeue_unconfirmed_sellers()

    pending, unmapped = _work(
        store,
        limit,
        products_paths=products_paths,
        slice_index=slice_index,
        slice_count=slice_count,
    )
    planned_items = len(pending) + len(unmapped)
    if planned_items == 0:
        _save_control(output_dir, "completed")
        return {
            "event": "seller_collection_complete",
            "sellers_path": str(store.sellers_path),
            "product_seller_path": str(store.product_seller_path),
            "final_path": str(build_seller_final(output_dir)),
        }

    planned_pages = min(10, max(1, math.ceil(planned_items / 60)))
    allowed, reason = claim_live_attempt(
        state_root, planned_items=planned_items, planned_pages=planned_pages
    )
    if not allowed:
        return {"event": "guard_refused", "reason": reason}

    state = {
        "mode": "top_thousand_sellers",
        "browser": "Patchright + installed Google Chrome",
        "limit": limit,
        "planned_items": planned_items,
        "document_navigations": 0,
        "api_calls": 0,
        "started_at": _now_text(),
    }
    _save_control(output_dir, "in_progress", started_at=state["started_at"])
    factory = browser_scope_factory or patchright_browser
    processed_vendors = {
        row["vendor_id"]
        for row in read_csv(store.sellers_path, SELLER_FIELDS)
        if row["vendor_id"]
    }
    mapped_products = 0
    processed_sellers = 0

    def emit(event: str, **changes: object) -> dict:
        state.update(event=event, **changes)
        snapshot = dict(state)
        if on_event is not None:
            on_event(snapshot)
        return snapshot

    if requeued:
        emit("sellers_requeued", requeued_sellers=requeued)

    def halt(event: str, reason_text: str, **changes: object) -> dict:
        _save_control(output_dir, "halted", reason=reason_text, event=event)
        return emit(event, reason=reason_text, **changes)

    class _SellerBlocked(RuntimeError):
        def __init__(self, vendor_id: str, reference: str) -> None:
            super().__init__(vendor_id)
            self.vendor_id = vendor_id
            self.reference = reference

    class _SellerFailed(RuntimeError):
        def __init__(self, vendor_id: str, reason: str) -> None:
            super().__init__(reason)
            self.vendor_id = vendor_id
            self.reason = reason

    try:
        with ExitStack() as stack:
            user_data_dir = profile_dir(
                state_root if browser_profile_root is None else browser_profile_root
            )
            user_data_dir.mkdir(parents=True, exist_ok=True)
            context = stack.enter_context(
                factory(user_data_dir, headless=False, proxy=proxy)
            )
            page = context.pages[0] if context.pages else context.new_page()
            emit("browser_started")
            for name, url in (("home", HOME_URL), ("shop_session", SHOP_SESSION_URL)):
                blocked, status, reference = _navigate(
                    page, url, 1_500, control, state
                )
                if blocked:
                    record_block(state_root, reference=reference)
                    return halt(
                        "blocked",
                        f"{name}에서 차단 신호를 확인했습니다.",
                        blocked_at=name,
                        reference=reference,
                        status=status,
                    )

            def fetch_seller(vendor_id: str, vendor: dict) -> dict | None:
                nonlocal processed_sellers
                if vendor_id in processed_vendors:
                    return None
                _checkpoint(control)
                # 요청 직전에 먼저 표시한다. 응답과 저장 사이에 프로세스가 꺼져도
                # 다음 실행은 같은 판매자에게 다시 요청하지 않는다(세션 시작
                # 시 표시 행을 대기열로 되돌리므로 끊긴 판매자는 다시 수집).
                attempted = _seller_row(
                    vendor_id,
                    vendor,
                    None,
                    UNCONFIRMED_SELLER_ERROR,
                )
                store.merge_sellers([attempted])
                state["api_calls"] += 1
                result = page.evaluate(FETCH_STORE_REVIEW_JS, vendor_id)
                blocked, reference = _api_block(result)
                if blocked:
                    failed = _seller_row(
                        vendor_id, vendor, None, "판매자정보 요청에서 차단됨"
                    )
                    store.merge_sellers([failed])
                    store.record_failed_seller(failed)
                    processed_vendors.add(vendor_id)
                    record_block(state_root, reference=reference)
                    raise _SellerBlocked(vendor_id, reference)
                data, error = _decode_api(result, f"판매자 {vendor_id} 정보")
                row = _seller_row(vendor_id, vendor, data, error)
                store.merge_sellers([row])
                if row["status"] == "failed":
                    store.record_failed_seller(row)
                processed_vendors.add(vendor_id)
                processed_sellers += 1
                emit("seller_saved", processed_sellers=processed_sellers)
                if error:
                    raise _SellerFailed(vendor_id, error)
                _wait(page, SELLER_DELAY_MS, control)
                return row

            for vendor_id, vendor in pending:
                fetch_seller(vendor_id, vendor)

            for start in range(0, len(unmapped), MAPPING_BATCH_SIZE):
                batch = unmapped[start : start + MAPPING_BATCH_SIZE]
                _checkpoint(control)
                state["api_calls"] += 1
                result = page.evaluate(
                    FETCH_VENDORS_JS,
                    [row["vendor_item_id"] for row in batch],
                )
                blocked, reference = _api_block(result)
                if blocked:
                    record_block(state_root, reference=reference)
                    return halt(
                        "blocked",
                        "상품-판매자 연결 요청에서 차단됐습니다.",
                        blocked_at="vendor_mapping",
                        reference=reference,
                    )
                data, error = _decode_api(result, "상품-판매자 연결")
                if data is None:
                    return halt("failed", error, failed_at="vendor_mapping")
                mapping_rows, vendors = _mapping_rows(data, batch)
                if mapping_rows:
                    store.merge_product_sellers(mapping_rows)
                    mapped_products += len(mapping_rows)
                mapped_ids = {row["vendor_item_id"] for row in mapping_rows}
                missing = [row for row in batch if row["vendor_item_id"] not in mapped_ids]
                if missing:
                    now = _now_text()
                    store.record_failed_mappings(
                        [
                            {
                                "product_id": row.get("product_id", ""),
                                "item_id": row.get("item_id", ""),
                                "vendor_item_id": row["vendor_item_id"],
                                "error": "판매자 연결 결과 없음",
                                "updated_at": now,
                            }
                            for row in missing
                        ]
                    )
                emit(
                    "mapping_batch_saved",
                    mapped_products=mapped_products,
                    failed_mappings=len(missing),
                )
                for vendor_id, vendor in vendors.items():
                    fetch_seller(vendor_id, vendor)
                if start + MAPPING_BATCH_SIZE < len(unmapped):
                    _wait(page, MAPPING_DELAY_MS, control)

            remaining_pending, remaining_unmapped = _work(
                store,
                1,
                products_paths=products_paths,
                slice_index=slice_index,
                slice_count=slice_count,
            )
            complete = not remaining_pending and not remaining_unmapped
            _save_control(output_dir, "completed" if complete else "ready")
            settled, settle_reason = settle_live_attempt(
                planned_items,
                planned_items,
                state_root,
                planned_pages=planned_pages,
                actual_pages=planned_pages,
            )
            if not settled:
                return halt("failed", settle_reason)
            result = emit(
                "seller_collection_complete" if complete else "seller_batch_completed",
                mapped_products=mapped_products,
                processed_sellers=processed_sellers,
                sellers_path=str(store.sellers_path),
                product_seller_path=str(store.product_seller_path),
            )
            if complete:
                result["final_path"] = str(build_seller_final(output_dir))
            return result
    except _SellerBlocked as error:
        return halt(
            "blocked",
            "판매자정보 요청에서 차단됐습니다.",
            blocked_at="business_info",
            vendor_id=error.vendor_id,
            reference=error.reference,
        )
    except _SellerFailed as error:
        return halt(
            "failed",
            f"판매자 {error.vendor_id} 처리 실패: {error.reason}",
            failed_at="business_info",
        )
    except CancelledError:
        # 사용자 정지 — control 을 cancelled 로 남겨 다음 실행이 이어서
        # 수집하게 한다(halted 가 아니므로 예약이 멈추지 않는다).
        _save_control(
            output_dir,
            "cancelled",
            reason="사용자가 판매자 작업을 취소했습니다.",
            event="cancelled",
        )
        return emit("cancelled", reason="사용자가 판매자 작업을 취소했습니다.")
    except Exception as error:  # noqa: BLE001 - 브라우저 경계 실패는 저장 후 중단
        return halt("failed", f"{type(error).__name__}: {error}")
