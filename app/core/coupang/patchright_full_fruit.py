"""Patchright 과일 전량 수집 — 1단계 상품 목록 수집."""

from __future__ import annotations

import csv
import json
import os
import tempfile
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

from app.core.base import CancelledError, Control
from app.core.coupang.patchright_canary import (
    CATEGORY_URL,
    EXTRACT_PRODUCTS_JS,
    HOME_URL,
    claim_live_attempt,
    patchright_browser,
    profile_dir,
    record_block,
)
from app.core.coupang.patchright_fruit import FRUIT_CATEGORIES
from app.core.coupang.patchright_sample import _checkpoint, _navigate
from app.core.coupang.search_parser import parse_extracted
from app.models.coupang_records import RECORD_FIELDS

PRODUCT_FIELDS = (
    "category_id",
    "category_name",
    "page_number",
    "product_id",
    "item_id",
    "vendor_item_id",
    "title",
    "price",
    "url",
    "collected_at",
)
SELLER_FIELDS = (
    "status",
    *RECORD_FIELDS,
    "error",
    "updated_at",
)
PRODUCT_SELLER_FIELDS = (
    "product_id",
    "item_id",
    "vendor_item_id",
    "vendor_id",
    "mapped_at",
)

STATE_FILENAME = "fruit_collection_state.json"
PRODUCTS_FILENAME = "fruit_products.csv"
SELLERS_FILENAME = "fruit_sellers.csv"
PRODUCT_SELLER_FILENAME = "fruit_product_seller.csv"
FAILED_SELLERS_FILENAME = "fruit_failed_sellers.json"
SUMMARY_FILENAME = "fruit_collection_summary.json"

MAX_LISTING_ITEMS = 60
MAX_CATEGORY_PAGES = 50
EMPTY_PAGE_TOLERANCE = 2


def _atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _write_json(path: Path, value: object) -> None:
    _atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2),
    )


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(
            descriptor, "w", encoding="utf-8-sig", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fields))
            writer.writeheader()
            writer.writerows(
                {
                    field: (
                        "'" + str(row.get(field, ""))
                        if str(row.get(field, "")).startswith(
                            ("=", "+", "-", "@", "\t", "\r", "\n")
                        )
                        else row.get(field, "")
                    )
                    for field in fields
                }
                for row in rows
            )
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _read_csv(path: Path, fields: tuple[str, ...]) -> list[dict]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != fields:
                raise ValueError(f"{path.name} 열 형식이 잘못됐습니다.")
            return [dict(row) for row in reader]
    except OSError as error:
        raise ValueError(f"{path.name}을 읽지 못했습니다: {error}") from error


class FullFruitStore:
    """전량 결과 파일을 검증하고 덮어쓰기 없이 합친다."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.products_path = output_dir / PRODUCTS_FILENAME
        self.sellers_path = output_dir / SELLERS_FILENAME
        self.product_seller_path = output_dir / PRODUCT_SELLER_FILENAME
        self.failed_sellers_path = output_dir / FAILED_SELLERS_FILENAME
        self.summary_path = output_dir / SUMMARY_FILENAME

    def ensure_files(self) -> None:
        if not self.products_path.exists():
            _write_csv(self.products_path, PRODUCT_FIELDS, [])
        if not self.sellers_path.exists():
            _write_csv(self.sellers_path, SELLER_FIELDS, [])
        if not self.product_seller_path.exists():
            _write_csv(self.product_seller_path, PRODUCT_SELLER_FIELDS, [])
        if not self.failed_sellers_path.exists():
            _write_json(self.failed_sellers_path, [])
        if not self.summary_path.exists():
            _write_json(
                self.summary_path,
                {
                    "status": "running",
                    "raw_products_seen": 0,
                    "unique_products": 0,
                    "unique_sellers": 0,
                    "seller_status_counts": {
                        "saved": 0,
                        "no_public_info": 0,
                        "failed": 0,
                    },
                },
            )

    def validate(self) -> None:
        _read_csv(self.products_path, PRODUCT_FIELDS)
        sellers = _read_csv(self.sellers_path, SELLER_FIELDS)
        if any(
            row.get("status") not in ("saved", "no_public_info", "failed")
            for row in sellers
        ):
            raise ValueError(f"{self.sellers_path.name} 판매자 상태가 잘못됐습니다.")
        _read_csv(self.product_seller_path, PRODUCT_SELLER_FIELDS)
        for path, expected_type in (
            (self.failed_sellers_path, list),
            (self.summary_path, dict),
        ):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise ValueError(f"{path.name}을 읽지 못했습니다: {error}") from error
            if not isinstance(value, expected_type):
                raise ValueError(  # noqa: TRY004 - 저장 파일 형식 오류 계약
                    f"{path.name} 형식이 잘못됐습니다."
                )

    def merge_products(self, rows: list[dict]) -> tuple[int, int]:
        existing = _read_csv(self.products_path, PRODUCT_FIELDS)
        by_vendor_item_id = {
            row["vendor_item_id"]: row for row in existing if row["vendor_item_id"]
        }
        before = len(by_vendor_item_id)
        for row in rows:
            vendor_item_id = str(row.get("vendor_item_id") or "")
            if vendor_item_id:
                by_vendor_item_id.setdefault(vendor_item_id, row)
        merged = list(by_vendor_item_id.values())
        _write_csv(self.products_path, PRODUCT_FIELDS, merged)
        return len(merged), len(merged) - before

    def merge_sellers(self, rows: list[dict]) -> tuple[int, int]:
        existing = _read_csv(self.sellers_path, SELLER_FIELDS)
        by_vendor_id = {
            row["vendor_id"]: row for row in existing if row["vendor_id"]
        }
        before = len(by_vendor_id)
        for row in rows:
            vendor_id = str(row.get("vendor_id") or "")
            status = row.get("status")
            if not vendor_id or status not in ("saved", "no_public_info", "failed"):
                raise ValueError("판매자 ID 또는 처리 상태가 잘못됐습니다.")
            previous = by_vendor_id.get(vendor_id)
            if previous is None or (
                previous.get("status") == "failed" and status != "failed"
            ):
                by_vendor_id[vendor_id] = row
        merged = list(by_vendor_id.values())
        _write_csv(self.sellers_path, SELLER_FIELDS, merged)
        return len(merged), len(merged) - before

    def merge_product_sellers(self, rows: list[dict]) -> tuple[int, int]:
        existing = _read_csv(self.product_seller_path, PRODUCT_SELLER_FIELDS)
        by_vendor_item_id = {
            row["vendor_item_id"]: row
            for row in existing
            if row["vendor_item_id"]
        }
        before = len(by_vendor_item_id)
        for row in rows:
            vendor_item_id = str(row.get("vendor_item_id") or "")
            vendor_id = str(row.get("vendor_id") or "")
            if not vendor_item_id or not vendor_id:
                raise ValueError("상품-판매자 연결 ID가 잘못됐습니다.")
            by_vendor_item_id.setdefault(vendor_item_id, row)
        merged = list(by_vendor_item_id.values())
        _write_csv(self.product_seller_path, PRODUCT_SELLER_FIELDS, merged)
        return len(merged), len(merged) - before

    def update_summary(self, *, raw_products_seen: int, unique_products: int) -> None:
        value = json.loads(self.summary_path.read_text(encoding="utf-8"))
        value["raw_products_seen"] = raw_products_seen
        value["unique_products"] = unique_products
        value["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_json(self.summary_path, value)


def _legacy_offset(output_dir: Path) -> int:
    path = output_dir / "patchright_progress_194284.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0
    except (OSError, ValueError) as error:
        raise ValueError(f"기존 사과/배 위치를 읽지 못했습니다: {error}") from error
    offset = value.get("next_offset") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("category_id") != "194284"
        or not isinstance(offset, int)
        or not 0 <= offset <= MAX_LISTING_ITEMS
    ):
        raise ValueError("기존 사과/배 위치 형식이 잘못됐습니다.")
    return offset


def _new_state(output_dir: Path) -> dict:
    legacy_offset = _legacy_offset(output_dir)
    statuses = {category_id: "pending" for category_id, _ in FRUIT_CATEGORIES}
    statuses[FRUIT_CATEGORIES[0][0]] = "running"
    return {
        "version": 1,
        "phase": "products",
        "status": "running",
        "category_index": 0,
        "page_number": 1,
        "next_offset": legacy_offset,
        "consecutive_empty_pages": 0,
        "raw_products_seen": legacy_offset,
        "unique_products": 0,
        "category_statuses": statuses,
        "completed_categories": [],
    }


def _validate_state(value: object) -> dict:
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("전량 수집 상태 형식이 잘못됐습니다.")
    if value.get("phase") not in ("products", "sellers", "complete"):
        raise ValueError("전량 수집 단계가 잘못됐습니다.")
    if value.get("status") not in (
        "running",
        "completed",
        "incomplete_limit_reached",
    ):
        raise ValueError("전량 수집 상태가 잘못됐습니다.")
    for field in (
        "category_index",
        "page_number",
        "next_offset",
        "consecutive_empty_pages",
        "raw_products_seen",
        "unique_products",
    ):
        if not isinstance(value.get(field), int):
            raise ValueError(  # noqa: TRY004 - 저장 파일 형식 오류 계약
                "전량 수집 상태 숫자 형식이 잘못됐습니다."
            )
    if not 0 <= value["category_index"] <= len(FRUIT_CATEGORIES):
        raise ValueError("전량 수집 카테고리 위치가 잘못됐습니다.")
    if not 1 <= value["page_number"] <= MAX_CATEGORY_PAGES + 1:
        raise ValueError("전량 수집 페이지 위치가 잘못됐습니다.")
    if not 0 <= value["next_offset"] <= MAX_LISTING_ITEMS:
        raise ValueError("전량 수집 상품 위치가 잘못됐습니다.")
    expected_ids = {category_id for category_id, _ in FRUIT_CATEGORIES}
    statuses = value.get("category_statuses")
    if not isinstance(statuses, dict) or set(statuses) != expected_ids:
        raise ValueError("전량 수집 카테고리 상태가 잘못됐습니다.")
    if any(
        status not in ("pending", "running", "completed", "incomplete_limit_reached")
        for status in statuses.values()
    ):
        raise ValueError("전량 수집 카테고리 상태가 잘못됐습니다.")
    completed = value.get("completed_categories")
    if not isinstance(completed, list) or any(item not in expected_ids for item in completed):
        raise ValueError("전량 수집 완료 카테고리 기록이 잘못됐습니다.")
    return value


def _read_state(output_dir: Path) -> dict:
    path = output_dir / STATE_FILENAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _new_state(output_dir)
    except (OSError, ValueError) as error:
        raise ValueError(f"전량 수집 상태를 읽지 못했습니다: {error}") from error
    return _validate_state(value)


def _save_state(output_dir: Path, state: dict) -> str:
    value = dict(state)
    value["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    path = output_dir / STATE_FILENAME
    _write_json(path, value)
    return str(path)


def _finish_category(state: dict, category_id: str) -> None:
    state["category_statuses"][category_id] = "completed"
    state["completed_categories"] = [*state["completed_categories"], category_id]
    state["category_index"] += 1
    state["page_number"] = 1
    state["next_offset"] = 0
    state["consecutive_empty_pages"] = 0
    if state["category_index"] == len(FRUIT_CATEGORIES):
        state["phase"] = "sellers"
    else:
        next_id = FRUIT_CATEGORIES[state["category_index"]][0]
        state["category_statuses"][next_id] = "running"


def _advance_listing_state(state: dict, *, selected_count: int) -> None:
    category_id = FRUIT_CATEGORIES[state["category_index"]][0]
    if selected_count:
        state["next_offset"] += selected_count
        state["consecutive_empty_pages"] = 0
        if state["next_offset"] >= MAX_LISTING_ITEMS:
            state["page_number"] += 1
            state["next_offset"] = 0
    elif state["next_offset"]:
        state["page_number"] += 1
        state["next_offset"] = 0
        state["consecutive_empty_pages"] = 0
    else:
        state["page_number"] += 1
        state["consecutive_empty_pages"] += 1
        if state["consecutive_empty_pages"] >= EMPTY_PAGE_TOLERANCE:
            _finish_category(state, category_id)
            return
    if state["page_number"] > MAX_CATEGORY_PAGES:
        state["page_number"] = MAX_CATEGORY_PAGES + 1
        state["status"] = "incomplete_limit_reached"
        state["category_statuses"][category_id] = "incomplete_limit_reached"


def _product_row(product, *, category_id: str, category_name: str, page_number: int, collected_at: str) -> dict:
    return {
        "category_id": category_id,
        "category_name": category_name,
        "page_number": page_number,
        "product_id": product.legacy_product_id,
        "item_id": product.item_id,
        "vendor_item_id": product.vendor_item_id,
        "title": product.title,
        "price": product.price,
        "url": product.url,
        "collected_at": collected_at,
    }


def run_listing_batch(
    *,
    output_dir: Path,
    limit: int = 24,
    control: Control | None = None,
    on_event: Callable[[dict], None] | None = None,
    state_root: Path | None = None,
    browser_scope_factory: Callable | None = None,
) -> dict:
    """현재 위치에서 상품 목록만 한 묶음 저장한다. 판매자 API는 호출하지 않는다."""
    if not 1 <= limit <= MAX_LISTING_ITEMS:
        raise ValueError(f"limit는 1~{MAX_LISTING_ITEMS}여야 합니다.")
    output_dir.mkdir(parents=True, exist_ok=True)
    state = _read_state(output_dir)
    if state["status"] != "running" or state["phase"] != "products":
        return {"event": state["status"], "phase": state["phase"]}
    store = FullFruitStore(output_dir)
    store.ensure_files()
    store.validate()

    category_id, category_name = FRUIT_CATEGORIES[state["category_index"]]
    page_number = state["page_number"]
    offset = state["next_offset"]
    effective_limit = min(limit, MAX_LISTING_ITEMS - offset)
    result = {
        "mode": "full_fruit_products",
        "browser": "Patchright + installed Google Chrome",
        "category_id": category_id,
        "category_name": category_name,
        "page_number": page_number,
        "offset": offset,
        "limit": effective_limit,
        "document_navigations": 0,
        "api_calls": 0,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if effective_limit == 0:
        _advance_listing_state(state, selected_count=0)
        result["state_path"] = _save_state(output_dir, state)
        result["event"] = "listing_batch_completed"
        return result

    allowed, reason = claim_live_attempt(state_root)
    if not allowed:
        result.update(event="guard_refused", reason=reason)
        return result

    factory = browser_scope_factory or patchright_browser
    try:
        with ExitStack() as stack:
            user_data_dir = profile_dir(state_root)
            user_data_dir.mkdir(parents=True, exist_ok=True)
            context = stack.enter_context(factory(user_data_dir, headless=False))
            page = context.pages[0] if context.pages else context.new_page()
            if on_event:
                on_event({**result, "event": "browser_started"})
            pages = (
                ("home", HOME_URL, 1_500),
                (
                    "category",
                    CATEGORY_URL.format(category_id=category_id).replace(
                        "?page=1", f"?page={page_number}"
                    ),
                    2_000,
                ),
            )
            for name, url, settle_ms in pages:
                _checkpoint(control)
                blocked, status, reference = _navigate(
                    page, url, settle_ms, control, result
                )
                if blocked:
                    record_block(state_root, reference=reference)
                    result.update(
                        event="blocked",
                        blocked_at=name,
                        reference=reference,
                        **{f"{name}_status": status},
                    )
                    return result
                result[f"{name}_status"] = status

            scan_limit = offset + effective_limit
            rows = page.evaluate(EXTRACT_PRODUCTS_JS, scan_limit)
            _checkpoint(control)
            parsed = [
                product for product in parse_extracted(rows) if product.vendor_item_id
            ]
            selected = parsed[offset:scan_limit]
            collected_at = time.strftime("%Y-%m-%d %H:%M:%S")
            product_rows = [
                _product_row(
                    product,
                    category_id=category_id,
                    category_name=category_name,
                    page_number=page_number,
                    collected_at=collected_at,
                )
                for product in parsed[: offset + len(selected)]
            ]
            unique_total, added_count = store.merge_products(product_rows)
            state["raw_products_seen"] += len(selected)
            state["unique_products"] = unique_total
            _advance_listing_state(state, selected_count=len(selected))
            state_path = _save_state(output_dir, state)
            store.update_summary(
                raw_products_seen=state["raw_products_seen"],
                unique_products=unique_total,
            )
            result.update(
                event="listing_batch_completed",
                product_count=len(selected),
                products_added=added_count,
                unique_products=unique_total,
                scan_limit=scan_limit,
                next_offset=state["next_offset"],
                next_page_number=state["page_number"],
                job_status=state["status"],
                state_path=state_path,
                products_path=str(store.products_path),
            )
            return result
    except CancelledError:
        result["event"] = "cancelled"
        return result
    except Exception as error:  # noqa: BLE001 - 브라우저 경계 실패를 결과로 반환
        result.update(event="failed", error=f"{type(error).__name__}: {error}")
        return result
