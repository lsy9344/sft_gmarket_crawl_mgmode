"""Patchright 과일 전량 수집 — 2단계 판매자 공개 사업자정보 수집."""

from __future__ import annotations

import csv
import json
import math
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

from app.core.base import CancelledError, Control
from app.core.coupang.patchright_canary import (
    BLOCKED_RESOURCE_TYPES,
    HOME_URL,
    block_heavy_resources,
    claim_live_attempt,
    patchright_browser,
    profile_dir,
    record_block,
    settle_live_attempt,
)
from app.core.coupang.patchright_full_fruit import (
    PRODUCT_FIELDS,
    PRODUCT_SELLER_FIELDS,
    SELLER_FIELDS,
    FullFruitStore,
    _read_csv,
    _write_json,
)
from app.core.coupang.patchright_sample import (
    FETCH_VENDORS_JS,
    SHOP_SESSION_URL,
    _api_block,
    _build_record,
    _checkpoint,
    _decode_api,
    _navigate,
    _wait,
)
from app.core.coupang.seller_detail import fetch_seller_detail
from app.core.coupang.store_files import (
    SELLER_RECHECK_ERROR,
    UNCONFIRMED_SELLER_ERROR,
)
from app.core.coupang.work_plan import seller_slice_index
from app.models.coupang_records import RECORD_FIELDS

SELLER_CONTROL_FILENAME = "fruit_seller_control.json"
MAX_SESSION_PRODUCTS = 600
MAPPING_BATCH_SIZE = 10
MAPPING_DELAY_MS = 3_000
SELLER_DELAY_MS = 3_000
CONSECUTIVE_FAILED_SELLER_LIMIT = 20
CONSECUTIVE_EMPTY_SELLER_LIMIT = 50
MIN_HTTP_503_RETRY_SECONDS = 3 * 60 * 60


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _control_path(output_dir: Path) -> Path:
    return output_dir / SELLER_CONTROL_FILENAME


def _read_control(output_dir: Path) -> dict:
    try:
        value = json.loads(_control_path(output_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "status": "ready"}
    except (OSError, ValueError) as error:
        raise ValueError(f"판매자 실행 기록을 읽지 못했습니다: {error}") from error
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or value.get("status") not in ("ready", "in_progress", "halted", "completed")
    ):
        raise ValueError("판매자 실행 기록 형식이 잘못됐습니다.")
    return value


def _save_control(output_dir: Path, status: str, **changes: object) -> None:
    value = {"version": 1, "status": status, "updated_at": _now_text(), **changes}
    _write_json(_control_path(output_dir), value)


def authorize_http_503_retry(
    output_dir: Path, *, now: float | None = None
) -> tuple[bool, str]:
    """HTTP 503 뒤 3시간 이상 기다린 예약 작업 한 번만 다시 연다."""
    control = _read_control(output_dir)
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


def _import_previous_samples(store: FullFruitStore) -> int:
    """완료된 소량 시험 결과도 최종 파일로 옮겨 재요청을 막는다."""
    imported: list[dict] = []
    for path in sorted(store.output_dir.glob("patchright_sample_*.csv")):
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != RECORD_FIELDS:
                raise ValueError(f"{path.name} 열 형식이 잘못됐습니다.")
            for row in reader:
                if row.get("vendor_id"):
                    imported.append(
                        {"status": "saved", **row, "error": "", "updated_at": _now_text()}
                    )

    seen_vendor_ids: list[str] = []
    for path in sorted(store.output_dir.glob("patchright_progress_*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError(f"{path.name}을 읽지 못했습니다: {error}") from error
        vendors = value.get("seen_vendor_ids", []) if isinstance(value, dict) else None
        if not isinstance(vendors, list) or any(
            not isinstance(vendor_id, str) or not vendor_id for vendor_id in vendors
        ):
            raise ValueError(f"{path.name} 판매자 기록 형식이 잘못됐습니다.")
        seen_vendor_ids.extend(vendors)

    known = {
        row["vendor_id"]
        for row in [*_read_csv(store.sellers_path, SELLER_FIELDS), *imported]
        if row.get("vendor_id")
    }
    for vendor_id in dict.fromkeys(seen_vendor_ids):
        if vendor_id not in known:
            imported.append(
                {
                    "status": "no_public_info",
                    "vendor_id": vendor_id,
                    "error": "이전 표본에서 공개 사업자정보 없음",
                    "updated_at": _now_text(),
                }
            )
            known.add(vendor_id)
    if not imported:
        return 0
    _total, added = store.merge_sellers(imported)
    return added


def _mapping_rows(data: dict, requested: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    if data.get("code") != 200:
        return [], {}
    requested_by_id = {row["vendor_item_id"]: row for row in requested}
    rows: list[dict] = []
    vendors: dict[str, dict] = {}
    mapped_at = _now_text()
    for item in data.get("data", {}).get("products", []) or []:
        vendor_item_id = str(item.get("vendorItemId") or "")
        source = requested_by_id.get(vendor_item_id)
        store_info = item.get("storeInfoArea") or {}
        vendor_id = str(store_info.get("vendorId") or "")
        if source is None or not vendor_id:
            continue
        rows.append(
            {
                "product_id": str(item.get("productId") or source.get("product_id") or ""),
                "item_id": str(item.get("itemId") or source.get("item_id") or ""),
                "vendor_item_id": vendor_item_id,
                "vendor_id": vendor_id,
                "mapped_at": mapped_at,
            }
        )
        vendors.setdefault(
            vendor_id,
            {
                "vendorId": vendor_id,
                "storeId": store_info.get("storeId"),
                "displayName": store_info.get("displayName") or "",
                "productId": item.get("productId") or source.get("product_id"),
                "itemId": item.get("itemId") or source.get("item_id"),
                "vendorItemId": vendor_item_id,
            },
        )
    return rows, vendors


def _vendor_from_saved_mapping(
    mapping: dict, products_by_id: dict[str, dict], seller: dict | None = None
) -> dict:
    product = products_by_id.get(mapping["vendor_item_id"], {})
    previous = seller or {}
    return {
        "vendorId": mapping["vendor_id"],
        "displayName": previous.get("store_name") or "",
        "productId": mapping.get("product_id") or product.get("product_id"),
        "itemId": mapping.get("item_id") or product.get("item_id"),
        "vendorItemId": mapping["vendor_item_id"],
        "url": previous.get("url") or "",
        "previous": previous,
    }


def _seller_row(
    vendor_id: str,
    vendor: dict,
    data: dict | None,
    error: str = "",
    *,
    status: str | None = None,
) -> dict:
    if status is None:
        status = "saved" if data is not None and data.get("name") else (
            "failed" if error else "no_public_info"
        )
    if status == "saved" and data is None:
        status = "failed"
        error = error or "저장할 판매자 데이터가 없습니다."
    if status == "saved" and data is not None:
        record = _build_record(vendor_id, data, vendor)
        previous = vendor.get("previous") or {}
        # 재확인 행이 남긴 스토어명·평점은 새 응답이 비어 있을 때만 보존한다.
        for field in (
            "store_name",
            "power_seller",
            "power_seller_title",
            "rating_count",
            "thumb_up_ratio",
        ):
            current = str(record.get(field) or "")
            previous_value = str(previous.get(field) or "")
            if current in ("", "False", "0") and previous_value not in (
                "",
                "False",
                "0",
            ):
                record[field] = previous[field]
        return {
            "status": "saved",
            **record,
            "error": "",
            "updated_at": _now_text(),
        }
    blank = {field: "" for field in RECORD_FIELDS}
    blank.update(
        vendor_id=vendor_id,
        url=_build_record(vendor_id, {}, vendor)["url"],
        store_name=vendor.get("displayName") or "",
    )
    return {
        "status": status,
        **blank,
        "error": error,
        "updated_at": _now_text(),
    }


def seller_work_queue(
    store,
    product_fields: tuple[str, ...],
    limit: int,
    *,
    products_paths: list[Path] | None = None,
    slice_index: int | None = None,
    slice_count: int = 1,
) -> tuple[list[tuple[str, dict]], list[dict]]:
    """이번 세션에 처리할 (판매자, 상품) 대기열을 고른다.

    판매자 단계의 공용 규칙이다 — 저장소(FullFruitStore/TopSellerStore)는
    products_path/product_seller_path/sellers_path/failed_mappings_path 를
    같은 이름으로 노출하고, 상품 파일 스키마(product_fields)만 다르다.
    연결된 판매자 중 아직 결과 행이 없는 것을 먼저 고르고, 남은 예산은
    연결되지 않은 상품에 쓴다.

    products_paths/slice_index/slice_count 는 판매자 조각(볼륨 인지 분할
    §5.2)용 확장이다 — 상품을 여러 목록 조각 폴더에서 모아 읽고,
    vendor_item_id 해시 슬라이스에 속한 상품만 이 조각의 일감으로 삼는다.
    슬라이스는 서로소이므로 같은 상품을 두 조각이 요청하지 않는다(병합
    dedupe가 넘치는 매핑을 흡수).
    """
    if products_paths is None:
        products = _read_csv(store.products_path, product_fields)
    else:
        products = []
        for path in products_paths:
            products.extend(_read_csv(path, product_fields))
    if slice_count > 1 and slice_index is not None:
        products = [
            row
            for row in products
            if seller_slice_index(row["vendor_item_id"], slice_count)
            == slice_index
        ]
    mappings = _read_csv(store.product_seller_path, PRODUCT_SELLER_FIELDS)
    sellers = _read_csv(store.sellers_path, SELLER_FIELDS)
    failed_mappings = json.loads(store.failed_mappings_path.read_text(encoding="utf-8"))
    products_by_id = {row["vendor_item_id"]: row for row in products}
    sellers_by_id = {
        row["vendor_id"]: row for row in sellers if row.get("vendor_id")
    }
    completed_vendors = {
        row["vendor_id"]
        for row in sellers
        if row["vendor_id"] and row.get("error") != SELLER_RECHECK_ERROR
    }
    failed_mapping_ids = {
        str(row.get("vendor_item_id") or "")
        for row in failed_mappings
        if isinstance(row, dict)
    }

    pending: list[tuple[str, dict]] = []
    pending_ids: set[str] = set()
    for mapping in mappings:
        vendor_id = mapping["vendor_id"]
        if vendor_id in completed_vendors or vendor_id in pending_ids:
            continue
        pending.append(
            (
                vendor_id,
                _vendor_from_saved_mapping(
                    mapping, products_by_id, sellers_by_id.get(vendor_id)
                ),
            )
        )
        pending_ids.add(vendor_id)
        if len(pending) == limit:
            return pending, []

    mapped_item_ids = {row["vendor_item_id"] for row in mappings}
    remaining = limit - len(pending)
    unmapped = [
        row
        for row in products
        if row["vendor_item_id"] not in mapped_item_ids
        and row["vendor_item_id"] not in failed_mapping_ids
    ][:remaining]
    return pending, unmapped


def _work(store: FullFruitStore, limit: int) -> tuple[list[tuple[str, dict]], list[dict]]:
    return seller_work_queue(store, PRODUCT_FIELDS, limit)


def run_seller_batch(
    *,
    output_dir: Path,
    limit: int = MAX_SESSION_PRODUCTS,
    control: Control | None = None,
    on_event: Callable[[dict], None] | None = None,
    state_root: Path | None = None,
    browser_profile_root: Path | None = None,
    browser_scope_factory: Callable | None = None,
) -> dict:
    """저장된 상품을 판매자와 연결하고 공개 사업자정보를 중간 저장한다."""
    if not 1 <= limit <= MAX_SESSION_PRODUCTS:
        raise ValueError(f"limit는 1~{MAX_SESSION_PRODUCTS}여야 합니다.")
    output_dir.mkdir(parents=True, exist_ok=True)
    store = FullFruitStore(output_dir)
    store.ensure_files()
    store.validate()
    imported_sellers = _import_previous_samples(store)
    current_control = _read_control(output_dir)
    if current_control["status"] in ("in_progress", "halted"):
        return {
            "event": "seller_halted",
            "reason": current_control.get("reason") or "앞선 판매자 작업이 완료되지 않았습니다.",
        }

    pending, unmapped = _work(store, limit)
    planned_items = len(pending) + len(unmapped)
    if planned_items == 0:
        existing_sellers = _read_csv(store.sellers_path, SELLER_FIELDS)
        if existing_sellers and not any(
            row.get("status") == "saved" for row in existing_sellers
        ):
            reason = (
                "판매자 사업자정보가 한 건도 저장되지 않은 상태입니다. "
                "완료로 처리하지 않습니다 — 상세 페이지 재조회가 필요합니다."
            )
            _save_control(output_dir, "halted", reason=reason, event="failed")
            store.update_seller_summary(status="halted")
            return {"event": "failed", "reason": reason, "status": "halted"}
        _save_control(output_dir, "completed")
        store.update_seller_summary(status="completed")
        return {
            "event": "seller_collection_complete",
            "imported_sellers": imported_sellers,
            "sellers_path": str(store.sellers_path),
            "product_seller_path": str(store.product_seller_path),
        }

    planned_pages = min(10, max(1, math.ceil(planned_items / 60)))
    allowed, reason = claim_live_attempt(
        state_root, planned_items=planned_items, planned_pages=planned_pages
    )
    if not allowed:
        store.update_seller_summary(status="ready")
        return {"event": "guard_refused", "reason": reason}

    state = {
        "mode": "full_fruit_sellers",
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
        for row in _read_csv(store.sellers_path, SELLER_FIELDS)
        if row["vendor_id"] and row.get("error") != SELLER_RECHECK_ERROR
    }
    mapped_products = 0
    processed_sellers = 0
    saved_sellers = 0
    consecutive_failed = 0
    consecutive_empty = 0

    def emit(event: str, **changes: object) -> dict:
        state.update(event=event, **changes)
        snapshot = dict(state)
        if on_event is not None:
            on_event(snapshot)
        return snapshot

    def halt(event: str, reason_text: str, **changes: object) -> dict:
        _save_control(output_dir, "halted", reason=reason_text, event=event)
        store.update_seller_summary(status="halted")
        return emit(event, reason=reason_text, **changes)

    try:
        with ExitStack() as stack:
            user_data_dir = profile_dir(
                state_root if browser_profile_root is None else browser_profile_root
            )
            user_data_dir.mkdir(parents=True, exist_ok=True)
            context = stack.enter_context(factory(user_data_dir, headless=False))
            page = context.pages[0] if context.pages else context.new_page()
            emit("browser_started")
            if block_heavy_resources(context):
                emit(
                    "heavy_resources_blocked",
                    resource_types=sorted(BLOCKED_RESOURCE_TYPES),
                )
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

            # 상세 페이지는 www.coupang.com 으로 이동하므로 보조 API는
            # shop.coupang.com 에 머무는 별도 탭에서 호출한다(같은 출처).
            review_page = page
            new_page = getattr(context, "new_page", None)
            if callable(new_page):
                try:
                    review_page = new_page()
                    blocked, status, reference = _navigate(
                        review_page, SHOP_SESSION_URL, 1_000, control, state
                    )
                    if blocked:
                        record_block(state_root, reference=reference)
                        return halt(
                            "blocked",
                            "판매자 보조 API 탭에서 차단 신호를 확인했습니다.",
                            blocked_at="shop_session",
                            reference=reference,
                            status=status,
                        )
                except Exception:  # noqa: BLE001 - 보조 탭 실패는 본 탭으로
                    review_page = page

            def fetch_seller(vendor_id: str, vendor: dict) -> dict | None:
                nonlocal processed_sellers, saved_sellers, consecutive_failed
                nonlocal consecutive_empty
                if vendor_id in processed_vendors:
                    return None
                _checkpoint(control)
                # 요청 직전에 먼저 표시한다. 응답과 저장 사이에 프로세스가 꺼져도
                # 다음 실행은 같은 판매자에게 다시 요청하지 않는다.
                attempted = _seller_row(
                    vendor_id,
                    vendor,
                    None,
                    UNCONFIRMED_SELLER_ERROR,
                )
                store.merge_sellers([attempted])
                detail = fetch_seller_detail(
                    page, vendor_id, vendor, control, state, review_page=review_page
                )
                if detail["blocked"]:
                    failed = _seller_row(
                        vendor_id, vendor, None, "판매자정보 요청에서 차단됨"
                    )
                    store.merge_sellers([failed])
                    store.record_failed_seller(failed)
                    processed_vendors.add(vendor_id)
                    record_block(state_root, reference=detail["reference"])
                    raise _SellerBlocked(vendor_id, detail["reference"])
                row = _seller_row(
                    vendor_id,
                    vendor,
                    detail["data"],
                    detail["error"],
                    status=detail["status"],
                )
                store.merge_sellers([row])
                if row["status"] == "failed":
                    store.record_failed_seller(row)
                processed_vendors.add(vendor_id)
                processed_sellers += 1
                if row["status"] == "saved":
                    saved_sellers += 1
                    consecutive_failed = 0
                    consecutive_empty = 0
                elif row["status"] == "failed":
                    consecutive_failed += 1
                    consecutive_empty = 0
                else:
                    consecutive_failed = 0
                    consecutive_empty += 1
                store.update_seller_summary(status="running")
                emit(
                    "seller_saved",
                    processed_sellers=processed_sellers,
                    saved_sellers=saved_sellers,
                )
                if consecutive_failed >= CONSECUTIVE_FAILED_SELLER_LIMIT:
                    raise _SellerFailed(
                        vendor_id,
                        "상품 상세 페이지 판매자 정보 추출이 연속 "
                        f"{consecutive_failed}회 실패했습니다.",
                    )
                if consecutive_empty >= CONSECUTIVE_EMPTY_SELLER_LIMIT:
                    raise _SellerFailed(
                        vendor_id,
                        "상품 상세 페이지 판매자 정보가 연속 "
                        f"{consecutive_empty}회 비어 있습니다. 페이지 구조나 "
                        "접근 조건이 달라졌는지 확인하세요.",
                    )
                _wait(page, SELLER_DELAY_MS, control)
                return row

            for vendor_id, vendor in pending:
                fetch_seller(vendor_id, vendor)

            for start in range(0, len(unmapped), MAPPING_BATCH_SIZE):
                batch = unmapped[start : start + MAPPING_BATCH_SIZE]
                _checkpoint(control)
                if review_page is page and not str(page.url).startswith(
                    SHOP_SESSION_URL
                ):
                    # 보조 탭을 못 연 경우, 상세 페이지가 www로 이동한 뒤의
                    # 매핑 POST가 동일 출처를 잃지 않게 shop으로 돌아간다.
                    blocked, status, reference = _navigate(
                        page, SHOP_SESSION_URL, 1_000, control, state
                    )
                    if blocked:
                        record_block(state_root, reference=reference)
                        return halt(
                            "blocked",
                            "상품-판매자 연결 준비 중 차단됐습니다.",
                            blocked_at="shop_session",
                            reference=reference,
                            status=status,
                        )
                state["api_calls"] += 1
                # 매핑 API는 shop.coupang.com 동일 출처가 필요하다. 상세
                # 페이지가 www로 이동하므로 shop 탭(review_page)에서 호출한다.
                result = review_page.evaluate(
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
                store.update_seller_summary(status="running")
                emit(
                    "mapping_batch_saved",
                    mapped_products=mapped_products,
                    failed_mappings=len(missing),
                )
                for vendor_id, vendor in vendors.items():
                    fetch_seller(vendor_id, vendor)
                if start + MAPPING_BATCH_SIZE < len(unmapped):
                    _wait(page, MAPPING_DELAY_MS, control)

            remaining_pending, remaining_unmapped = _work(store, 1)
            complete = not remaining_pending and not remaining_unmapped
            if complete and processed_sellers and saved_sellers == 0:
                return halt(
                    "failed",
                    "판매자 사업자정보를 한 건도 저장하지 못했습니다. "
                    "상세 페이지 구조·차단 여부를 확인하기 전에는 완료로 "
                    "처리하지 않습니다.",
                    processed_sellers=processed_sellers,
                )
            _save_control(output_dir, "completed" if complete else "ready")
            store.update_seller_summary(status="completed" if complete else "ready")
            settled, settle_reason = settle_live_attempt(
                planned_items,
                planned_items,
                state_root,
                planned_pages=planned_pages,
                actual_pages=planned_pages,
            )
            if not settled:
                return halt("failed", settle_reason)
            return emit(
                "seller_collection_complete" if complete else "seller_batch_completed",
                mapped_products=mapped_products,
                processed_sellers=processed_sellers,
                saved_sellers=saved_sellers,
                imported_sellers=imported_sellers,
                sellers_path=str(store.sellers_path),
                product_seller_path=str(store.product_seller_path),
            )
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
        return halt("cancelled", "사용자가 판매자 작업을 취소했습니다.")
    except Exception as error:  # noqa: BLE001 - 브라우저 경계 실패는 저장 후 중단
        return halt("failed", f"{type(error).__name__}: {error}")


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
