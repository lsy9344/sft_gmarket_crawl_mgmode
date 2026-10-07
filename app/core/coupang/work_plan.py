"""볼륨 인지 작업 분할 계획 — 가중 packing·조각 분할·판매자 슬라이스.

병렬 확장의 작업 단위를 카테고리 개수가 아니라 예상 물량 기준으로 만든다
(PARALLEL_VOLUME_AWARE_SPLIT_DESIGN_20261006 §5). 물량 소스는 카테고리
트리의 productCount 스냅숏(§4 1순위)이고, 전혀 없으면 라운드로빈 균등
분할로 열화 호환(§4 3순위)한다.

작업 단위 세 종류(§5.2 스키마):
- whole  : 작은 카테고리 묶음 — 목록→판매자를 한 폴더에서 순차(현행 규약).
- pages  : 큰 카테고리의 목록 조각 (카테고리, 시작페이지, 종료페이지).
           각 조각은 자기 shard 폴더에 독립 상태로 수집한다.
- sellers: 목록 완료 카테고리의 판매자 조각 — 상품을 vendor_item_id 해시로
           서로소 K슬라이스에 나눠 아무 회선이나 가져가 병행한다(§5.3).
           의존성은 배출 규칙(목록 완료 카테고리에서만 생성)으로 보존.

계획은 근사다 — productCount 는 [수집 시작] 시점 스냅숏이고 실제 3P 비율·
페이지 밀도는 수집 중에야 알 수 있다. 틀려도 결과는 편향된 배분일 뿐이며
(데이터 오염 없음) 완주 판정(exhaust 규약)과 병합 dedupe(vendor_item_id/
vendor_id 키)가 흡수한다.
"""

from __future__ import annotations

import csv
import zlib
from dataclasses import dataclass
from pathlib import Path
from statistics import median

from app.core.coupang.patchright_top_thousand import (
    MAX_CATEGORY_PAGES,
    MAX_LISTING_ITEMS,
)

__all__ = [
    "PLAN_VERSION",
    "WORK_KIND_PAGES",
    "WORK_KIND_SELLERS",
    "WORK_KIND_WHOLE",
    "WorkUnit",
    "build_seller_units",
    "count_product_rows",
    "plan_summary",
    "plan_work_units",
    "round_robin_shards",
    "seller_slice_count_for",
    "seller_slice_index",
    "unit_from_dict",
    "unit_to_dict",
]

# 매니저 상태 파일의 plan_version — 1(또는 없음)=통짜 샤드 이전 규약,
# 2=볼륨 인지 조각 분할. 구버전 상태는 이전 규약으로 그대로 재개한다(§5.5).
PLAN_VERSION = 2

WORK_KIND_WHOLE = "whole"
WORK_KIND_PAGES = "pages"
WORK_KIND_SELLERS = "sellers"

# pages/sellers 조각이 다룰 카테고리는 1개뿐이다(조각 = 단일 카테고리 분할).
PIECE_CATEGORY_COUNT = 1


@dataclass(frozen=True)
class WorkUnit:
    """작업 단위 1개 — 샤드 폴더 1개와 1:1.

    whole 은 categories 묶음 전체를 순차 수집하고, pages 는 단일 카테고리의
    page_from~page_to 목록만, sellers 는 단일 카테고리 상품의
    slice_index/slice_count 번 슬라이스 판매자 매핑만 담는다.
    product_sources 는 판매자 조각이 상품(top_products.csv)을 읽을 작업
    폴더 이름(shard_NN) — 목록 조각 폴더들이다.
    """

    kind: str
    categories: tuple[tuple[str, str], ...]
    page_from: int = 0
    page_to: int = 0
    slice_index: int = 0
    slice_count: int = 0
    product_sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in (WORK_KIND_WHOLE, WORK_KIND_PAGES, WORK_KIND_SELLERS):
            raise ValueError(f"알 수 없는 작업 단위 종류입니다: {self.kind}")
        if not self.categories:
            raise ValueError("작업 단위는 카테고리 1개 이상을 담아야 합니다.")
        if self.kind != WORK_KIND_WHOLE and len(self.categories) != PIECE_CATEGORY_COUNT:
            raise ValueError("조각(work pages/sellers)은 카테고리 1개만 담습니다.")
        if self.kind == WORK_KIND_PAGES and not (
            1 <= self.page_from <= self.page_to <= MAX_CATEGORY_PAGES
        ):
            raise ValueError("목록 조각의 페이지 범위가 잘못됐습니다.")
        if self.kind == WORK_KIND_SELLERS and not (
            0 <= self.slice_index < max(1, self.slice_count)
        ):
            raise ValueError("판매자 조각의 슬라이스 번호가 잘못됐습니다.")

    @property
    def category_id(self) -> str:
        """조각이 속한 카테고리 id — whole 은 첫 카테고리(배출 규칙 미사용)."""
        return self.categories[0][0]

    @property
    def category_name(self) -> str:
        return self.categories[0][1]

    @property
    def label(self) -> str:
        """사람 표기용 단위 이름 — 카드·로그 표기(§6)."""
        if self.kind == WORK_KIND_PAGES:
            return f"{self.category_name} 목록 {self.page_from}~{self.page_to}쪽"
        if self.kind == WORK_KIND_SELLERS:
            return (
                f"{self.category_name} 판매자 "
                f"{self.slice_index + 1}/{max(1, self.slice_count)}"
            )
        if len(self.categories) == 1:
            return self.category_name
        return f"{self.category_name} 외 {len(self.categories) - 1}개"


def unit_to_dict(unit: WorkUnit) -> dict:
    """상태 파일용 직렬화 — families 목록과 별도로 unit 사양을 저장한다."""
    return {
        "kind": unit.kind,
        "categories": [
            [category_id, name] for category_id, name in unit.categories
        ],
        "page_from": unit.page_from,
        "page_to": unit.page_to,
        "slice_index": unit.slice_index,
        "slice_count": unit.slice_count,
        "product_sources": list(unit.product_sources),
    }


def unit_from_dict(raw: object) -> WorkUnit:
    """unit_to_dict 역직렬화 — 형식이 어긋나면 ValueError(재시작 시 새 배분)."""
    if not isinstance(raw, dict):
        raise ValueError("작업 단위 사양이 사전이 아닙니다.")
    categories_raw = raw.get("categories")
    if not isinstance(categories_raw, list) or not categories_raw:
        raise ValueError("작업 단위 카테고리 목록이 잘못됐습니다.")
    categories: list[tuple[str, str]] = []
    for pair in categories_raw:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("작업 단위 카테고리 항목이 잘못됐습니다.")
        category_id, name = pair
        if not isinstance(category_id, str) or not isinstance(name, str):
            raise ValueError("작업 단위 카테고리 값이 문자열이 아닙니다.")
        categories.append((category_id, name))
    sources_raw = raw.get("product_sources", [])
    if not isinstance(sources_raw, list) or not all(
        isinstance(source, str) for source in sources_raw
    ):
        raise ValueError("작업 단위 상품 소스 목록이 잘못됐습니다.")
    return WorkUnit(
        kind=str(raw.get("kind") or ""),
        categories=tuple(categories),
        page_from=int(raw.get("page_from") or 0),
        page_to=int(raw.get("page_to") or 0),
        slice_index=int(raw.get("slice_index") or 0),
        slice_count=int(raw.get("slice_count") or 0),
        product_sources=tuple(sources_raw),
    )


def seller_slice_index(vendor_item_id: str, slice_count: int) -> int:
    """vendor_item_id → 슬라이스 번호 — 프로세스를 거쳐도 같은 값(§5.2).

    파이썬 내장 hash() 는 실행마다 시드가 바뀌어 재시작 이어하기가 깨지므로
    crc32 를 쓴다. 슬라이스는 서로소(전체를 빠짐없이 나눔)이고, 같은 상품은
    항상 같은 슬라이스로 간다.
    """
    if slice_count < 1:
        raise ValueError("slice_count는 1 이상이어야 합니다.")
    return zlib.crc32(str(vendor_item_id).encode("utf-8")) % slice_count


def seller_slice_count_for(
    row_count: int, instance_count: int, *, seller_limit: int
) -> int:
    """판매자 조각의 K — 세션 1개 분량(seller_limit)보다 작은 조각은 만들지 않는다.

    설계 문서 §10 권장은 '인스턴스 수와 동일'이지만, 물량이 세션 1개보다
    적은 카테고리를 인스턴스 수만큼 쪼개면 빈 조각만 늘어난다(빈 조각은
    즉시 완주하지만 폴더·큐가 부풀고 라운드 로그가 지저분해진다). 세션
    분량보다 큰 물량에만 인스턴스 수 상한까지 병렬화한다.
    """
    if row_count <= 0:
        return 1
    by_work = -(-row_count // max(1, seller_limit))  # ceil
    return max(1, min(by_work, max(1, instance_count)))


def build_seller_units(
    category: tuple[str, str],
    product_sources: list[str],
    row_count: int,
    instance_count: int,
    *,
    seller_limit: int,
) -> list[WorkUnit]:
    """목록이 완료된 카테고리의 판매자 조각 K개를 만든다(§5.3 배출 규칙).

    product_sources 는 이 카테고리의 목록 조각 폴더 이름(shard_NN)들 —
    판매자 조각은 그 폴더들의 top_products.csv 를 합쳐 슬라이스로 나눈다.
    """
    slice_count = seller_slice_count_for(
        row_count, instance_count, seller_limit=seller_limit
    )
    return [
        WorkUnit(
            kind=WORK_KIND_SELLERS,
            categories=(category,),
            slice_index=index,
            slice_count=slice_count,
            product_sources=tuple(product_sources),
        )
        for index in range(slice_count)
    ]


def round_robin_shards(
    family: list[tuple[str, str]], shard_count: int
) -> list[list[tuple[str, str]]]:
    """가족(루트+후손 카테고리)을 겹치지 않게 라운드로빈 교차 분할한다.

    물량을 모르할 때의 열화 호환(§4 3순위) — parallel_manager.
    split_family_into_shards 의 원래 구현을 이 모듈로 옮겨 공유한다.
    빈 샤드는 반환 목록에서 제외한다.
    """
    if shard_count < 1:
        raise ValueError("shard_count는 1 이상이어야 합니다.")
    shards: list[list[tuple[str, str]]] = [[] for _ in range(shard_count)]
    for index, pair in enumerate(family):
        shards[index % shard_count].append(tuple(pair))
    return [shard for shard in shards if shard]


def _filled_volumes(
    family: list[tuple[str, str]], volumes: dict[str, int]
) -> dict[str, int] | None:
    """카테고리별 예상 물량 — 미지(0) 노드는 알려진 값의 중앙값으로 채운다.

    아무도 물량을 모르면 None(호출자는 라운드로빈 폴백). 중앙값은 하나의
    거대 카테고리가 평균을 크게 끌어올리는 편향을 막는다.
    """
    known = [
        int(volumes.get(category_id) or 0)
        for category_id, _name in family
        if int(volumes.get(category_id) or 0) > 0
    ]
    if not known:
        return None
    filler = max(1, int(median(known)))
    return {
        category_id: (
            value if (value := int(volumes.get(category_id) or 0)) > 0 else filler
        )
        for category_id, _name in family
    }


def _page_pieces(
    category: tuple[str, str], volume: int, target_volume: int
) -> list[WorkUnit]:
    """큰 카테고리(물량 > 2G)를 목록 페이지 조각으로 쪼갠다(§5.2).

    예상 페이지 수 = 물량 / 페이지당 상품(60) — productCount 는 로켓 배송
    상품도 세므로 3P 실측보다 크게 잡힌다. 근사로만 쓴다(모듈 docstring).
    조각 수는 물량/G — 각 조각의 예상 물량이 G 에 가까워진다. 페이지는
    연속·서로소 구간으로 나눈다. 마지막 조각만 예상 종료 페이지 대신 전역
    상한(MAX_CATEGORY_PAGES)까지 열어두는데, productCount 가 목록 깊이를
    과소평가해도(페이지당 상품이 60보다 적은 경우 등) 목록의 실제 끝까지
    추적하게 하기 위해서다 — 예상이 맞으면 빈 페이지 2회 관용
    (EMPTY_PAGE_TOLERANCE)이 예상 종료 직후 조각을 스스로 완주시키므로
    추가 비용은 빈 페이지 방문 2회뿐이고, 조각 간 서로소성도 유지된다
    (끝 조각의 뒤쪽만 연장).
    """
    estimated_pages = max(
        1, min(MAX_CATEGORY_PAGES, -(-volume // MAX_LISTING_ITEMS))
    )
    piece_count = max(1, -(-volume // max(1, target_volume)))
    pages_per_piece = -(-estimated_pages // piece_count)
    pieces: list[WorkUnit] = []
    start = 1
    while start <= estimated_pages:
        end = min(start + pages_per_piece - 1, estimated_pages)
        pieces.append(
            WorkUnit(
                kind=WORK_KIND_PAGES,
                categories=(category,),
                page_from=start,
                page_to=end,
            )
        )
        start = end + 1
    if pieces and pieces[-1].page_to < MAX_CATEGORY_PAGES:
        last = pieces[-1]
        pieces[-1] = WorkUnit(
            kind=WORK_KIND_PAGES,
            categories=(category,),
            page_from=last.page_from,
            page_to=MAX_CATEGORY_PAGES,
        )
    return pieces


def _pack_whole_units(
    smalls: list[tuple[str, str]], volumes: dict[str, int], bin_count: int
) -> list[WorkUnit]:
    """작은 카테고리들을 LPT(최장 처리 시간 우선 greedy)로 bin-packing(§5.1).

    물량 내림차순으로 정렬해 현재 물량이 가장 적은 샤드에 배정한다.
    동점(물량 같음·빈 bin)은 낮은 샤드 번호 — 입력 순서가 같으면 항상 같은
    배분이 나온다(결정성). 빈 bin 은 버린다.
    """
    ordered = sorted(
        smalls, key=lambda pair: (-volumes[pair[0]], pair[0])
    )
    loads = [0] * bin_count
    bins: list[list[tuple[str, str]]] = [[] for _ in range(bin_count)]
    for pair in ordered:
        lightest = min(range(bin_count), key=lambda index: (loads[index], index))
        bins[lightest].append(pair)
        loads[lightest] += volumes[pair[0]]
    return [
        WorkUnit(kind=WORK_KIND_WHOLE, categories=tuple(binned))
        for binned in bins
        if binned
    ]


def plan_work_units(
    family: list[tuple[str, str]],
    volumes: dict[str, int],
    instance_count: int,
    *,
    seller_limit: int = 130,
) -> list[WorkUnit]:
    """가족 하나를 물량 가중 작업 단위 목록으로 나눈다(§5.1+§5.2).

    절차: (1) 물량 미지면 라운드로빈 whole 유닛(현행 열화 호환 — 이때만
    단위 수가 카테고리 수에 갇힌다). (2) 물량을 알면 S = 2N(스틸링 여유
    2배), G = 총물량/S. (3) 물량 > 2G 인 카테고리는 페이지 조각(≈G씩),
    나머지 smalls 는 남은 단위 예산(S - 조각 수)만큼의 bin 에 LPT packing
    — 총 단위 수가 S 에 수렴한다. 카테고리 수 < N 이어도 큰 카테고리가
    조각으로 쪼개져 전 회선에 일감이 생긴다(§3.2 분할 천장 제거).
    (4) 판매자 조각은 계획 시점이 아니라 목록 완료 순간 배출된다
    (build_seller_units, §5.3).

    단위 순서는 조각(무거운 것 먼저) → whole 샤드 — 인스턴스 1:1 초기 배분이
    긴 단위부터 잡도록(LPT 리스트 스케줄링).
    """
    if not family:
        raise ValueError("가족은 비어 있지 않아야 합니다.")
    if instance_count < 1:
        raise ValueError("instance_count는 1 이상이어야 합니다.")
    filled = _filled_volumes(family, volumes)
    if filled is None:
        return [
            WorkUnit(kind=WORK_KIND_WHOLE, categories=tuple(shard))
            for shard in round_robin_shards(
                family, min(len(family), instance_count * 2)
            )
        ]

    total_volume = sum(filled.values())
    unit_budget = instance_count * 2
    target_volume = max(1, total_volume // unit_budget)

    bigs: list[tuple[tuple[str, str], int]] = []
    smalls: list[tuple[str, str]] = []
    for pair in family:
        volume = filled[pair[0]]
        if volume > 2 * target_volume:
            bigs.append((pair, volume))
        else:
            smalls.append(pair)

    pieces: list[WorkUnit] = []
    for pair, volume in bigs:
        pieces.extend(_page_pieces(pair, volume, target_volume))
    # 무거운(예상 구간이 긴) 조각이 먼저 큐에 오도록 — 초기 1:1 배분의
    # 공평성. 마지막 조각은 전역 상한까지 열려 있어 page_to 가 의도 무게와
    # 무관하므로 구간 길이로 정렬한다.
    pieces.sort(
        key=lambda unit: (
            -(unit.page_to - unit.page_from + 1),
            unit.category_id,
            unit.page_from,
        )
    )

    whole_bins = max(1, unit_budget - len(pieces)) if smalls else 0
    wholes = _pack_whole_units(smalls, filled, whole_bins) if smalls else []
    return pieces + wholes


def plan_summary(
    family: list[tuple[str, str]],
    volumes: dict[str, int],
    units: list[WorkUnit],
) -> dict:
    """시작 전 미리보기용 요약(§6) — 총 물량·단위 수·조각 분할 카테고리 수."""
    total_volume = sum(
        max(0, int(volumes.get(category_id) or 0)) for category_id, _ in family
    )
    pages_units = [unit for unit in units if unit.kind == WORK_KIND_PAGES]
    return {
        "volume_known": total_volume > 0,
        "total_volume": total_volume,
        "category_count": len(family),
        "unit_count": len(units),
        "split_category_count": len({unit.category_id for unit in pages_units}),
        "pages_unit_count": len(pages_units),
    }


def count_product_rows(output_root: Path, work_dir_names: list[str]) -> int:
    """작업 폴더들의 top_products.csv 행 수 합 — 판매자 조각 K 결정용(§5.2)."""
    total = 0
    for name in work_dir_names:
        path = output_root / name / "top_products.csv"
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                total += sum(1 for _row in csv.DictReader(handle))
        except OSError:
            continue  # 아직 목록 조각이 돌지 않았다 — 0으로 센다
    return total
