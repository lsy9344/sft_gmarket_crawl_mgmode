"""불변 수집 계획(CrawlPlan) — Pre-scan 확인과 실제 수집 실행을 일치시킨다.

문제: crawl() 이 실행 시점에 매번 collected_ids.json 을 다시 읽어 대상을
재계산하면, 사용자가 Pre-scan 결과 화면에서 확인한 "신규 대상"과 실제로
수집되는 대상이 어긋날 수 있다 — 조사 이후 ID 파일이 바뀌거나(체크포인트
승격 등), max_items 설정이 달라지는 경우 특히 그렇다.

해결: [수집 시작] 클릭 시점에 카테고리별 target_codes 를 max_items 캡까지
포함해 완전히 확정한 CrawlPlan 을 만든다. crawl() 은 이 계획을 그대로
실행하며, 실행 도중 collected_ids.json 이 바뀌어도 이미 확정된 target_codes
자체는 흔들리지 않는다. plan_hash 는 감사/로그용으로 계획 내용을 요약한다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from app.models.records import STATUS_COLLECTABLE, PrescanResult


@dataclass(frozen=True)
class CategoryPlan:
    """카테고리 1개에 대해 확정된 수집 대상."""

    category_name: str
    source: str
    target_codes: tuple[str, ...]
    # True 면 max_items 캡으로 인해 이 실행에서 다루지 못한 신규 대상이
    # 더 남아 있음을 의미한다 — crawler.py 는 이 경우 카테고리를 '완료'로
    # 기록하지 않는다(다음 실행에서 계속 수집할 수 있도록).
    capped: bool = False


@dataclass(frozen=True)
class CrawlPlan:
    """[수집 시작] 시점에 확정되는 불변 실행 계획."""

    categories: tuple[CategoryPlan, ...]
    output_dir: str
    max_items: int
    plan_hash: str

    @property
    def total_targets(self) -> int:
        return sum(len(c.target_codes) for c in self.categories)


def build_crawl_plan(
    prescan_results: list[PrescanResult],
    collected_ids: set[str],
    max_items: int,
    output_dir: str,
) -> CrawlPlan:
    """Pre-scan 결과 + 확정 시점 collected_ids 스냅샷으로 CrawlPlan 을 생성.

    이 함수가 반환한 후에는 collected_ids.json 이 어떻게 바뀌든 이 CrawlPlan
    의 target_codes 는 절대 재계산되지 않는다 — crawl() 은 이 값을 그대로 실행한다.

    같은 goodscode 가 서로 다른 카테고리 리스팅에 중복 노출될 수 있으므로
    (예: 상품이 여러 카테고리에 동시 태깅), `reserved` 로 이미 앞선 카테고리가
    가져간 코드를 추적해 뒤따르는 카테고리가 같은 코드를 또 예약하지 못하게
    한다 — 그렇지 않으면 같은 건이 두 번 수집되어 ALL 통합 파일에 중복 행으로
    남는다.
    """
    categories: list[CategoryPlan] = []
    reserved: set[str] = set()
    for p in prescan_results:
        if p.status != STATUS_COLLECTABLE:
            continue
        candidate_codes = tuple(
            c for c in (p.codes or []) if c not in collected_ids and c not in reserved
        )
        if not candidate_codes:
            continue
        target_codes = candidate_codes[:max_items]
        capped = len(candidate_codes) > max_items
        reserved.update(target_codes)
        categories.append(CategoryPlan(p.category_name, p.source, target_codes, capped))

    plan_hash = _hash_plan(categories, output_dir, max_items)
    return CrawlPlan(tuple(categories), output_dir, max_items, plan_hash)


def _hash_plan(categories: list[CategoryPlan], output_dir: str, max_items: int) -> str:
    payload = json.dumps(
        {
            "output_dir": output_dir,
            "max_items": max_items,
            "categories": [
                {"name": c.category_name, "codes": list(c.target_codes), "capped": c.capped}
                for c in categories
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
