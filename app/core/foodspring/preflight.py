"""Foodspring 런타임 preflight 검증.

암묵적 다운로드를 유발하지 않는 검사만 수행한다:
- 필수 패키지(scrapling.fetchers, requests, openpyxl) import 가능 여부
- 대상 사이트 네트워크 도달성 (짧은 타임아웃 1회, 본문 다운로드 없음)
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from enum import Enum

LIST_URL = "https://www.foodspring.co.kr/special/wcpd"


class PreflightStatus(Enum):
    OK = "ok"
    PACKAGE_MISSING = "package_missing"
    NETWORK_UNREACHABLE = "network_unreachable"
    RUNTIME_MISSING = "runtime_missing"


@dataclass
class PreflightResult:
    status: PreflightStatus
    message: str


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def check_runtime() -> PreflightResult:
    missing = []
    for mod in ("scrapling", "scrapling.fetchers", "requests", "openpyxl"):
        if not _module_available(mod):
            missing.append(mod)
    if missing:
        return PreflightResult(
            PreflightStatus.PACKAGE_MISSING,
            "필수 패키지가 없습니다:\n  " + "\n  ".join(missing)
            + "\n\nrequirements.txt 를 설치해주세요: pip install -r requirements.txt",
        )

    # 세션 확보에 사용하는 Patchright Chromium 런타임 검사
    # (Gmarket 사전조사와 동일 런타임을 재사용)
    try:
        from app.core.gmarket_preflight import check_gmarket_runtime

        gr = check_gmarket_runtime()
        if not gr.ok:
            return PreflightResult(
                PreflightStatus.RUNTIME_MISSING,
                "Foodspring 세션에 필요한 Chromium 런타임이 없습니다.\n" + gr.message,
            )
    except ImportError:
        return PreflightResult(
            PreflightStatus.PACKAGE_MISSING,
            "gmarket_preflight 모듈을 불러올 수 없습니다. 설치 상태를 확인하세요.",
        )
    return PreflightResult(PreflightStatus.OK, "준비됨")


def check_network(timeout: float = 8.0) -> PreflightResult:
    import requests

    try:
        resp = requests.get(
            LIST_URL,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=timeout,
            allow_redirects=True,
        )
    except Exception as e:  # noqa: BLE001
        return PreflightResult(
            PreflightStatus.NETWORK_UNREACHABLE,
            f"foodspring.co.kr 에 접속할 수 없습니다:\n{e}",
        )
    if resp.status_code != 200:
        return PreflightResult(
            PreflightStatus.NETWORK_UNREACHABLE,
            f"foodspring.co.kr 이 비정상 응답을 반환했습니다 (HTTP {resp.status_code}).",
        )
    return PreflightResult(PreflightStatus.OK, "준비됨")


def check_all(timeout: float = 8.0) -> PreflightResult:
    result = check_runtime()
    if result.status != PreflightStatus.OK:
        return result
    return check_network(timeout=timeout)