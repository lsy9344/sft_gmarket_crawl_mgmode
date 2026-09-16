"""Decodo 주거용 프록시 계정 — Coupang 카테고리 탭 전용.

성공 실측(2026-09-10~11, docs/coupang/DECODO_PROXY_METHODOLOGY_20260910.md):
한국 고정 세션(스티키 24h) + 카테고리 1개 실행 + 실패 시 세션 교체 재시도.
설정 탭에 입력한 사용자명/비밀번호로 그 계정 회선을 쓴다.

저장: output/decodo_settings.json (.gitignore). 토큰 우선순위는 설정 파일만.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from app.core import config

SETTINGS_FILENAME = "decodo_settings.json"
DEFAULT_HOST = "gate.decodo.com"
DEFAULT_PORT = 7000
DEFAULT_COUNTRY = "kr"
STICKY_DURATION_MIN = 1440  # 24h — 실측 성공 조합
EXIT_IP_URL = "http://ip.decodo.com/json"
EXIT_IP_TIMEOUT = 20.0
_KOREA_CODES = frozenset({"kr", "kor"})

_SESSION_ID_SAFE = re.compile(r"[^A-Za-z0-9]+")


@dataclass
class DecodoSettings:
    """사용자 입력 Decodo 자격 (output/decodo_settings.json)."""

    username: str = ""          # 계정 ID (예: sp3lqmo64w). user- 접두사는 저장 시 제거.
    password: str = ""
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    country: str = DEFAULT_COUNTRY
    saved_at: str = ""

    def with_saved_stamp(self) -> DecodoSettings:
        return replace(
            self,
            saved_at=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        )

    def sanitized(self) -> DecodoSettings:
        port = self.port
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = DEFAULT_PORT
        if not 1 <= port <= 65535:
            port = DEFAULT_PORT
        country = str(self.country or "").strip().lower() or DEFAULT_COUNTRY
        return replace(
            self,
            username=normalize_user_id(self.username),
            password=str(self.password or "").strip(),
            host=str(self.host or "").strip() or DEFAULT_HOST,
            port=port,
            country=country[:2] if country else DEFAULT_COUNTRY,
        )


def normalize_user_id(raw: str) -> str:
    """대시보드 사용자명 → 계정 ID.

    붙여넣기 변형을 흡수한다:
    - ``user-abc`` → ``abc``
    - 스티키 전체 문자열 ``user-abc-session-...`` → ``abc``
    """
    s = str(raw or "").strip()
    if s.lower().startswith("user-"):
        s = s[5:]
    if "-session-" in s:
        s = s.split("-session-", 1)[0]
    return s.strip()


def credentials_ready(settings: DecodoSettings | None = None) -> bool:
    s = settings if settings is not None else load_settings()
    s = s.sanitized()
    return bool(s.username and s.password)


def default_settings_path() -> str:
    return str(config.DEFAULT_OUTPUT_DIR / SETTINGS_FILENAME)


def load_settings(path: str | None = None) -> DecodoSettings:
    fp = path or default_settings_path()
    try:
        raw = json.loads(_read_text(fp))
    except (OSError, ValueError):
        return DecodoSettings()
    if not isinstance(raw, dict):
        return DecodoSettings()
    known = {f: raw[f] for f in DecodoSettings.__dataclass_fields__ if f in raw}
    return DecodoSettings(**known).sanitized()


def save_settings(settings: DecodoSettings, path: str | None = None) -> str:
    fp = path or default_settings_path()
    payload = asdict(settings.sanitized().with_saved_stamp())
    _write_atomic(fp, json.dumps(payload, ensure_ascii=False, indent=2))
    return fp


def make_session_id(attempt: int, now_ts: float | None = None) -> str:
    """스티키 세션 ID — 시도마다 달라야 새 회선이 할당된다."""
    import time

    ts = int(now_ts if now_ts is not None else time.time()) % 1_000_000
    raw = f"t{int(attempt)}{ts}"
    return _SESSION_ID_SAFE.sub("", raw) or f"t{int(attempt)}"


def sticky_proxy_dict(
    settings: DecodoSettings | None = None,
    session_id: str = "",
) -> dict | None:
    """Camoufox/playwright ``proxy`` 인자. 자격 미완비면 None.

    사용자명 형식(실측 확정):
    ``user-<id>-session-<sid>-sessionduration-1440-country-kr``
    """
    s = (settings if settings is not None else load_settings()).sanitized()
    if not (s.username and s.password):
        return None
    sid = _SESSION_ID_SAFE.sub("", str(session_id or "").strip()) or make_session_id(1)
    username = (
        f"user-{s.username}-session-{sid}"
        f"-sessionduration-{STICKY_DURATION_MIN}-country-{s.country}"
    )
    return {
        "server": f"http://{s.host}:{s.port}",
        "username": username,
        "password": s.password,
    }


def proxy_summary(proxy: dict | None) -> str:
    """로그용 — 비밀번호 없이 사용자명만."""
    if not proxy:
        return ""
    return str(proxy.get("username", ""))


class DecodoError(RuntimeError):
    """Decodo 회선 확인·연결 실패."""


@dataclass(frozen=True)
class ExitIpInfo:
    """ip.decodo.com/json 파싱 결과 — 실제 출발 국가 판정용."""

    ip: str = ""
    country_code: str = ""
    country_name: str = ""

    @property
    def is_korea(self) -> bool:
        code = self.country_code.strip().lower()
        if code in _KOREA_CODES:
            return True
        name = self.country_name.strip().lower()
        return "korea" in name and "north" not in name


def parse_exit_ip(payload: Any) -> ExitIpInfo:
    """Decodo IP JSON → ExitIpInfo. 국가/IP가 없으면 DecodoError."""
    if not isinstance(payload, dict):
        raise DecodoError("회선 확인 응답이 객체가 아닙니다")
    ip = ""
    proxy = payload.get("proxy")
    if isinstance(proxy, dict):
        ip = str(proxy.get("ip") or "").strip()
    if not ip:
        ip = str(payload.get("ip") or "").strip()
    country = payload.get("country")
    code = ""
    name = ""
    if isinstance(country, dict):
        code = str(country.get("code") or "").strip()
        name = str(country.get("name") or "").strip()
    elif isinstance(country, str):
        raw = country.strip()
        if len(raw) <= 3:
            code = raw
        else:
            name = raw
    if not code:
        code = str(
            payload.get("country_code") or payload.get("countryCode") or ""
        ).strip()
    if not ip and not (code or name):
        raise DecodoError("회선 확인 응답에 IP/국가가 없습니다")
    return ExitIpInfo(ip=ip, country_code=code, country_name=name)


def proxy_url(proxy: dict) -> str:
    """프록시 인증을 게이트 주소에 넣는다.

    requests 의 ``auth=`` 는 대상 사이트(Authorization)로 가고,
    프록시(Proxy-Authorization)로는 가지 않는다. 회선 확인이 407 이 되지
    않으려면 사용자명/비밀번호를 프록시 URL 에 넣어야 한다.
    """
    server = str((proxy or {}).get("server") or "").strip()
    if not server:
        return ""
    if "://" in server:
        scheme, rest = server.split("://", 1)
    else:
        scheme, rest = "http", server
    user = quote(str(proxy.get("username") or ""), safe="")
    password = quote(str(proxy.get("password") or ""), safe="")
    if user or password:
        return f"{scheme}://{user}:{password}@{rest}"
    return f"{scheme}://{rest}"


def fetch_exit_ip(
    proxy: dict,
    *,
    timeout: float = EXIT_IP_TIMEOUT,
    session: requests.Session | None = None,
) -> ExitIpInfo:
    """스티키 세션으로 ip.decodo.com/json 을 읽어 실제 출발 국가를 확인한다."""
    url = proxy_url(proxy)
    if not url:
        raise DecodoError("프록시 정보가 없습니다")
    proxies = {"http": url, "https": url}
    owned = session is None
    sess = session or requests.Session()
    try:
        try:
            response = sess.get(
                EXIT_IP_URL, proxies=proxies, timeout=timeout,
            )
        except requests.RequestException as e:
            raise DecodoError(f"회선 확인 요청 실패: {e}") from e
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 407:
            raise DecodoError(
                "프록시 인증 실패(HTTP 407) — Decodo 사용자명/비밀번호를 확인하세요"
            )
        raise_for_status = getattr(response, "raise_for_status", None)
        if raise_for_status is not None:
            try:
                raise_for_status()
            except requests.RequestException as e:
                raise DecodoError(f"회선 확인 응답 오류: {e}") from e
        text = getattr(response, "text", None)
        if text is None:
            raw = getattr(response, "content", b"")
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        try:
            payload = json.loads(text or "")
        except ValueError as e:
            raise DecodoError("회선 확인 응답이 JSON이 아닙니다") from e
        return parse_exit_ip(payload)
    finally:
        if owned:
            closer = getattr(sess, "close", None)
            if closer is not None:
                closer()


def masked_secret(value: str) -> str:
    text = str(value or "")
    if len(text) <= 4:
        return "…" if text else ""
    return f"…{text[-4:]}"


def _read_text(fp: str) -> str:
    with open(fp, encoding="utf-8") as f:
        return f.read()


def _write_atomic(fp: str, text: str) -> None:
    directory = Path(fp).parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".decodo_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, fp)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
