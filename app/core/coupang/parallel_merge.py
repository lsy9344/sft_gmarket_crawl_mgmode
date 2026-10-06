"""병렬 샤드 출력 병합 — 단일 카테고리 분할 수집의 최종 산출 단계.

분할 모드(shard_mode)에서 각 샤드 폴더(shard_NN)가 독립적으로 수집한
상품·판매자·연결 결과를 실행 루트에서 하나로 합친 뒤 완성형 데이터셋을
만든다. 샤드는 카테고리가 서로 겹치지 않게 나뉘므로 같은 상품이 두 샤드에
나올 수 없지만, 같은 판매자가 여러 샤드의 카테고리에 상품을 팔면 판매자
행이 중복될 수 있다 — 판매자/연결은 각 저장소의 병합 규칙(vendor_id,
vendor_item_id 기준, failed 는 비failed 에게 자리를 내준다)으로 중복을
제거한다.

병합이 끝나면 루트에 coupang_shard_merge.json 표시 파일을 남긴다 — 매니저
finalize 가 이 파일으로 이미 병합된 실행을 다시 합치지 않는다. 병합은 전
인스턴스가 done 일 때만 호출되므로 파일 충돌은 없다(단일 스레드).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from app.core.coupang.patchright_full_fruit import (
    PRODUCT_SELLER_FIELDS,
    SELLER_FIELDS,
)
from app.core.coupang.patchright_top_sellers import (
    TopSellerStore,
    build_seller_final,
)
from app.core.coupang.patchright_top_thousand import (
    FINAL_FAMILY_CATEGORIES,
    PRODUCT_FIELDS,
    TopThousandStore,
    build_combined_final_dataset,
    build_final_dataset,
)
from app.core.coupang.store_files import read_csv, write_csv, write_json

__all__ = [
    "MERGE_STATE_FILENAME",
    "merge_shard_outputs",
]

# 병합 완료 표시 파일 — 루트에 있으면 이 실행은 이미 병합됐다.
MERGE_STATE_FILENAME = "coupang_shard_merge.json"


def _read_csv_if_exists(path: Path, fields: tuple[str, ...]) -> list[dict]:
    """샤드 저장소 CSV 를 읽는다. 파일이 없으면 빈 목록(빈 샤드 결과)."""
    if not path.exists():
        return []
    return read_csv(path, fields)


def _read_json_list_if_exists(path: Path) -> list[dict]:
    """샤드 실패 기록 JSON 을 읽는다. 없거나 빈 파일이면 빈 목록."""
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _merge_failed_json(shard_dirs: list[Path], filename: str, key: str) -> list[dict]:
    """샤드별 실패 기록 JSON 을 key(vendor_id/vendor_item_id)로 합친다."""
    merged: dict[str, dict] = {}
    for shard_dir in shard_dirs:
        for row in _read_json_list_if_exists(shard_dir / filename):
            if not isinstance(row, dict):
                continue
            row_key = str(row.get(key) or "")
            if row_key:
                merged.setdefault(row_key, row)
    return list(merged.values())


def merge_shard_outputs(output_root: Path, shard_dirs: list[Path]) -> dict:
    """샤드 폴더 전부의 결과를 루트로 합치고 완성형 데이터셋을 만든다.

    반환은 요약 dict(shard 수·병합 행 수·완성형 경로). 루트 완성형은
    판매자 파일(coupang_판매자_N명.csv)과 통합 final_dataset_all.csv 이고,
    병합된 카테고리가 기본 A 가족의 어느 루트 가족을 온전히 덮으면 그 루트의
    final_dataset_{root}.csv 도 만든다.
    """
    if not shard_dirs:
        raise ValueError("병합할 샤드 폴더가 없습니다.")
    output_root.mkdir(parents=True, exist_ok=True)

    products: list[dict] = []
    sellers: list[dict] = []
    mappings: list[dict] = []
    for shard_dir in shard_dirs:
        products.extend(
            _read_csv_if_exists(shard_dir / "top_products.csv", PRODUCT_FIELDS)
        )
        sellers.extend(
            _read_csv_if_exists(shard_dir / "top_sellers.csv", SELLER_FIELDS)
        )
        mappings.extend(
            _read_csv_if_exists(
                shard_dir / "top_product_seller.csv", PRODUCT_SELLER_FIELDS
            )
        )
    failed_mappings = _merge_failed_json(
        shard_dirs, "top_failed_mappings.json", "vendor_item_id"
    )
    failed_sellers = _merge_failed_json(
        shard_dirs, "top_failed_sellers.json", "vendor_id"
    )

    # 루트 저장소에 빈 파일을 만들어 두고 저장소 병합 규칙으로 합친다 —
    # 첫 등장(낮은 샤드 번호)이 남고 판매자는 failed 보다 saved 가 이긴다.
    top_store = TopThousandStore(output_root)
    write_csv(output_root / "top_products.csv", PRODUCT_FIELDS, [])
    write_csv(output_root / "top_sellers.csv", SELLER_FIELDS, [])
    write_csv(
        output_root / "top_product_seller.csv", PRODUCT_SELLER_FIELDS, []
    )
    seller_store = TopSellerStore(output_root)
    product_total, _ = top_store.merge_products(products)
    seller_store.merge_sellers(sellers)
    mapping_total, _ = seller_store.merge_product_sellers(mappings)
    write_json(output_root / "top_failed_mappings.json", failed_mappings)
    write_json(output_root / "top_failed_sellers.json", failed_sellers)

    # 완성형 — 판매자 데이터셋과 통합 상품 데이터셋.
    finals = {"sellers": str(build_seller_final(output_root))}
    finals["all"] = str(build_combined_final_dataset(top_store)[0])
    merged_ids = {row["category_id"] for row in products}
    for root_id, family in FINAL_FAMILY_CATEGORIES.items():
        if family <= merged_ids:
            finals[root_id] = str(build_final_dataset(top_store, root_id)[0])

    saved_sellers = sum(
        1 for row in read_csv(output_root / "top_sellers.csv", SELLER_FIELDS)
        if row.get("status") == "saved"
    )
    summary = {
        "shard_count": len(shard_dirs),
        "shard_dirs": [str(shard_dir) for shard_dir in shard_dirs],
        "products": product_total,
        "sellers": saved_sellers,
        "mappings": mapping_total,
        "failed_mappings": len(failed_mappings),
        "failed_sellers": len(failed_sellers),
        "finals": finals,
        "merged_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    write_json(
        output_root / MERGE_STATE_FILENAME,
        {"version": 1, **summary},
    )
    return summary
