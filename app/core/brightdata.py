"""Bright Data 계정 설정 — API 토큰·존 입력 저장과 사용량 차감 대상 계정 해석.

2026-09-09 신규 기능. 두 탭이 Bright Data 기술을 쓴다(실측 문서 기준):
- Gmarket 카테고리 탭 — Web Unlocker(`POST /request`, 요청제 과금).
  docs/gmarket/ACCESS_ROUTES_RESEARCH_20260908.md §6
- Coupang 카테고리 탭 — Camoufox + ISP 프록시(대역폭제 과금).
  docs/coupang/BRIGHTDATA_AKAMAI_REVIEW_20260908.md §3·§7

이 모듈이 사용자가 입력한 계정 자격 증명(자신의 키 — 사용량은 그 키에서
차감된다)을 저장소 밖 파일(output/ 는 .gitignore)에 보관하고, 두 엔진이
실행 시점에 읽어가는 단일 해석 지점이다.

토큰 우선순위: 설정 파일(UI 입력) → 환경변수 BRIGHTDATA_API_TOKEN →
기존 output/brightdata_token.txt(개발 환경 하위 호환).

계정 조회 API(실측·공식 문서):
- `GET /customer/balance` → {"balance": N, "pending_balance": N}
  https://docs.brightdata.com/api-reference/account-management-api/Get_total_balance_through_API
- `GET /zone/passwords?zone=<name>` → 존 비밀번호 (2026-09-08 실측,
  ACCESS_ROUTES_RESEARCH §8)
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any

import requests

from app.core import config

SETTINGS_FILENAME = "brightdata_settings.json"

BRIGHTDATA_BALANCE_URL = "https://api.brightdata.com/customer/balance"
BRIGHTDATA_ZONE_PASSWORDS_URL = "https://api.brightdata.com/zone/passwords"

# ISP 프록시 접속 — Bright Data 표준 엔드포인트.
# 사용자명 형식: brd-customer-<계정ID>-zone-<존명> (ACCESS_ROUTES_RESEARCH §8)
DEFAULT_ISP_HOST = "brd.superproxy.io"
DEFAULT_ISP_PORT = 22225

_BALANCE_TIMEOUT = 15
_ZONE_PASSWORD_TIMEOUT = 15


class BrightDataAPIError(RuntimeError):
    """Bright Data 계정 API 호출 실패 (상태 코드·응답 본문 보존)."""

    def __init__(self, message: str, status: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class BrightDataSettings:
    """사용자 입력 Bright Data 계정 설정 (output/brightdata_settings.json)."""

    api_token: str = ""                 # 계정 API 토큰 — 요청 과금 대상 계정의 열쇠
    unlocker_zone: str = ""             # Web Unlocker 존 (빈 값 → config.BRIGHTDATA_ZONE)
    country: str = ""                   # 출발 국가 (빈 값 → config.BRIGHTDATA_COUNTRY)
    isp_enabled: bool = False           # Coupang 카테고리 탭 ISP 프록시 경유 여부
    isp_customer_id: str = ""           # 계정 ID (예: hl_22fb0228 — /status customer)
    isp_zone: str = ""                  # ISP 존명 (예: gm_isp_kr3)
    isp_password: str = ""              # 존 비밀번호 — /zone/passwords 로 조회 가능
    isp_host: str = DEFAULT_ISP_HOST
    isp_port: int = DEFAULT_ISP_PORT
    saved_at: str = ""                  # 마지막 저장 시각 (기록용)

    def with_saved_stamp(self) -> BrightDataSettings:
        """saved_at 을 현재 시각으로 갱신한 복제본 반환."""
        return replace(self, saved_at=datetime.now().astimezone().strftime(
            "%Y-%m-%d %H:%M:%S"))

    def sanitized(self) -> BrightDataSettings:
        """문자열 필드 trim + isp_port 정수화. 저장·조회 전 공통 정규화."""
        port = self.isp_port
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = DEFAULT_ISP_PORT
        if not 1 <= port <= 65535:
            port = DEFAULT_ISP_PORT
        return replace(
            self,
            api_token=str(self.api_token or "").strip(),
            unlocker_zone=str(self.unlocker_zone or "").strip(),
            country=str(self.country or "").strip().lower(),
            isp_customer_id=str(self.isp_customer_id or "").strip(),
            isp_zone=str(self.isp_zone or "").strip(),
            isp_password=str(self.isp_password or "").strip(),
            isp_host=str(self.isp_host or "").strip() or DEFAULT_ISP_HOST,
            isp_port=port,
        )


def default_settings_path() -> str:
    """설정 파일 경로 — output/brightdata_settings.json (저장소 밖)."""
    return str(config.DEFAULT_OUTPUT_DIR / SETTINGS_FILENAME)


def load_settings(path: str | None = None) -> BrightDataSettings:
    """설정 파일 로드. 파일이 없거나 손상됐으면 기본값 — 절대 예외를 던지지 않는다.

    토큰 수집기·UI 기동 경로에서 호출되므로 손상 파일은 기본값으로 폴백한다
    (기존 collected_ids 손상 처리와 달리, 재입력으로 복구 가능한 값이므로).
    """
    fp = path or default_settings_path()
    try:
        raw = json.loads(_read_text(fp))
    except (OSError, ValueError):
        return BrightDataSettings()
    if not isinstance(raw, dict):
        return BrightDataSettings()
    known = {f: raw[f] for f in BrightDataSettings.__dataclass_fields__ if f in raw}
    return BrightDataSettings(**known).sanitized()


def save_settings(settings: BrightDataSettings, path: str | None = None) -> str:
    """설정 저장 (원자적 교체 + POSIX 0600). 저장된 파일 경로를 반환."""
    fp = path or default_settings_path()
    payload = asdict(settings.sanitized().with_saved_stamp())
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    _write_atomic(fp, text)
    return fp


def resolve_api_token(settings: BrightDataSettings | None = None) -> str:
    """요청 과금이 걸릴 토큰 — 설정 파일(UI 입력) → 환경변수 → 기존 토큰 파일."""
    s = settings if settings is not None else load_settings()
    if s.api_token:
        return s.api_token
    env = os.environ.get("BRIGHTDATA_API_TOKEN", "").strip()
    if env:
        return env
    return config.brightdata_api_token()


def resolve_unlocker(
    settings: BrightDataSettings | None = None,
) -> tuple[str, str, str]:
    """Web Unlocker 3요소 — (토큰, 존, 국가). 존·국가는 빈 값이면 기본 상수."""
    s = settings if settings is not None else load_settings()
    token = resolve_api_token(s)
    zone = s.unlocker_zone or config.BRIGHTDATA_ZONE
    country = s.country or config.BRIGHTDATA_COUNTRY
    return token, zone, country


def isp_proxy_dict(settings: BrightDataSettings | None = None) -> dict | None:
    """Camoufox(playwright) `proxy` 인자 — 활성화·자격 완비 시에만 dict, 아니면 None.

    {"server": "http://brd.superproxy.io:22225",
     "username": "brd-customer-<ID>-zone-<존명>", "password": "<존 비밀번호>"}
    """
    s = settings if settings is not None else load_settings()
    if not s.isp_enabled:
        return None
    if not (s.isp_customer_id and s.isp_zone and s.isp_password):
        return None
    return {
        "server": f"http://{s.isp_host}:{s.isp_port}",
        "username": f"brd-customer-{s.isp_customer_id}-zone-{s.isp_zone}",
        "password": s.isp_password,
    }


def isp_proxy_summary(proxy: dict | None) -> str:
    """로그용 프록시 요약 — 비밀번호 노출 없이 사용자명만."""
    if not proxy:
        return ""
    return str(proxy.get("username", ""))


def masked_token(token: str) -> str:
    """토큰 식별용 마지막 4자리 마스킹 — 로그·UI 에 계정 구분 정보로 표시."""
    token = (token or "").strip()
    if len(token) <= 4:
        return "****"
    return f"…{token[-4:]}"


def fetch_balance(
    token: str,
    timeout: float = _BALANCE_TIMEOUT,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """계정 잔액 조회 (GET /customer/balance) — 토큰 검증을 겸한다.

    성공 시 {"balance": float, "pending_balance": float}. 401/403 은
    BrightDataAPIError 로 변환해 UI 가 "토큰 불일치"를 구분해 안내한다.
    """
    body = _account_get(BRIGHTDATA_BALANCE_URL, token, timeout, session)
    try:
        data = json.loads(body)
    except ValueError as e:
        raise BrightDataAPIError("잔액 응답이 JSON 이 아닙니다.", body=body[:200]) from e
    if not isinstance(data, dict) or "balance" not in data:
        raise BrightDataAPIError("잔액 응답 형식이 예상과 다릅니다.", body=body[:200])
    return data


def fetch_zone_password(
    token: str,
    zone: str,
    timeout: float = _ZONE_PASSWORD_TIMEOUT,
    session: requests.Session | None = None,
) -> str:
    """존 비밀번호 조회 (GET /zone/passwords?zone=…) — 2026-09-08 실측 엔드포인트.

    응답 형식 변동에 대비해 관용적으로 파싱한다: JSON 배열이면 첫 항목,
    dict 면 password 키(또는 첫 문자열 값), 평문이면 그대로.
    """
    zone = (zone or "").strip()
    if not zone:
        raise BrightDataAPIError("존 이름이 비어 있습니다.")
    url = f"{BRIGHTDATA_ZONE_PASSWORDS_URL}?zone={zone}"
    body = _account_get(url, token, timeout, session)
    try:
        data = json.loads(body)
    except ValueError:
        text = body.strip()
        if not text:
            raise BrightDataAPIError(f"존 '{zone}' 의 비밀번호 응답이 비어 있습니다.",
                                     body=body[:200])
        return text
    if isinstance(data, list):
        for item in data:
            if isinstance(item, str) and item.strip():
                return item.strip()
    if isinstance(data, dict):
        pw = data.get("password")
        if isinstance(pw, str) and pw.strip():
            return pw.strip()
        for value in data.values():
            if isinstance(value, str) and value.strip():
                return value.strip()
    raise BrightDataAPIError(f"존 '{zone}' 의 비밀번호를 응답에서 찾지 못했습니다.",
                             body=body[:200])


# ── ISP 프록시 연결 테스트 ────────────────────────────────────────────────

# requests 세부 예외 — 최소 스텁 환경(tests/__init__.py)에는 세부 클래스가
# 없으므로 RequestException 으로 폴백한다(분류 정확도는 실제 requests 에서만
# 완전 — 함수가 호출 불가가 되지 않게 하는 방어).
_REQUESTS_CONNECT_TIMEOUT = getattr(requests, "ConnectTimeout", requests.RequestException)
_REQUESTS_TIMEOUT = getattr(requests, "Timeout", requests.RequestException)
_REQUESTS_PROXY_ERROR = getattr(requests, "ProxyError", requests.RequestException)
_REQUESTS_CONNECTION_ERROR = getattr(requests, "ConnectionError", requests.RequestException)

# 프록시 경유로 출발 IP 를 확인하는 공개 엔드포인트. 요청 1건 수 KB 로 ISP 존
# 대역폭(과금 구간)이 아주 조금 소비된다 — 자격 3요소 검증 비용으로 납득 가능.
ISP_TEST_URL = "http://lumtest.com/myip.json"           # Bright Data 제공 IP 확인
ISP_TEST_FALLBACK_URL = "https://api.ipify.org/?format=json"
ISP_TEST_TIMEOUT = 20.0


def test_isp_proxy(
    settings: BrightDataSettings | None = None,
    timeout: float = ISP_TEST_TIMEOUT,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """ISP 프록시 자격(계정 ID·존·비밀번호)을 실제 연결로 검증한다.

    잔액 조회가 계정 API 토큰만 검증하는 것과 달리, 프록시 3요소가 모두
    맞아야 통과한다 — Coupang 탭에서 "불완비로 직접 접속" 침묵 폴백을 막는
    사전 확인 수단(2026-09-09 검토 반영). 성공 시 {"ip", "country"} —
    country 는 1차 엔드포인트(lumtest)가 줄 때만 채워진다.
    """
    proxy = isp_proxy_dict(settings)
    if not proxy:
        raise BrightDataAPIError(
            "ISP 프록시가 비활성화되었거나 계정 ID/존/비밀번호가 비어 있습니다 — "
            "먼저 입력한 뒤 테스트하세요.")
    proxies = {"http": proxy["server"], "https": proxy["server"]}
    auth = (proxy["username"], proxy["password"])
    owned = session is None
    sess = session or requests.Session()
    try:
        try:
            r = sess.get(ISP_TEST_URL, proxies=proxies, auth=auth, timeout=timeout)
        except _REQUESTS_CONNECT_TIMEOUT as e:
            raise BrightDataAPIError(
                "프록시 접속 시간 초과 — 호스트(brd.superproxy.io)·포트·네트워크를 "
                "확인하세요.") from e
        except _REQUESTS_TIMEOUT as e:
            fallback = _isp_test_fallback(sess, proxies, auth, timeout)
            if fallback is not None:
                return fallback
            raise BrightDataAPIError(
                f"프록시 응답 시간 초과({timeout:.0f}초) — 존 상태(IP 할당 여부)를 "
                "확인하세요.") from e
        except (_REQUESTS_PROXY_ERROR, _REQUESTS_CONNECTION_ERROR) as e:
            fallback = _isp_test_fallback(sess, proxies, auth, timeout)
            if fallback is not None:
                return fallback
            raise BrightDataAPIError(
                f"프록시 연결 실패 — 계정 ID·존·비밀번호와 호스트/포트를 "
                f"확인하세요: {e}") from e
        except requests.RequestException as e:
            raise BrightDataAPIError(f"프록시 테스트 실패: {e}") from e

        if r.status_code == 407:
            raise BrightDataAPIError(
                "프록시 인증 실패(HTTP 407) — 계정 ID·ISP 존·비밀번호가 "
                "일치하지 않습니다.", status=407)
        try:
            r.raise_for_status()
            data = json.loads(r.text or "{}")
        except requests.RequestException as e:
            raise BrightDataAPIError(f"IP 확인 요청 실패: {e}") from e
        except ValueError as e:
            raise BrightDataAPIError(
                "IP 확인 응답이 JSON 이 아닙니다.", body=(r.text or "")[:200]) from e
        if isinstance(data, dict) and data.get("ip"):
            return {"ip": str(data["ip"]),
                    "country": str(data.get("country", "") or "")}
        raise BrightDataAPIError(
            "IP 확인 응답 형식이 예상과 다릅니다.", body=(r.text or "")[:200])
    finally:
        if owned:
            sess.close()


def _isp_test_fallback(
    sess: requests.Session,
    proxies: dict[str, str],
    auth: tuple[str, str],
    timeout: float,
) -> dict[str, Any] | None:
    """1차 엔드포인트가 네트워크 오류일 때 폴백으로 IP 만 확인한다. 실패 시 None."""
    try:
        r = sess.get(ISP_TEST_FALLBACK_URL, proxies=proxies, auth=auth,
                     timeout=timeout)
        r.raise_for_status()
        data = json.loads(r.text or "{}")
    except (requests.RequestException, ValueError):
        return None
    if isinstance(data, dict) and data.get("ip"):
        return {"ip": str(data["ip"]), "country": ""}
    return None


def _account_get(
    url: str, token: str, timeout: float,
    session: requests.Session | None,
) -> str:
    """Bearer 인증 GET 공통 — 200 이외는 BrightDataAPIError."""
    if not (token or "").strip():
        raise BrightDataAPIError("Bright Data API 토큰이 설정되지 않았습니다.")
    owned = session is None
    sess = session or requests.Session()
    try:
        r = sess.get(
            url,
            headers={"Authorization": f"Bearer {token.strip()}"},
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise BrightDataAPIError(f"Bright Data API 연결 실패: {e}") from e
    finally:
        if owned:
            sess.close()
    if r.status_code in (401, 403):
        raise BrightDataAPIError(
            f"토큰이 거부됐습니다 (HTTP {r.status_code}) — 계정 설정의 API 토큰을 확인하세요.",
            status=r.status_code, body=(r.text or "")[:200],
        )
    if r.status_code != 200:
        raise BrightDataAPIError(
            f"Bright Data API 오류 (HTTP {r.status_code})",
            status=r.status_code, body=(r.text or "")[:200],
        )
    return r.text or ""


# ── 파일 I/O 헬퍼 (테스트 대체 가능한 최소 경계) ─────────────────────────


def _read_text(fp: str) -> str:
    with open(fp, encoding="utf-8") as f:
        return f.read()


def _write_atomic(fp: str, text: str) -> None:
    """동일 폴더 임시 파일 쓰기 → 치환. POSIX 에서는 0600 으로 잠근다."""
    import tempfile
    from pathlib import Path

    directory = Path(fp).parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".brightdata_")
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
