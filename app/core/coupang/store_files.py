"""저장소 파일 공용 유틸 — CSV/JSON 원자 쓰기·검증 읽기·병합·대기열 되돌리기.

patchright_full_fruit 이 사실상 유틸 모듈로 쓰이던 IO 헬퍼와, 세 저장소
(FullFruitStore/TopSellerStore/TopThousandStore)가 같은 모양으로 되풀이하던
덮어쓰지 않기 병합·실패 기록 규칙을 한곳에 둔다. 열 스키마 등 도메인
상수는 각 도메인 모듈이 계속 소유한다(이 모듈은 스키마를 값으로만 받는다).
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

__all__ = [
    "UNCONFIRMED_SELLER_ERROR",
    "atomic_write_text",
    "category_item_key",
    "clear_unconfirmed_sellers",
    "item_key",
    "merge_by_key",
    "merge_failed_records",
    "read_csv",
    "saved_supersedes_failed",
    "seller_key",
    "validate_mapping_row",
    "validate_seller_row",
    "write_csv",
    "write_json",
]

# 판매자 정보 요청 직전에 먼저 남기는 표시 문구 — 이 문구를 가진 행은
# 응답을 받기 전에 실행이 끊긴 것이므로 이어하기 전에 대기열로 되돌린다.
UNCONFIRMED_SELLER_ERROR = "판매자정보 요청 시작; 결과 미확정"

# 유효한 판매자 처리 상태 — 세 저장소가 공유하는 계약.
_SELLER_STATUSES = ("saved", "no_public_info", "failed")


def seller_key(row: dict):
    """판매자 병합 키(vendor_id). 비면 None(건너뜀)."""
    vendor_id = str(row.get("vendor_id") or "")
    return vendor_id or None


def item_key(row: dict):
    """상품 병합 키(vendor_item_id). 비면 None(건너뜀)."""
    vendor_item_id = str(row.get("vendor_item_id") or "")
    return vendor_item_id or None


def category_item_key(row: dict):
    """카테고리×상품 병합 키 — 둘 다 있어야 병합 대상(그 외 None)."""
    vendor_item_id = str(row.get("vendor_item_id") or "")
    category_id = str(row.get("category_id") or "")
    if vendor_item_id and category_id:
        return (category_id, vendor_item_id)
    return None


def validate_seller_row(row: dict) -> None:
    vendor_id = str(row.get("vendor_id") or "")
    status = row.get("status")
    if not vendor_id or status not in _SELLER_STATUSES:
        raise ValueError("판매자 ID 또는 처리 상태가 잘못됐습니다.")


def validate_mapping_row(row: dict) -> None:
    if not str(row.get("vendor_item_id") or "") or not str(
        row.get("vendor_id") or ""
    ):
        raise ValueError("상품-판매자 연결 ID가 잘못됐습니다.")


def saved_supersedes_failed(previous: dict, row: dict) -> bool:
    """failed 로 남은 판매자는 나중에 온 비failed 결과에 자리를 내준다."""
    return previous.get("status") == "failed" and row.get("status") != "failed"


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
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


def write_json(path: Path, value: object) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2),
    )


def write_csv(path: Path, fields: tuple[str, ...], rows: list[dict]) -> None:
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


def read_csv(path: Path, fields: tuple[str, ...]) -> list[dict]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != fields:
                raise ValueError(f"{path.name} 열 형식이 잘못됐습니다.")
            return [dict(row) for row in reader]
    except OSError as error:
        raise ValueError(f"{path.name}을 읽지 못했습니다: {error}") from error


def merge_by_key(
    path: Path,
    fields: tuple[str, ...],
    rows: list[dict],
    *,
    key_of: Callable[[dict], object],
    validate: Callable[[dict], None] | None = None,
    should_replace: Callable[[dict, dict], bool] | None = None,
    existing_key_of: Callable[[dict], object] | None = None,
) -> tuple[int, int]:
    """키가 같은 기존 행을 덮어쓰지 않고 합친다. 반환은 (총 행 수, 증가 수).

    key_of(row) 가 None 을 반환하면 새 행을 조용히 건너뛴다(빈 ID 행의
    제품 병합 관례). validate 는 행마다 병합 전에 불려 ValueError 를 던질 수
    있다(판매자 상태 검증). should_replace(previous, row) 가 True 면 기존
    행을 새 행으로 바꾼다(failed 행이 비failed 결과에 자리를 내주는 규칙).
    existing_key_of 는 저장된 행을 읽을 때만 쓰는 키(기본값 key_of) —
    카테고리×상품 병합에서 저장 행은 상품 ID 만으로 인덱싱하는 관례.
    """
    index_key = existing_key_of or key_of
    existing = read_csv(path, fields)
    by_key: dict[object, dict] = {}
    for row in existing:
        key = index_key(row)
        if key is not None:
            by_key[key] = row
    before = len(by_key)
    for row in rows:
        if validate is not None:
            validate(row)
        key = key_of(row)
        if key is None:
            continue
        previous = by_key.get(key)
        if previous is None or (
            should_replace is not None and should_replace(previous, row)
        ):
            by_key[key] = row
    merged = list(by_key.values())
    write_csv(path, fields, merged)
    return len(merged), len(merged) - before


def merge_failed_records(path: Path, rows: list[dict], key_field: str) -> None:
    """실패 기록 JSON 목록을 key_field 기준 중복 없이 합친다(첫 등록 우선)."""
    failures = json.loads(path.read_text(encoding="utf-8"))
    by_key = {
        str(item.get(key_field) or ""): item
        for item in failures
        if isinstance(item, dict) and item.get(key_field)
    }
    for row in rows:
        key = str(row.get(key_field) or "")
        if key:
            by_key.setdefault(key, dict(row))
    write_json(path, list(by_key.values()))


def clear_unconfirmed_sellers(path: Path, fields: tuple[str, ...]) -> int:
    """요청 표시만 남은(결과 미확정) 판매자 행을 지워 대기열로 되돌린다.

    반환은 되돌린 행 수. 표시 행은 요청 직전에 쓰고 응답 저장과 함께
    덮이므로, 세션 시작 시점에 남아 있다는 것은 프로세스가 요청 도중
    끊겼다는 뜻이다 — 지워야 이어하기가 그 판매자를 다시 수집한다.
    """
    rows = read_csv(path, fields)
    kept = [row for row in rows if row.get("error") != UNCONFIRMED_SELLER_ERROR]
    removed = len(rows) - len(kept)
    if removed:
        write_csv(path, fields, kept)
    return removed
