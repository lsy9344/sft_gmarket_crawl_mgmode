"""테스트 패키지 초기화: 프로젝트 루트를 sys.path 에 추가하고,
requests/bs4 가 설치되어 있지 않은 환경에서만 최소 스텁을 등록한다.

PyQt6/scrapling 은 여기서 다루지 않는다 — core 계층(모델/설정/저장/엔진)은
Qt·브라우저에 의존하지 않으므로 순수 로직만 테스트한다(workers/ui 는 제외).

실제 requests/bs4 가 설치된 환경(예: 실 배포/향후 CI)에서는 스텁이 아니라
진짜 모듈을 그대로 사용한다 — 테스트가 SellerCrawler.fetch_seller_info 를
monkeypatch 해서 내부 HTTP 호출 자체를 우회하므로 어느 쪽이든 안전하다.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _install_requests_stub() -> None:
    mod = types.ModuleType("requests")

    class _RequestException(Exception):
        pass

    class _Session:
        def __init__(self) -> None:
            self.headers: dict = {}

        def get(self, *args, **kwargs):
            raise RuntimeError("stub requests.Session.get — 테스트에서 monkeypatch 필요")

        def close(self) -> None:
            pass

    mod.Session = _Session
    mod.RequestException = _RequestException
    sys.modules["requests"] = mod


def _install_bs4_stub() -> None:
    mod = types.ModuleType("bs4")

    class _BeautifulSoup:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def select_one(self, *args, **kwargs):
            return None

        def select(self, *args, **kwargs):
            return []

    mod.BeautifulSoup = _BeautifulSoup
    sys.modules["bs4"] = mod


try:
    import requests  # noqa: F401
except ImportError:
    _install_requests_stub()

try:
    import bs4  # noqa: F401
except ImportError:
    _install_bs4_stub()
