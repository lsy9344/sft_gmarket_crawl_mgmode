"""공통 유틸리티 (파일명 치환, 시간 포맷, goodscode 추출)."""

from __future__ import annotations

import re

from app.core import config

_INVALID_FILENAME = re.compile(r'[/\\:*?"<>|]')
_GOODSCODE_RE = re.compile(r"goodscode=(\d+)")


def sanitize_filename(name: str) -> str:
    """Windows 파일명 금지문자(/ \\ : * ? " < > |)를 _ 로 치환 (WORK_ORDER §9.1 / §15)."""
    return _INVALID_FILENAME.sub("_", name).strip()


def extract_goodscodes(html: str) -> list[str]:
    """리스팅 HTML 에서 goodscode 추출. 순서 유지 중복 제거 (WORK_ORDER §4.3)."""
    if not html:
        return []
    codes = _GOODSCODE_RE.findall(html)
    return list(dict.fromkeys(codes))


def contains_bot_challenge(html: str) -> bool:
    """리스팅 HTML 에 봇/캡차 감지 키워드가 포함되어 있으면 True (§6 차단 대응)."""
    if not html:
        return False
    return any(kw in html for kw in config.BOT_KEYWORDS)


def estimate_seconds(new_count: int, seconds_per_item: float = config.SECONDS_PER_ITEM) -> float:
    """신규 건수 × 건당 소요시간 → 예상 총 소요 시간(초) (WORK_ORDER §3.2 / §11)."""
    return max(0.0, new_count) * seconds_per_item


def format_duration(seconds: float) -> str:
    """초 → 사람이 읽는 한글 형식. 예: 843 → '14분 3초', 3661 → '1시간 1분 1초'."""
    total = round(max(0.0, seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}시간")
    if minutes:
        parts.append(f"{minutes}분")
    # 초는 시간 단독이 아닌 이상 항상 표시(0초여도 '0초')
    if secs or not parts:
        parts.append(f"{secs}초")
    return " ".join(parts)


def format_clock(seconds: float) -> str:
    """초 → MM:SS 또는 HH:MM:SS (진행 패널 경과/남은시간 표시용)."""
    total = round(max(0.0, seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
