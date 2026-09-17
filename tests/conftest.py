"""테스트 공통 설정.

AliExpress 크롤러 테스트는 회선 확인(fetch_exit_ip)·프록시 조회를 실제
네트워크/실제 자격으로 수행하지 않도록 스텁으로 고정한다. 개발 PC 에 실제
Decodo 자격이 저장돼 있어도 테스트가 절대 외부로 나가지 않게 하는 것이 목적이다.
"""

from __future__ import annotations

import pytest

# 크롤러를 직접/워커로 돌리는 테스트 모듈 — 프록시 관련 스텁이 필요하다.
_CRAWLER_TEST_MODULES = {
    "test_aliexpress_category_e2e",
    "test_adversarial_resume_resilience",
    "test_adversarial_challenger_2",
}


@pytest.fixture(autouse=True)
def _aliexpress_offline_proxy_stub(request, monkeypatch):
    module_name = request.module.__name__.rsplit(".", 1)[-1]
    if module_name not in _CRAWLER_TEST_MODULES:
        yield
        return

    from app.core import decodo

    def fake_sticky_proxy(settings=None, session_id=""):
        return {
            "server": "http://gate.decodo.com:7000",
            "username": (
                f"user-testuser-session-{session_id or 't1'}"
                "-sessionduration-1440-country-kr"
            ),
            "password": "test-password",
        }

    def fake_fetch_exit_ip(proxy, **kwargs):
        return decodo.ExitIpInfo(
            ip="203.0.113.10", country_code="KR", country_name="South Korea",
        )

    monkeypatch.setattr(decodo, "sticky_proxy_dict", fake_sticky_proxy)
    monkeypatch.setattr(decodo, "fetch_exit_ip", fake_fetch_exit_ip)
    yield
