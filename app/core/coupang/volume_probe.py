"""계획 조사(물량 probe) — 카테고리당 목록 1페이지로 크기 추정(§4 2순위).

카테고리 트리 응답에 productCount 가 붙지 않으면(2026-10-07 라이브 실측:
3,168개 노드 전부 미부착 — 설계 §10 의 노드별 부착 검증이 부정으로 확정),
[수집 시작] 직전에 직접 회선 브라우저 1개로 각 카테고리의 목록 1페이지를
확인해 전체 페이지 수(``.product-list-paging`` 의 data-total)로 물량을
추정한다 — 홈 웜업 1회 + 카테고리당 요청 1회(설계 §4 의 상한).

추정치는 work_plan 계획의 근사 입력으로만 쓴다(정확도 요구 없음).
data-total 이 없으면 1페이지 카드 수로 최소 크기라도 잡고, 어느 쪽도
실패한 카테고리는 물량 미지로 둔다(계획은 알려진 값의 중앙값으로 채운다).
차단 신호를 만나면 조사를 멈추고 그때까지의 결과만 돌려준다 — 이어서
시작할 수집의 안전 장치가 회선 상태를 다시 심사한다.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

from app.core.coupang.patchright_canary import (
    CATEGORY_URL,
    HOME_URL,
    patchright_browser,
)
from app.core.coupang.patchright_sample import _navigate, _wait
from app.core.coupang.patchright_top_thousand import MAX_LISTING_ITEMS

__all__ = [
    "EXTRACT_LISTING_SIZE_JS",
    "PROBE_DELAY_MS",
    "probe_family_volumes",
]

# 목록 페이지의 전체 페이지 수는 페이지네이션 요소의 data-total 에 있다
# (PAGE_STRUCTURE §리스팅 — 전체 페이지 수). 카드 링크 수는 폴백용.
EXTRACT_LISTING_SIZE_JS = r"""
() => {
  const paging = document.querySelector('.product-list-paging[data-total]');
  const raw = paging ? paging.getAttribute('data-total') : '';
  const total = parseInt(raw || '', 10);
  const links = document.querySelectorAll('a[href*="/vp/products/"]').length;
  return {
    totalPages: Number.isFinite(total) && total > 0 ? total : null,
    links: links,
  };
}
"""

# 카테고리 사이 대기 — 목록 세션의 페이지 간격과 같은 무게(2초).
PROBE_DELAY_MS = 2_000


def _volume_from(size: object) -> int:
    """probe 결과 1건 → 예상 물량. 판독 실패는 0(미지)."""
    if not isinstance(size, dict):
        return 0
    total_pages = size.get("totalPages")
    links = size.get("links")
    if (
        isinstance(total_pages, int)
        and not isinstance(total_pages, bool)
        and total_pages > 0
    ):
        return total_pages * MAX_LISTING_ITEMS
    if isinstance(links, int) and links > 0:
        return MAX_LISTING_ITEMS  # 페이지 수를 못 읽었지만 상품은 있다 — 1페이지 분
    return 1  # 빈 목록 — 즉시 완주하는 아주 작은 단위로 계획에 참여시킨다


def probe_family_volumes(
    family: list[tuple[str, str]],
    browser_scope_factory: Callable | None = None,
    *,
    on_progress: Callable[[str, int, int, int], None] | None = None,
) -> dict[str, int]:
    """가족 카테고리별 예상 물량 — 목록 1페이지 probe(§4 2순위).

    직접 회선(프록시 없음) 브라우저 1개로 홈 웜업 후 카테고리당 목록
    1페이지를 확인한다. on_progress 는 (카테고리 이름, 예상 물량, 순서,
    전체) — 호출자가 진행 표시·이벤트 처리에 쓴다. 반환은 {category_id:
    물량} — 판독에 실패한 카테고리는 포함하지 않는다(계획이 중앙값으로
    채운다). 홈에서 차단이면 빈 사전(물량 미지 폴백).
    """
    factory = browser_scope_factory or patchright_browser
    volumes: dict[str, int] = {}
    with tempfile.TemporaryDirectory(prefix="coupang-volume-probe-") as tmp:
        user_data_dir = Path(tmp) / "profile"
        user_data_dir.mkdir(parents=True, exist_ok=True)
        with factory(user_data_dir, headless=False, proxy=None) as context:
            page = context.pages[0] if context.pages else context.new_page()
            nav_state = {"document_navigations": 0}
            blocked, _status, _reference = _navigate(
                page, HOME_URL, 1_500, None, nav_state
            )
            if blocked:
                return {}
            for index, (category_id, name) in enumerate(family):
                url = CATEGORY_URL.format(category_id=category_id)
                size = None
                try:
                    blocked, _status, _reference = _navigate(
                        page, url, 2_000, None, nav_state
                    )
                    if blocked:
                        break  # 조사 중단 — 부분 결과만 반환(가드가 이어서 심사)
                    size = page.evaluate(EXTRACT_LISTING_SIZE_JS)
                except Exception:  # noqa: BLE001 - 한 카테고리 실패는 미지로
                    size = None
                volume = _volume_from(size)
                if volume > 0:
                    volumes[category_id] = volume
                if on_progress is not None:
                    on_progress(name or category_id, volume, index + 1, len(family))
                if index + 1 < len(family):
                    _wait(page, PROBE_DELAY_MS, None)
    return volumes
