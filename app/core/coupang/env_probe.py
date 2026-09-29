"""쿠팡 수집 직전 환경 사전점검 — 운영 규율(2026-09-29)의 앱 내 구현.

브라우저 홈 웜업 403 하나를 확인하는 데 프록시 회선 1개·Chromium 프로필
1개·실행 10여 초가 든다. '새 Decodo 회선 전면 차단'(층3) 상태에서는 재시도
3회가 전부 실패해 회선 평판만 깎는다 — 2026-09-28 12:58·2026-09-29 08:28
실측(재시도 위치 성공 0/8). 브라우저·프로필을 만들기 전에 curl_cffi
(Chrome 계열 지문) 단일 요청 2개 — 집 회선 직접 1회 + 새 Decodo 회선
1회 — 로 홈 접근을 확인한다. 수동 절차(환경 점검 후 막혀 있으면 그날은
접음)와 동등하다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from curl_cffi import requests as curl_requests

from app.core.decodo import proxy_url

PROBE_URL = "https://www.coupang.com/"
PROBE_TIMEOUT_SECONDS = 15.0
# curl_cffi 0.15.0 이 지원하는 가장 최신 Chrome 지문. 층1(구형 Firefox
# ClientHello 차단) 교훈 — 지문은 '현행 정식 브라우저 코호트'를 따른다.
CHROME_IMPERSONATE = "chrome146"

ProbeFn = Callable[[dict | None], "EnvProbeResult"]


@dataclass(frozen=True)
class EnvProbeResult:
    """상태 코드. None 은 요청 자체 실패(타임아웃 등)로 '불명'이다."""

    direct_status: int | None = None
    proxy_status: int | None = None
    direct_error: str = ""
    proxy_error: str = ""

    @property
    def direct_blocked(self) -> bool:
        return self.direct_status == 403

    @property
    def proxy_blocked(self) -> bool:
        return self.proxy_status == 403


def probe_environment(
    proxy: dict | None,
    *,
    url: str = PROBE_URL,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    direct: bool = True,
) -> EnvProbeResult:
    """홈에 Chrome 지문 GET 1회(직접) + 1회(프록시). 차단 판정은 403만."""
    direct_status: int | None = None
    direct_error = ""
    if direct:
        try:
            response = curl_requests.get(
                url, timeout=timeout, impersonate=CHROME_IMPERSONATE,
            )
            direct_status = int(response.status_code)
        except Exception as e:  # noqa: BLE001 - 점검 실패는 '불명'으로 진행
            direct_error = f"{type(e).__name__}: {e}"

    proxy_status: int | None = None
    proxy_error = ""
    if proxy:
        try:
            response = curl_requests.get(
                url,
                proxies={"http": proxy_url(proxy), "https": proxy_url(proxy)},
                timeout=timeout,
                impersonate=CHROME_IMPERSONATE,
            )
            proxy_status = int(response.status_code)
        except Exception as e:  # noqa: BLE001
            proxy_error = f"{type(e).__name__}: {e}"

    return EnvProbeResult(
        direct_status=direct_status,
        proxy_status=proxy_status,
        direct_error=direct_error,
        proxy_error=proxy_error,
    )
