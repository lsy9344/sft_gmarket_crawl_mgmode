"""볼륨 인지 작업 분할 계획(work_plan) 단위 테스트 — 네트워크 없음.

PARALLEL_VOLUME_AWARE_SPLIT_DESIGN_20261006 §8-1: LPT packing 균등성·결정성,
조각 분할 서로소성(페이지 범위·판매자 슬라이스), 작업 단위 직렬화,
물량 미지 폴백(라운드로빈), K(슬라이스 수) 휴리스틱.
"""

from __future__ import annotations

import unittest

from app.core.coupang.work_plan import (
    PLAN_VERSION,
    WORK_KIND_PAGES,
    WORK_KIND_SELLERS,
    WORK_KIND_WHOLE,
    WorkUnit,
    build_seller_units,
    plan_summary,
    plan_work_units,
    round_robin_shards,
    seller_slice_count_for,
    seller_slice_index,
    unit_from_dict,
    unit_to_dict,
)

# 채소 실측(2026-10-06)을 닮은 가족 — 편차 60배(6 vs 377) 재현.
_VEGETABLE_FAMILY = [
    ("194432", "채소"),
    ("194433", "잎채소"),
    ("194434", "뿌리채소"),
    ("194435", "과채"),
    ("194436", "버섯"),
    ("194437", "쌈채소"),
    ("194438", "양념채소"),
    ("194439", "나물"),
    ("194440", "콩류"),
    ("194441", "건나물"),
    ("194442", "브로콜리"),
    ("194443", "샐러드채소"),
    ("194444", "미나리"),
    ("194445", "죽순"),
]
_VEGETABLE_VOLUMES = {
    "194432": 377_000,
    "194433": 120_000,
    "1934434": 0,  # 오타로 미기록 — 미지 취급
    "194434": 60_000,
    "194435": 45_000,
    "194436": 30_000,
    "194437": 18_000,
    "194438": 12_000,
    "194439": 8_000,
    "194440": 5_000,
    "194441": 3_000,
    "194442": 2_000,
    "194443": 1_200,
    "194444": 600,
    "194445": 6,  # 실측 최소(3P 6개)급
}


def _piece_page_spans(units: list[WorkUnit], category_id: str) -> list[tuple[int, int]]:
    return sorted(
        (unit.page_from, unit.page_to)
        for unit in units
        if unit.kind == WORK_KIND_PAGES and unit.category_id == category_id
    )


class PlanWorkUnitsTest(unittest.TestCase):
    """plan_work_units — LPT 균등성·결정성·조각 서로소성·폴백."""

    def test_all_whole_when_no_volume_info(self):
        """물량을 전혀 모르면 라운드로빈 whole 유닛 — 현행과 같은 결과(§4 3순위)."""
        family = [(str(number), f"카테고리{number}") for number in range(1, 15)]
        units = plan_work_units(family, {}, 7)
        self.assertTrue(all(unit.kind == WORK_KIND_WHOLE for unit in units))
        shards = round_robin_shards(family, 14)
        self.assertEqual(
            [list(unit.categories) for unit in units], shards
        )

    def test_lpt_packing_balances_and_is_deterministic(self):
        """물량 가중 배분 — 라운드로빈 대비 편차 축소·두 번 돌려 같은 결과."""
        units = plan_work_units(_VEGETABLE_FAMILY, _VEGETABLE_VOLUMES, 7)
        again = plan_work_units(_VEGETABLE_FAMILY, _VEGETABLE_VOLUMES, 7)
        self.assertEqual(units, again)
        # whole 유닛끼리의 적재 편차가 목표(G=총물량/S)의 3배를 넘지 않는다.
        volumes = {**_VEGETABLE_VOLUMES, "194434": 60_000}  # 미지 보간 포함
        total = sum(volumes[pair[0]] for pair in _VEGETABLE_FAMILY)
        target = total // (7 * 2)
        whole_loads = [
            sum(volumes[pair[0]] for pair in unit.categories)
            for unit in units
            if unit.kind == WORK_KIND_WHOLE
        ]
        for load in whole_loads:
            self.assertLess(load, 3 * target, f"샤드 적재 편차 과대: {load}")

    def test_round_robin_skew_is_larger_than_lpt(self):
        """검증 대조 — 같은 입력에서 라운드로빈 최대/최소 편차가 LPT 보다 크다."""
        volumes = {**_VEGETABLE_VOLUMES, "194434": 60_000}
        units = plan_work_units(_VEGETABLE_FAMILY, volumes, 7)
        whole_loads = sorted(
            sum(volumes[pair[0]] for pair in unit.categories)
            for unit in units
            if unit.kind == WORK_KIND_WHOLE
        )
        rr_loads = sorted(
            sum(volumes[pair[0]] for pair in shard)
            for shard in round_robin_shards(_VEGETABLE_FAMILY, 14)
        )
        self.assertGreater(
            rr_loads[-1] - rr_loads[0], whole_loads[-1] - whole_loads[0]
        )

    def test_big_category_pages_are_disjoint_and_covering(self):
        """큰 카테고리의 목록 조각 — 연속·서로소·전 구간 커버, 상한 50쪽 이내."""
        units = plan_work_units(_VEGETABLE_FAMILY, _VEGETABLE_VOLUMES, 7)
        for category_id in ("194432", "194433"):
            spans = _piece_page_spans(units, category_id)
            self.assertTrue(spans, f"{category_id} 조각이 없습니다")
            self.assertEqual(spans[0][0], 1)
            import itertools

            for (_prev_from, prev_to), (
                next_from,
                next_to,
            ) in itertools.pairwise(spans):
                self.assertEqual(next_from, prev_to + 1)  # 연속·서로소
            self.assertLessEqual(spans[-1][1], 50)

    def test_single_big_category_fills_all_instances(self):
        """카테고리 1개 < 인스턴스 수여도 조각이 전 회선 일감을 만든다(§1)."""
        family = [("194432", "채소")]
        volumes = {"194432": 377_000}
        units = plan_work_units(family, volumes, 7)
        # 2N 예산 안에서 인스턴스 수 이상의 조각 — 전 회선 활성(§3.2 제거).
        self.assertGreaterEqual(len(units), 7)
        self.assertLessEqual(len(units), 14)
        self.assertTrue(all(unit.kind == WORK_KIND_PAGES for unit in units))
        spans = _piece_page_spans(units, "194432")
        self.assertEqual(spans[0][0], 1)
        self.assertEqual(spans[-1][1], 50)  # 377,000/60 > 50 → 상한 커버

    def test_unit_count_converges_to_double_instance_budget(self):
        """총 단위 수가 2N 에 수렴 — 조각+whole 예산 합계(§5.1 S 유지)."""
        units = plan_work_units(_VEGETABLE_FAMILY, _VEGETABLE_VOLUMES, 7)
        self.assertEqual(len(units), 14)

    def test_smalls_stay_whole_and_union_is_the_family(self):
        """조각 대상이 아닌 카테고리는 whole 에 정확히 1회, 조각 카테고리는
        pages 조각에만 — 배분은 겹치지 않고 합집합이 가족 전체다."""
        units = plan_work_units(_VEGETABLE_FAMILY, _VEGETABLE_VOLUMES, 7)
        pages_ids = {
            unit.category_id for unit in units if unit.kind == WORK_KIND_PAGES
        }
        whole_ids = [
            category_id
            for unit in units
            if unit.kind == WORK_KIND_WHOLE
            for category_id, _name in unit.categories
        ]
        self.assertEqual(len(whole_ids), len(set(whole_ids)))  # 중복 배정 없음
        self.assertFalse(set(whole_ids) & pages_ids)  # 조각·whole 이중 배정 없음
        self.assertEqual(
            set(whole_ids) | pages_ids,
            {category_id for category_id, _ in _VEGETABLE_FAMILY},
        )

    def test_pieces_come_before_wholes_for_lpt_initial_assignment(self):
        """무거운 조각이 큐 앞쪽에 온다(초기 1:1 배분의 공평성)."""
        units = plan_work_units(_VEGETABLE_FAMILY, _VEGETABLE_VOLUMES, 7)
        kinds = [unit.kind for unit in units]
        first_whole = kinds.index(WORK_KIND_WHOLE)
        self.assertNotIn(WORK_KIND_PAGES, kinds[first_whole:])

    def test_unknown_volume_filled_with_median(self):
        """미지 카테고리는 알려진 물량의 중앙값으로 채워 배분에 참여한다."""
        family = [("1", "a"), ("2", "b"), ("3", "c")]
        volumes = {"1": 100_000, "2": 10_000}  # 3번 미지 → 중앙값 55,000
        units = plan_work_units(family, volumes, 2)
        seen = {category_id for unit in units for category_id, _ in unit.categories}
        self.assertEqual(seen, {"1", "2", "3"})


class SellerSliceTest(unittest.TestCase):
    """판매자 슬라이스 — 서로소·결정성·K 휴리스틱(§5.2, §8-1)."""

    def test_slices_partition_and_are_stable(self):
        item_ids = [f"{number:07d}" for number in range(1, 2001)]
        for slice_count in (1, 3, 8, 20):
            buckets: list[list[str]] = [[] for _ in range(slice_count)]
            for item_id in item_ids:
                index = seller_slice_index(item_id, slice_count)
                # 같은 입력은 항상 같은 슬라이스(프로세스 재시작 이어하기).
                self.assertEqual(index, seller_slice_index(item_id, slice_count))
                buckets[index].append(item_id)
            union = sorted(item for bucket in buckets for item in bucket)
            self.assertEqual(union, sorted(item_ids))  # 빠짐없이 서로소

    def test_slice_count_heuristic(self):
        """K — 세션 1개 분량보다 작으면 1, 인스턴스 수 상한(§10 권장 변형)."""
        self.assertEqual(seller_slice_count_for(0, 20, seller_limit=130), 1)
        self.assertEqual(seller_slice_count_for(100, 20, seller_limit=130), 1)
        self.assertEqual(seller_slice_count_for(130, 20, seller_limit=130), 1)
        self.assertEqual(seller_slice_count_for(131, 20, seller_limit=130), 2)
        self.assertEqual(seller_slice_count_for(1000, 20, seller_limit=130), 8)
        self.assertEqual(seller_slice_count_for(9_999_999, 7, seller_limit=130), 7)

    def test_build_seller_units_shape(self):
        units = build_seller_units(
            ("194432", "채소"),
            ["shard_01", "shard_02"],
            800,
            7,
            seller_limit=130,
        )
        self.assertEqual(len(units), 7)  # ceil(800/130)=7 (상한=인스턴스 수)
        for index, unit in enumerate(units):
            self.assertEqual(unit.kind, WORK_KIND_SELLERS)
            self.assertEqual(unit.slice_index, index)
            self.assertEqual(unit.slice_count, 7)
            self.assertEqual(unit.product_sources, ("shard_01", "shard_02"))


class WorkUnitContractTest(unittest.TestCase):
    """작업 단위 스키마 — 검증·직렬화 왕복(§5.2 스키마, §8-1)."""

    def test_unit_to_dict_roundtrip(self):
        units = [
            WorkUnit(WORK_KIND_WHOLE, (("1", "a"), ("2", "b"))),
            WorkUnit(WORK_KIND_PAGES, (("3", "c"),), page_from=9, page_to=16),
            WorkUnit(
                WORK_KIND_SELLERS,
                (("3", "c"),),
                slice_index=2,
                slice_count=5,
                product_sources=("shard_01", "shard_02"),
            ),
        ]
        for unit in units:
            self.assertEqual(unit_from_dict(unit_to_dict(unit)), unit)

    def test_invalid_units_are_rejected(self):
        with self.assertRaises(ValueError):
            WorkUnit("nonsense", (("1", "a"),))
        with self.assertRaises(ValueError):
            WorkUnit(WORK_KIND_WHOLE, ())
        with self.assertRaises(ValueError):
            WorkUnit(WORK_KIND_PAGES, (("1", "a"), ("2", "b")))  # 조각은 1카테고리
        with self.assertRaises(ValueError):
            WorkUnit(WORK_KIND_PAGES, (("1", "a"),), page_from=5, page_to=4)
        with self.assertRaises(ValueError):
            WorkUnit(WORK_KIND_PAGES, (("1", "a"),), page_from=0, page_to=3)
        with self.assertRaises(ValueError):
            WorkUnit(
                WORK_KIND_PAGES, (("1", "a"),), page_from=1, page_to=51
            )  # 상한 50쪽
        with self.assertRaises(ValueError):
            WorkUnit(WORK_KIND_SELLERS, (("1", "a"),), slice_index=3, slice_count=3)

    def test_unit_from_dict_rejects_bad_payloads(self):
        for bad in (
            None,
            [],
            {"kind": WORK_KIND_WHOLE},
            {"kind": WORK_KIND_WHOLE, "categories": []},
            {"kind": WORK_KIND_WHOLE, "categories": [["1"]]},
            {
                "kind": WORK_KIND_WHOLE,
                "categories": [["1", "a"]],
                "product_sources": [3],
            },
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                unit_from_dict(bad)

    def test_labels_are_human_readable(self):
        whole = WorkUnit(WORK_KIND_WHOLE, (("1", "채소"), ("2", "버섯")))
        piece = WorkUnit(
            WORK_KIND_PAGES, (("1", "채소"),), page_from=9, page_to=16
        )
        seller = WorkUnit(
            WORK_KIND_SELLERS,
            (("1", "채소"),),
            slice_index=1,
            slice_count=4,
        )
        self.assertIn("외 1개", whole.label)
        self.assertIn("9~16쪽", piece.label)
        self.assertIn("2/4", seller.label)


class PlanSummaryTest(unittest.TestCase):
    """시작 전 미리보기 요약(§6)."""

    def test_summary_reports_volume_and_split_counts(self):
        units = plan_work_units(_VEGETABLE_FAMILY, _VEGETABLE_VOLUMES, 7)
        summary = plan_summary(_VEGETABLE_FAMILY, _VEGETABLE_VOLUMES, units)
        self.assertTrue(summary["volume_known"])
        self.assertEqual(
            summary["total_volume"],
            sum(_VEGETABLE_VOLUMES[pair[0]] for pair in _VEGETABLE_FAMILY),
        )
        self.assertEqual(summary["unit_count"], len(units))
        self.assertEqual(summary["category_count"], len(_VEGETABLE_FAMILY))
        self.assertGreater(summary["split_category_count"], 0)
        self.assertEqual(
            summary["pages_unit_count"],
            sum(1 for unit in units if unit.kind == WORK_KIND_PAGES),
        )

    def test_summary_flags_unknown_volume(self):
        family = [("1", "a")]
        units = plan_work_units(family, {}, 3)
        summary = plan_summary(family, {}, units)
        self.assertFalse(summary["volume_known"])
        self.assertEqual(summary["split_category_count"], 0)


class PlanVersionConstantTest(unittest.TestCase):
    def test_plan_version_is_two(self):
        """v2 = 볼륨 인지 조각 규약 — 매니저 상태 호환 판별에 쓴다(§5.5)."""
        self.assertEqual(PLAN_VERSION, 2)


if __name__ == "__main__":
    unittest.main()
