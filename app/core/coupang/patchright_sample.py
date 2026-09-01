"""PROTOTYPE: Patchright로 상품·판매자를 작은 묶음으로 확인한다."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

from app.core.base import CancelledError, Control
from app.core.coupang.exporter import CoupangExporter, CsvWriteError
from app.core.coupang.patchright_canary import (
    BLOCK_MARKERS,
    BLOCK_STATUSES,
    CATEGORY_URL,
    EXTRACT_PRODUCTS_JS,
    HOME_URL,
    REFERENCE_RE,
    claim_live_attempt,
    inspect_block,
    patchright_browser,
    profile_dir,
    record_block,
)
from app.core.coupang.search_parser import parse_extracted
from app.models.coupang_records import CoupangRecord, CoupangRunConfig

MAX_SAMPLE_ITEMS = 8
MAX_SAMPLE_SCAN_ITEMS = 20
SHOP_SESSION_URL = "https://shop.coupang.com/A00067881"

FETCH_VENDORS_JS = r"""
async (viids) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 30000);
  try {
    const response = await fetch(
      "https://shop.coupang.com/api/v2/store/individualInfo/products",
      {
        method: "POST",
        redirect: "error",
        credentials: "include",
        headers: {"Content-Type": "application/json"},
        signal: controller.signal,
        body: JSON.stringify({
          vendorItemIds: viids,
          isVIBased: true,
          storeId: 109671,
          vendorId: "A00067881",
          ignoreAdultCheck: false,
          pageType: 3,
        }),
      },
    );
    return {status: response.status, body: await response.text()};
  } catch (error) {
    return {error: error.name === "AbortError" ? "timeout" : error.message};
  } finally {
    clearTimeout(timer);
  }
}
"""

FETCH_STORE_REVIEW_JS = r"""
async (vendorId) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 30000);
  try {
    const params = new URLSearchParams({vendorId, urlName: vendorId});
    const response = await fetch(
      "https://shop.coupang.com/api/v1/store/getStoreReview?" + params.toString(),
      {credentials: "include", redirect: "error", signal: controller.signal},
    );
    return {status: response.status, body: await response.text()};
  } catch (error) {
    return {error: error.name === "AbortError" ? "timeout" : error.message};
  } finally {
    clearTimeout(timer);
  }
}
"""


def _emit(
    state: dict,
    event: str,
    on_event: Callable[[dict], None] | None,
    **changes: object,
) -> dict:
    state.update(changes, event=event)
    snapshot = dict(state)
    if on_event is not None:
        on_event(snapshot)
    return snapshot


def _checkpoint(control: Control | None) -> None:
    if control is not None:
        control.checkpoint()


def _wait(page, milliseconds: int, control: Control | None) -> None:
    if control is None:
        page.wait_for_timeout(milliseconds)
    else:
        control.sleep(milliseconds / 1_000)


def _navigate(
    page,
    url: str,
    settle_ms: int,
    control: Control | None,
    state: dict,
) -> tuple[bool, int | None, str]:
    """문서 이동 1회를 세고, 기다리기 전과 후에 차단을 검사한다."""
    state["document_navigations"] += 1
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    except Exception as navigation_error:
        try:
            blocked, status, reference = inspect_block(page)
        except Exception:
            raise navigation_error
        if blocked:
            return blocked, status, reference
        raise navigation_error
    _checkpoint(control)
    blocked, status, reference = inspect_block(page, response)
    if blocked:
        return blocked, status, reference
    _wait(page, settle_ms, control)
    _checkpoint(control)
    return inspect_block(page, response)


def _api_block(result: dict) -> tuple[bool, str]:
    status = result.get("status")
    body = str(result.get("body") or "")
    text = body.lower()
    blocked = status in BLOCK_STATUSES or any(marker in text for marker in BLOCK_MARKERS)
    match = REFERENCE_RE.search(body)
    return blocked, f"Reference #{match.group(1)}" if match else ""


def _decode_api(result: dict, label: str) -> tuple[dict | None, str]:
    if result.get("error"):
        return None, f"{label} 요청 실패: {result['error']}"
    status = result.get("status")
    body = str(result.get("body") or "")
    if status != 200:
        return None, f"{label} HTTP {status}"
    if not body or body.lstrip().startswith("<"):
        return None, f"{label}에서 JSON이 아닌 응답을 받았습니다."
    try:
        value = json.loads(body)
    except ValueError:
        return None, f"{label} JSON을 읽지 못했습니다."
    if not isinstance(value, dict):
        return None, f"{label} 응답 형식이 잘못됐습니다."
    return value, ""


def _product_dict(product) -> dict:
    return {
        "product_id": product.legacy_product_id,
        "item_id": product.item_id,
        "vendor_item_id": product.vendor_item_id,
        "title": product.title,
        "price": product.price,
        "url": product.url,
    }


def _progress_path(output_dir: Path, category_id: str) -> Path:
    return output_dir / f"patchright_progress_{category_id}.json"


def _read_progress(output_dir: Path, category_id: str) -> tuple[int, list[str]]:
    path = _progress_path(output_dir, category_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0, []
    except (OSError, ValueError) as error:
        raise ValueError(f"수집 위치 기록을 읽지 못했습니다: {error}") from error
    if not isinstance(value, dict) or value.get("category_id") != category_id:
        raise ValueError("수집 위치 기록 형식이 잘못됐습니다.")
    next_offset = value.get("next_offset")
    seen = value.get("seen_vendor_item_ids")
    if (
        not isinstance(next_offset, int)
        or not 0 <= next_offset <= MAX_SAMPLE_SCAN_ITEMS
        or not isinstance(seen, list)
        or any(not isinstance(item, str) or not item for item in seen)
    ):
        raise ValueError("수집 위치 기록 형식이 잘못됐습니다.")
    return next_offset, list(dict.fromkeys(seen))


def _save_progress(
    output_dir: Path,
    category_id: str,
    next_offset: int,
    seen_vendor_item_ids: list[str],
) -> str:
    path = _progress_path(output_dir, category_id)
    temporary = path.with_name(f"{path.name}.tmp")
    value = {
        "category_id": category_id,
        "next_offset": next_offset,
        "seen_vendor_item_ids": list(dict.fromkeys(seen_vendor_item_ids)),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return str(path)


def _parse_vendors(
    data: dict, requested_vendor_item_ids: list[str], limit: int
) -> dict[str, dict]:
    if data.get("code") != 200:
        return {}
    requested = set(requested_vendor_item_ids)
    vendors: dict[str, dict] = {}
    for product in data.get("data", {}).get("products", []) or []:
        if str(product.get("vendorItemId") or "") not in requested:
            continue
        store = product.get("storeInfoArea") or {}
        vendor_id = str(store.get("vendorId") or "")
        if not vendor_id or vendor_id in vendors:
            continue
        vendors[vendor_id] = {
            "vendorId": vendor_id,
            "storeId": store.get("storeId"),
            "displayName": store.get("displayName") or "",
            "productId": product.get("productId"),
            "itemId": product.get("itemId"),
            "vendorItemId": product.get("vendorItemId"),
        }
        if len(vendors) >= limit:
            break
    return vendors


def _build_record(vendor_id: str, data: dict, vendor: dict) -> dict:
    product_id = vendor.get("productId")
    item_id = vendor.get("itemId")
    vendor_item_id = vendor.get("vendorItemId")
    url = (
        f"https://www.coupang.com/vp/products/{product_id}"
        f"?itemId={item_id}&vendorItemId={vendor_item_id}"
        if product_id and item_id and vendor_item_id
        else ""
    )
    badge = data.get("qualitySellerBadgeDto") or {}
    return CoupangRecord(
        vendor_id=vendor_id,
        url=url,
        store_name=vendor.get("displayName") or "",
        company_name=data.get("name") or "",
        ceo_name=data.get("repPersonName") or "",
        business_number=data.get("businessNumber") or "",
        phone=data.get("repPhoneNum") or "",
        email=data.get("repEmail") or "",
        address=f"{data.get('repAddr1', '')} {data.get('repAddr2', '')}".strip(),
        ecommerce_report_number=data.get("eCommerceReportNumber") or "",
        power_seller=bool(badge),
        power_seller_title=badge.get("qualityTitle") or "",
        rating_count=data.get("ratingCount") or 0,
        thumb_up_ratio=data.get("thumbUpRatio") or 0,
    ).to_dict()


def _save(records: list[dict], output_dir: Path, *, partial: bool) -> tuple[str, str]:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    config = CoupangRunConfig(
        output_dir=output_dir,
        output_prefix=f"patchright_sample_{stamp}",
    )
    json_path, csv_path = CoupangExporter(config).save(records, partial=partial)
    return str(json_path or ""), str(csv_path or "")


def _save_partial_safely(
    records: list[dict], output_dir: Path
) -> tuple[str, str, str]:
    if not records:
        return "", "", ""
    try:
        json_path, csv_path = _save(records, output_dir, partial=True)
        return json_path, csv_path, ""
    except CsvWriteError as error:
        return error.json_path, "", str(error)
    except Exception as error:  # 부분 결과 보존 실패도 원래 종료 이유와 함께 보고한다
        return "", "", f"{type(error).__name__}: {error}"


def run_sample(
    *,
    category_id: str,
    output_dir: Path,
    limit: int = MAX_SAMPLE_ITEMS,
    offset: int | None = None,
    control: Control | None = None,
    on_event: Callable[[dict], None] | None = None,
    state_root: Path | None = None,
    browser_scope_factory: Callable | None = None,
) -> dict:
    """홈·목록 한 페이지에서 다음 작은 묶음의 판매자 정보를 저장한다."""
    if not category_id.isdigit():
        raise ValueError("category_id는 숫자여야 합니다.")
    if not 1 <= limit <= MAX_SAMPLE_ITEMS:
        raise ValueError(f"limit는 1~{MAX_SAMPLE_ITEMS}여야 합니다.")
    saved_offset, seen_vendor_item_ids = _read_progress(output_dir, category_id)
    if offset is None:
        offset = saved_offset
    elif offset < saved_offset:
        raise ValueError(
            f"offset은 저장된 다음 위치 {saved_offset}보다 작을 수 없습니다."
        )
    if offset < 0 or offset + limit > MAX_SAMPLE_SCAN_ITEMS:
        raise ValueError(
            f"offset + limit는 {MAX_SAMPLE_SCAN_ITEMS} 이하여야 합니다."
        )

    state = {
        "mode": "live_sample",
        "browser": "Patchright + installed Google Chrome",
        "category_id": category_id,
        "limit": limit,
        "offset": offset,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "document_navigations": 0,
        "api_calls": 0,
    }
    allowed, reason = claim_live_attempt(state_root)
    if not allowed:
        return _emit(state, "guard_refused", on_event, reason=reason)

    factory = browser_scope_factory or patchright_browser
    records: list[dict] = []
    try:
        with ExitStack() as stack:
            user_data_dir = profile_dir(state_root)
            user_data_dir.mkdir(parents=True, exist_ok=True)
            context = stack.enter_context(factory(user_data_dir, headless=False))
            page = context.pages[0] if context.pages else context.new_page()
            _emit(state, "browser_started", on_event)

            pages = (
                ("home", HOME_URL, 1_500),
                ("category", CATEGORY_URL.format(category_id=category_id), 2_000),
            )
            for name, url, settle_ms in pages:
                _checkpoint(control)
                blocked, status, reference = _navigate(
                    page, url, settle_ms, control, state
                )
                if blocked:
                    record_block(state_root, reference=reference)
                    return _emit(
                        state,
                        "blocked",
                        on_event,
                        blocked_at=name,
                        reference=reference,
                        **{f"{name}_status": status},
                    )
                _emit(
                    state,
                    f"{name}_loaded",
                    on_event,
                    **{f"{name}_status": status},
                )

            # 같은 목록 페이지에서 저장된 위치까지 읽고, 이미 처리한 상품은 제외한다.
            # 페이지를 넘기거나 추가 문서 요청을 만들지는 않는다.
            scan_limit = offset + limit
            rows = page.evaluate(EXTRACT_PRODUCTS_JS, scan_limit)
            _checkpoint(control)
            parsed_products = [
                product for product in parse_extracted(rows) if product.vendor_item_id
            ]
            seen = set(seen_vendor_item_ids)
            if seen:
                products = [
                    product
                    for product in parsed_products
                    if product.vendor_item_id not in seen
                ][:limit]
            else:
                products = parsed_products[offset:scan_limit]
            if not products:
                return _emit(
                    state,
                    "failed",
                    on_event,
                    error="목록에서 vendorItemId가 있는 상품을 찾지 못했습니다.",
                )
            product_rows = [_product_dict(product) for product in products]
            _emit(
                state,
                "products_sampled",
                on_event,
                products=product_rows,
                scan_limit=scan_limit,
            )

            _checkpoint(control)
            blocked, status, reference = _navigate(
                page, SHOP_SESSION_URL, 1_500, control, state
            )
            if blocked:
                record_block(state_root, reference=reference)
                return _emit(
                    state,
                    "blocked",
                    on_event,
                    blocked_at="shop_session",
                    reference=reference,
                    shop_status=status,
                )
            _emit(state, "shop_loaded", on_event, shop_status=status)

            vendor_item_ids = [product.vendor_item_id for product in products]
            state["api_calls"] += 1
            mapping_result = page.evaluate(
                FETCH_VENDORS_JS,
                vendor_item_ids,
            )
            _checkpoint(control)
            blocked, reference = _api_block(mapping_result)
            if blocked:
                record_block(state_root, reference=reference)
                return _emit(
                    state,
                    "blocked",
                    on_event,
                    blocked_at="vendor_mapping",
                    reference=reference,
                )
            mapping_data, error = _decode_api(mapping_result, "판매자 연결")
            if mapping_data is None:
                return _emit(state, "failed", on_event, error=error)
            vendors = _parse_vendors(mapping_data, vendor_item_ids, limit)
            if not vendors:
                return _emit(
                    state, "failed", on_event, error="연결된 판매자를 찾지 못했습니다."
                )
            _emit(state, "vendors_mapped", on_event, vendor_count=len(vendors))

            for vendor_id, vendor in list(vendors.items())[:limit]:
                _checkpoint(control)
                state["api_calls"] += 1
                review_result = page.evaluate(FETCH_STORE_REVIEW_JS, vendor_id)
                _checkpoint(control)
                blocked, reference = _api_block(review_result)
                if blocked:
                    record_block(state_root, reference=reference)
                    json_path, csv_path, save_error = _save_partial_safely(
                        records, output_dir
                    )
                    return _emit(
                        state,
                        "blocked",
                        on_event,
                        blocked_at="business_info",
                        reference=reference,
                        records=records,
                        json_path=json_path,
                        csv_path=csv_path,
                        save_error=save_error,
                    )
                review_data, error = _decode_api(
                    review_result, f"판매자 {vendor_id} 정보"
                )
                if review_data is None:
                    json_path, csv_path, save_error = _save_partial_safely(
                        records, output_dir
                    )
                    return _emit(
                        state,
                        "failed",
                        on_event,
                        error=error,
                        records=records,
                        json_path=json_path,
                        csv_path=csv_path,
                        save_error=save_error,
                    )
                if review_data.get("name"):
                    records.append(_build_record(vendor_id, review_data, vendor))
                _wait(page, 1_500, control)
                _checkpoint(control)

            if not records:
                return _emit(
                    state,
                    "failed",
                    on_event,
                    error="공개 사업자정보를 가진 판매자를 찾지 못했습니다.",
                )
            _checkpoint(control)
            try:
                json_path, csv_path = _save(records, output_dir, partial=False)
            except CsvWriteError as error:
                return _emit(
                    state,
                    "failed",
                    on_event,
                    error="CSV 저장에 실패했지만 JSON은 보존했습니다.",
                    save_error=str(error),
                    records=records,
                    json_path=error.json_path,
                    csv_path="",
                )
            selected_ids = [product.vendor_item_id for product in products]
            if seen_vendor_item_ids:
                updated_seen = [*seen_vendor_item_ids, *selected_ids]
            else:
                updated_seen = [
                    product.vendor_item_id
                    for product in parsed_products[: offset + len(products)]
                ]
            next_offset = offset + len(products)
            try:
                progress_path = _save_progress(
                    output_dir,
                    category_id,
                    next_offset,
                    updated_seen,
                )
            except OSError as error:
                return _emit(
                    state,
                    "failed",
                    on_event,
                    error=f"결과는 저장했지만 수집 위치 기록에 실패했습니다: {error}",
                    records=records,
                    json_path=json_path,
                    csv_path=csv_path,
                )
            return _emit(
                state,
                "sample_completed",
                on_event,
                records=records,
                record_count=len(records),
                json_path=json_path,
                csv_path=csv_path,
                next_offset=next_offset,
                progress_path=progress_path,
            )
    except CancelledError:
        json_path, csv_path, save_error = _save_partial_safely(records, output_dir)
        return _emit(
            state,
            "cancelled",
            on_event,
            records=records,
            json_path=json_path,
            csv_path=csv_path,
            save_error=save_error,
        )
    except Exception as error:
        json_path = str(state.get("json_path") or "")
        csv_path = str(state.get("csv_path") or "")
        save_error = ""
        if records and not json_path:
            json_path, csv_path, save_error = _save_partial_safely(
                records, output_dir
            )
        return _emit(
            state,
            "failed",
            on_event,
            error=f"{type(error).__name__}: {error}",
            records=records,
            json_path=json_path,
            csv_path=csv_path,
            save_error=save_error,
        )
