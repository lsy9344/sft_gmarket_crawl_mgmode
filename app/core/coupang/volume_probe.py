"""계획 조사(물량 probe) — 카테고리당 목록 1페이지로 크기 추정(§4 2순위).

카테고리 트리 응답에 productCount 가 붙지 않으면(2026-10-07 라이브 실측:
3,168개 노드 전부 미부착 — 설계 §10 의 노드별 부착 검증이 부정으로 확정),
[수집 시작] 직전에 각 카테고리의 목록 1페이지를 확인해 전체 페이지 수
(``.product-list-paging`` 의 data-total)로 물량을 추정한다 — 홈 웜업 1회 +
카테고리당 요청 1회(설계 §4 의 상한).

회선은 직접(집) 회선을 먼저 쓰고, 차단·실패로 물량을 못 얻으면 Decodo
조사 전용 스티키 회선(sid ``i990`` — 인스턴스 네임스페이스 i20~i209 밖이라
출구 IP 가 겹치지 않는다)으로 1회 재시도한다(2026-10-07 실측: 새로고침
직후 직접 회선 소프트 블록으로 probe 가 실패한 사례 대응). 조사 결과는
24시간 캐시로 남겨 같은 가족의 재시작은 재조사 없이 계획한다.

추정치는 work_plan 계획의 근사 입력으로만 쓴다(정확도 요구 없음).
data-total 이 없으면 1페이지 카드 수로 최소 크기라도 잡고, 어느 쪽도
실패한 카테고리는 물량 미지로 둔다(계획은 알려진 값의 중앙값으로 채운다).
차단 신호를 만나면 조사를 멈추고 그때까지의 결과만 돌려준다 — 이어서
시작할 수집의 안전 장치가 회선 상태를 다시 심사한다.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
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
    "PROBE_CACHE_FILENAME",
    "PROBE_CACHE_TTL_SECONDS",
    "PROBE_DELAY_MS",
    "PROBE_SESSION_ID",
    "plan_volume_probe",
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

# 조사 전용 decodo sid — 인스턴스 네임스페이스(i{N}0~i{N}9, N=2~20)와
# 겹치지 않는 십진 영역. 회선이 겹치면 조사 트래픽이 수집 회선 평판을
# 가열한다(안전 봉투 원칙).
PROBE_SESSION_ID = "i990"

# 조사 결과 캐시 — 같은 가족의 재시작은 재조사하지 않는다(쿠팡 접촉 최소화).
PROBE_CACHE_FILENAME = "coupang_volume_probe.json"
PROBE_CACHE_TTL_SECONDS = 24 * 60 * 60


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
    proxy: dict | None = None,
) -> dict[str, int]:
    """가족 카테고리별 예상 물량 — 목록 1페이지 probe(§4 2순위).

    proxy=None 이면 직접 회선, dict 이면 그 스티키 프록시로 브라우저 1개를
    열어 홈 웜업 후 카테고리당 목록 1페이지를 확인한다. on_progress 는
    (카테고리 이름, 예상 물량, 순서, 전체) — 호출자가 진행 표시·이벤트
    처리에 쓴다. 반환은 {category_id: 물량} — 판독에 실패한 카테고리는
    포함하지 않는다(계획이 중앙값으로 채운다). 홈에서 차단이면 빈 사전
    (물량 미지 폴백).
    """
    factory = browser_scope_factory or patchright_browser
    volumes: dict[str, int] = {}
    with tempfile.TemporaryDirectory(prefix="coupang-volume-probe-") as tmp:
        user_data_dir = Path(tmp) / "profile"
        user_data_dir.mkdir(parents=True, exist_ok=True)
        with factory(user_data_dir, headless=False, proxy=proxy) as context:
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


def _probe_proxy() -> dict | None:
    """조사 전용 decodo 스티키 프록시 — 자격이 없으면 None(직접 회선만)."""
    try:
        from app.core.decodo import (
            credentials_ready,
            load_settings,
            sticky_proxy_dict,
        )
    except Exception:  # noqa: BLE001 - 모듈 부재는 자격 없음과 같다
        return None
    try:
        if not credentials_ready():
            return None
        return sticky_proxy_dict(load_settings(), PROBE_SESSION_ID)
    except Exception:  # noqa: BLE001 - 자격 읽기 실패도 프록시 불가로
        return None


def _load_cached_volumes(
    cache_path: Path, family: list[tuple[str, str]]
) -> dict[str, int] | None:
    """캐시된 조사 결과 — 같은 가족이고 24시간 이내일 때만."""
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    ids = raw.get("family_ids")
    volumes = raw.get("volumes")
    saved_ts = raw.get("saved_ts")
    if (
        ids != [category_id for category_id, _name in family]
        or not isinstance(volumes, dict)
        or isinstance(saved_ts, bool)
        or not isinstance(saved_ts, (int, float))
        or time.time() - float(saved_ts) > PROBE_CACHE_TTL_SECONDS
    ):
        return None
    usable = {
        str(key): int(value)
        for key, value in volumes.items()
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    }
    return usable or None


def _save_cached_volumes(
    cache_path: Path, family: list[tuple[str, str]], volumes: dict[str, int]
) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "family_ids": [category_id for category_id, _name in family],
        "volumes": volumes,
        "saved_ts": time.time(),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    temporary = cache_path.with_name(f"{cache_path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, cache_path)


def plan_volume_probe(
    family: list[tuple[str, str]],
    browser_scope_factory: Callable | None = None,
    *,
    on_progress: Callable[[str, int, int, int], None] | None = None,
    on_cached: Callable[[], None] | None = None,
    cache_path: Path | None = None,
) -> dict[str, int]:
    """물량 조사 상위 진입 — 캐시 → 직접 회선 → decodo 조사 전용 회선.

    같은 가족을 24시간 이내에 조사했으면 캐시를 재사용한다(on_cached 로
    알린다). 직접 회선이 차단돼 물량을 못 얻으면 decodo 조사 전용
    스티키(sid i990)로 1회 재시도한다. 둘 다 실패하면 빈 사전 —
    호출자(패널)는 라운드로빈 균등 분할로 폴백한다.
    """
    if cache_path is not None:
        cached = _load_cached_volumes(cache_path, family)
        if cached is not None:
            if on_cached is not None:
                on_cached()
            return cached
    volumes = probe_family_volumes(
        family, browser_scope_factory, on_progress=on_progress
    )
    if not volumes:
        proxy = _probe_proxy()
        if proxy is not None:
            volumes = probe_family_volumes(
                family, browser_scope_factory,
                on_progress=on_progress, proxy=proxy,
            )
    if volumes and cache_path is not None:
        _save_cached_volumes(cache_path, family, volumes)
    return volumes
