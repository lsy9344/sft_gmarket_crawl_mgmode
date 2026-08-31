"""쿠팡 차단 상태 영속화·쿨다운 게이트 — Qt 비의존 코어.

Akamai IP 평판 차단은 12~20시간 경과 후 복구되는 실측 근거가 있다
(SEARCH_POC_FINDINGS rev.2: ~12h, rev.24: ~20h). 차단 감지 직후의 재실행은
실패가 확실할뿐 아니라 재시도 자체가 IP 평판을 추가로 깎는다(2026-08-30
배포 PC 실측 — 12:46 차단 후 17:33 재실행, 재차 차단).

이 모듈은 차단 감지 시각을 output_dir 에 JSON 으로 기록하고, 엔진 시작 단계에서
쿨다운 미경과 재실행을 거부한다. 모든 저장은 best-effort — 디스크 오류가
수집 흐름을 막지 않는다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

BLOCK_STATE_FILENAME = "coupang_block_state.json"
# 실측 근거의 하한(12h)을 기본 쿨다운으로 사용한다. 상한은 20h.
DEFAULT_BLOCK_COOLDOWN_HOURS = 12.0
# 시계가 뒤로 간/앞서간 경우의 오차 허용 — blocked_ts 가 미래면 기록 손상으로 본다.
_CLOCK_SKEW_TOLERANCE_SECONDS = 60.0


def block_state_path(output_dir: Path) -> Path:
    return Path(output_dir) / BLOCK_STATE_FILENAME


def read_block_state(output_dir: Path) -> dict | None:
    """기록된 차단 상태를 반환. 없거나 손상됐으면 None."""
    try:
        raw = json.loads(block_state_path(output_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("blocked_ts"), (int, float)):
        return None
    return raw


def record_block(
    output_dir: Path,
    *,
    reason: str = "",
    reference: str = "",
    now: datetime | None = None,
) -> str | None:
    """차단 감지 시각을 기록한다. 반환값은 기록 파일 경로(실패 시 None).

    ``reference`` 는 Akamai Reference # 식별자 — 재발 차단을 IP 단위로
    대조하기 위한 진단 정보다.
    """
    ts = now or datetime.now()  # noqa: DTZ005 - 로컬 naive 시각 사용(기존 캐시·로그 관례 동일)
    payload = {
        "blocked_at": ts.strftime("%Y-%m-%d %H:%M:%S"),
        "blocked_ts": ts.timestamp(),
        "reason": reason or "",
        "reference": reference or "",
    }
    try:
        path = block_state_path(output_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return str(path)
    except OSError:
        return None


def cooldown_remaining_seconds(
    output_dir: Path,
    cooldown_hours: float = DEFAULT_BLOCK_COOLDOWN_HOURS,
    now: datetime | None = None,
) -> float:
    """차단 쿨다운 잔여 초. 기록 없음·만료·손상이면 0.0."""
    state = read_block_state(output_dir)
    if state is None:
        return 0.0
    try:
        blocked_ts = float(state["blocked_ts"])
    except (KeyError, TypeError, ValueError):
        return 0.0
    if blocked_ts <= 0:
        return 0.0
    now_ts = (now or datetime.now()).timestamp()  # noqa: DTZ005 - 위와 동일 관례
    if blocked_ts > now_ts + _CLOCK_SKEW_TOLERANCE_SECONDS:
        return 0.0
    remaining = blocked_ts + cooldown_hours * 3600 - now_ts
    return max(0.0, remaining)


def format_remaining(seconds: float) -> str:
    """잔여 초 → '2시간 15분' 형태의 안내 문자열."""
    total_minutes = max(0, int(seconds // 60))
    hours, minutes = divmod(total_minutes, 60)
    if hours > 0:
        return f"{hours}시간 {minutes}분"
    return f"{minutes}분"
