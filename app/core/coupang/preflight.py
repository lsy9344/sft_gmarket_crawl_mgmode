"""Coupang 런타임 preflight 검증 (WORK_ORDER §11.2, AC-23).

암묵적 다운로드를 유발하지 않고 파일시스템 검사만으로 준비 상태를 판정한다.
Camoufox 0.5.4 실제 캐시 구조:
  INSTALL_DIR (~/.cache/camoufox)
    config.json          — {"active_version": "browsers/official/<ver>-<sha8>"}
                            (channel/pinned 키는 pinned fetch 후 남지 않는다 — 2026-07-27 실측)
    browsers/official/<version>/version.json   — {"version": "152.0.4", "build": "beta.28", ...}
    browsers/official/<version>/camoufox       — 실행 파일
    geoip/config.yml     — "name: MaxMind GeoLite2"
    geoip/mmdb/maxmind geolite2-ipv4.mmdb

모든 검사는 read-only — Camoufox API를 호출하지 않는다(설정 변경 가능성 차단).
"""

from __future__ import annotations

import json
import os
import platform
import time
from dataclasses import dataclass
from enum import Enum
from importlib import metadata
from pathlib import Path

PINNED_CAMOUFOX_VERSION = "0.5.4"
PINNED_BROWSER_CHANNEL = "official"
PINNED_BROWSER_VERSION = "152.0.4-beta.28"
GEOIP_MAX_AGE_DAYS = 30

_LAUNCH_FILE = {
    "Windows": "camoufox.exe",
    "Darwin": "Camoufox.app/Contents/MacOS/camoufox",
    "Linux": "camoufox-bin",
}


class PreflightStatus(Enum):
    OK = "ok"
    PACKAGE_MISSING = "package_missing"
    PACKAGE_VERSION_MISMATCH = "package_version_mismatch"
    BROWSER_MISSING = "browser_missing"
    BROWSER_VERSION_MISMATCH = "browser_version_mismatch"
    GEOIP_MISSING = "geoip_missing"
    GEOIP_STALE = "geoip_stale"


@dataclass
class PreflightResult:
    status: PreflightStatus
    message: str


INSTALL_GUIDE = (
    "설치 방법:\n"
    "  배포 exe: SellerCollector.exe --setup-runtime\n"
    "  설치 확인: SellerCollector.exe --verify-runtime\n"
    "  배포 패키지: 같은 폴더의 CoupangRuntimeSetup.exe 를 1회 실행 (Windows)\n"
    "  소스 실행: python scripts/setup_coupang_runtime.py\n"
    "  수동 설치: python -m camoufox sync\n"
    f"             python -m camoufox fetch official/stable/{PINNED_BROWSER_VERSION}\n"
    f"             python -m camoufox set official/stable/{PINNED_BROWSER_VERSION}\n"
    "             patchright install chromium\n"
    "  다른 PC 로 런타임 캐시를 복사해도 되지만 GeoIP DB 가 30일 이내여야 합니다."
)


def _get_install_dir() -> Path | None:
    """Camoufox 0.5.4 INSTALL_DIR(브라우저 캐시)을 가져온다."""
    try:
        from camoufox.pkgman import INSTALL_DIR  # type: ignore
        return Path(INSTALL_DIR)
    except (ImportError, AttributeError):
        pass
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA", "")
        if base:
            return Path(base) / "camoufox"
    elif system == "Darwin":
        return Path.home() / "Library" / "Caches" / "camoufox"
    else:
        xdg = os.environ.get("XDG_CACHE_HOME", "")
        if xdg:
            return Path(xdg) / "camoufox"
        return Path.home() / ".cache" / "camoufox"
    return None


def _get_camoufox_version() -> str | None:
    """camoufox 패키지 버전 문자열을 가져온다."""
    try:
        return metadata.version("camoufox")
    except (metadata.PackageNotFoundError, OSError, ValueError):
        pass
    try:
        import camoufox
        ver = getattr(camoufox, "__version__", None)
        if isinstance(ver, str):
            return ver
    except (ImportError, OSError):
        pass
    return None


def _check_package_version() -> PreflightResult | None:
    """camoufox 패키지 버전이 pinned과 일치하는지 확인.

    Fail-closed: 버전을 읽지 못하면 불일치로 처리한다.
    """
    try:
        import camoufox  # noqa: F401
    except ImportError:
        return PreflightResult(
            PreflightStatus.PACKAGE_MISSING,
            f"Camoufox 패키지가 설치되지 않았습니다.\n{INSTALL_GUIDE}",
        )
    except OSError as e:
        # camoufox 0.5.4 import 체인(camoufox → async_api → utils →
        # geolocation → import maxminddb)에서 maxminddb 네이티브 라이브러리
        # 로드가 실패하면 OSError 가 난다. geolocation.py 는 ImportError 만
        # 처리하므로 이 OSError 는 첫 `import camoufox` 로 그대로 전파된다.
        # 잡지 않으면 UI 슬롯 예외/설치 도구 traceback 으로 새어 나가 AC-23
        # fail-closed 계약을 깬다 — 구조화된 결과로 변환한다.
        return PreflightResult(
            PreflightStatus.PACKAGE_VERSION_MISMATCH,
            f"Camoufox/GeoIP 네이티브 라이브러리 로드 실패: {e}\n"
            "설치: pip install camoufox[geoip]==0.5.4\n"
            f"{INSTALL_GUIDE}",
        )
    ver = _get_camoufox_version()
    if ver is None:
        return PreflightResult(
            PreflightStatus.PACKAGE_VERSION_MISMATCH,
            f"Camoufox 버전을 확인할 수 없습니다 "
            f"(요구: {PINNED_CAMOUFOX_VERSION}).\n"
            "PyInstaller 빌드 시 metadata 포함을 확인하세요.\n"
            f"{INSTALL_GUIDE}",
        )
    if ver != PINNED_CAMOUFOX_VERSION:
        return PreflightResult(
            PreflightStatus.PACKAGE_VERSION_MISMATCH,
            f"Camoufox 버전 불일치: {ver} (요구: {PINNED_CAMOUFOX_VERSION}).\n{INSTALL_GUIDE}",
        )
    try:
        import maxminddb  # noqa: F401
    except (ImportError, OSError) as e:
        return PreflightResult(
            PreflightStatus.PACKAGE_VERSION_MISMATCH,
            f"GeoIP extra(maxminddb)를 로드할 수 없습니다: {e}\n"
            "설치: pip install camoufox[geoip]==0.5.4\n"
            f"{INSTALL_GUIDE}",
        )
    return None


def _read_config_json(install_dir: Path) -> dict | None:
    """config.json을 읽어 활성 브라우저 설정을 반환."""
    config_path = install_dir / "config.json"
    if not config_path.is_file():
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return None


def _validate_version_json(version_dir: Path) -> str | None:
    """version.json을 파싱하여 'version-build' 문자열을 반환.

    필수 키(version, build)가 없으면 None.
    """
    vj = version_dir / "version.json"
    if not vj.is_file():
        return None
    try:
        with open(vj, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    build = data.get("build")
    if not version or not build:
        return None
    return f"{version}-{build}"


def _has_executable(version_dir: Path) -> bool:
    """현재 OS의 정확한 실행 파일 하나만 검사 (Camoufox 0.5.4 LAUNCH_FILE 계약)."""
    system = platform.system()
    launch = _LAUNCH_FILE.get(system)
    if launch is None:
        return False
    exe = version_dir / launch
    if not exe.is_file():
        return False
    if system == "Windows":
        return True
    return os.access(exe, os.X_OK)


def _version_matches(version_str: str, pinned: str) -> bool:
    """정확한 버전 일치 확인 — 완전 일치만 허용."""
    return version_str == pinned


def _find_browser_version(install_dir: Path) -> str | None:
    """config.json의 active_version으로 활성 브라우저 버전을 확인.

    우선순위: config.json active_version → pinned 매칭 → 디렉터리 검색.
    version.json 파싱 + 실행 파일 존재를 모두 검증한다.
    """
    config = _read_config_json(install_dir)
    if config:
        active = config.get("active_version", "")
        if active:
            active_path = install_dir / active
            if active_path.is_dir():
                ver = _validate_version_json(active_path)
                if ver and _has_executable(active_path):
                    return active_path.name
    browsers_dir = install_dir / "browsers" / PINNED_BROWSER_CHANNEL
    if not browsers_dir.is_dir():
        return None
    for version_dir in sorted(browsers_dir.iterdir()):
        if not version_dir.is_dir():
            continue
        ver = _validate_version_json(version_dir)
        if ver and _version_matches(ver, PINNED_BROWSER_VERSION) and _has_executable(version_dir):
            return version_dir.name
    return None


def _diagnostic_scan(install_dir: Path) -> str:
    """진단용 디렉터리 스캔 — 오류 메시지에만 사용."""
    browsers_dir = install_dir / "browsers"
    if not browsers_dir.is_dir():
        return "browsers/ 디렉터리 없음"
    channels = [d.name for d in browsers_dir.iterdir() if d.is_dir()]
    if not channels:
        return "browsers/ 내부 채널 없음"
    found = []
    for ch in channels:
        ch_dir = browsers_dir / ch
        for vd in sorted(ch_dir.iterdir()):
            if vd.is_dir():
                found.append(f"{ch}/{vd.name}")
    return f"발견된 버전: {found}"


def _check_browser(install_dir: Path) -> PreflightResult | None:
    """브라우저 검증 (fail-closed): config.json active_version + version.json + 실행 파일.

    버전 고정은 active_version 경로 prefix 와 version.json(version/build) 로 강제한다.
    config.json 의 channel/pinned 키는 검사하지 않는다(아래 주석 참조).
    """
    config = _read_config_json(install_dir)
    if config is None:
        diag = _diagnostic_scan(install_dir)
        return PreflightResult(
            PreflightStatus.BROWSER_MISSING,
            f"config.json이 없거나 손상되었습니다. ({diag})\n{INSTALL_GUIDE}",
        )

    active = config.get("active_version", "")
    if not isinstance(active, str) or not active:
        diag = _diagnostic_scan(install_dir)
        return PreflightResult(
            PreflightStatus.BROWSER_MISSING,
            f"config.json active_version이 비어 있거나 문자열이 아닙니다. ({diag})\n{INSTALL_GUIDE}",
        )

    # config.json 의 channel/pinned 키는 검사하지 않는다: Camoufox 0.5.4 `fetch` 는
    # 시작 시 INSTALL_DIR 을 통째로 지우고("Cleaning old data") config.json 을
    # {"active_version": ...} 만으로 다시 쓴다 — 직전 `set` 이 기록한 channel/pinned
    # 는 pinned fetch 후 남지 않는다(2026-07-27 실측). 버전 고정은 아래에서
    # active_version 경로 prefix + version.json(version/build) 로 권위 있게 강제한다.
    expected_prefix = f"browsers/{PINNED_BROWSER_CHANNEL}/"
    if not active.startswith(expected_prefix):
        return PreflightResult(
            PreflightStatus.BROWSER_VERSION_MISMATCH,
            f"active_version이 {expected_prefix} 아래가 아닙니다: {active}.\n{INSTALL_GUIDE}",
        )

    version_segment = active[len(expected_prefix):]
    if (
        not version_segment
        or "\x00" in version_segment
        or "/" in version_segment
        or "\\" in version_segment
        or version_segment in (".", "..")
        or len(version_segment) > 255
    ):
        return PreflightResult(
            PreflightStatus.BROWSER_VERSION_MISMATCH,
            f"active_version 형식이 잘못되었습니다 (단일 디렉터리만 허용): {active!r}.\n{INSTALL_GUIDE}",
        )

    active_path = install_dir / active

    try:
        trusted_root = install_dir.resolve()
        official_dir = trusted_root / "browsers" / PINNED_BROWSER_CHANNEL

        for component, label in [
            (install_dir / "browsers", "browsers"),
            (install_dir / "browsers" / PINNED_BROWSER_CHANNEL, f"browsers/{PINNED_BROWSER_CHANNEL}"),
            (active_path, f"active_version ({active})"),
        ]:
            if component.is_symlink():
                return PreflightResult(
                    PreflightStatus.BROWSER_VERSION_MISMATCH,
                    f"{label}이 심볼릭 링크입니다 (보안 거부).\n{INSTALL_GUIDE}",
                )

        resolved = active_path.resolve()
        if not (str(resolved) == str(official_dir) or str(resolved).startswith(str(official_dir) + os.sep)):
            return PreflightResult(
                PreflightStatus.BROWSER_VERSION_MISMATCH,
                f"active_version이 허용 경로를 벗어납니다: {active}.\n{INSTALL_GUIDE}",
            )

        if not active_path.is_dir():
            return PreflightResult(
                PreflightStatus.BROWSER_VERSION_MISMATCH,
                f"active_version 경로가 존재하지 않습니다: {active}.\n{INSTALL_GUIDE}",
            )
    except (OSError, ValueError) as e:
        return PreflightResult(
            PreflightStatus.BROWSER_VERSION_MISMATCH,
            f"active_version 경로 검사 실패: {e}\n{INSTALL_GUIDE}",
        )

    ver = _validate_version_json(active_path)
    if ver is None:
        return PreflightResult(
            PreflightStatus.BROWSER_VERSION_MISMATCH,
            f"version.json이 손상되었거나 필수 키(version/build)가 없습니다: {active_path}.\n{INSTALL_GUIDE}",
        )

    if ver != PINNED_BROWSER_VERSION:
        return PreflightResult(
            PreflightStatus.BROWSER_VERSION_MISMATCH,
            f"브라우저 버전 불일치: {ver} (요구: {PINNED_BROWSER_VERSION}).\n{INSTALL_GUIDE}",
        )

    if not _has_executable(active_path):
        return PreflightResult(
            PreflightStatus.BROWSER_VERSION_MISMATCH,
            f"브라우저 실행 파일이 없거나 실행 권한이 없습니다: {active_path}.\n{INSTALL_GUIDE}",
        )

    return None


def _read_geoip_config(install_dir: Path) -> str | None:
    """geoip/config.yml을 YAML로 파싱하여 활성 provider 이름을 반환."""
    config_path = install_dir / "geoip" / "config.yml"
    if not config_path.is_file():
        return None
    try:
        import yaml  # type: ignore[import-untyped]
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, UnicodeError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    if isinstance(name, str) and name:
        return name
    return None


def _get_repos_geoip_entry(provider_name: str) -> dict | None:
    """bundled repos.yml에서 provider의 geoip entry를 YAML로 파싱하여 반환 (대소문자 무시)."""
    try:
        from camoufox.pkgman import INSTALLED_PATH  # type: ignore
        repos_path = Path(INSTALLED_PATH) / "repos.yml"
    except (ImportError, AttributeError):
        try:
            import camoufox
            repos_path = Path(camoufox.__file__).parent / "repos.yml"
        except (ImportError, AttributeError):
            return None

    if not repos_path.is_file():
        return None

    try:
        import yaml  # type: ignore[import-untyped]
        with open(repos_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, UnicodeError, yaml.YAMLError):
        return None

    if not isinstance(data, dict):
        return None

    lower_name = provider_name.lower()
    for entry in data.get("geoip", []):
        if isinstance(entry, dict):
            entry_name = entry.get("name", "")
            if isinstance(entry_name, str) and entry_name.lower() == lower_name:
                return entry
    return None


def _get_geoip_provider_info(provider_name: str) -> tuple[str, str] | None:
    """repos.yml에서 (structure, canonical_name) 반환. 대소문자 무시 매칭."""
    entry = _get_repos_geoip_entry(provider_name)
    if entry is None:
        return None
    urls = entry.get("urls", {})
    if not isinstance(urls, dict):
        return None
    canonical = entry.get("name", provider_name)
    if "combined" in urls:
        return "combined", canonical
    if "ipv4" in urls:
        return "split", canonical
    return None


def _expected_geoip_paths(install_dir: Path, provider: str, structure: str) -> list[Path]:
    """Camoufox geolocation.py get_mmdb_path와 동일한 정확 경로 생성."""
    mmdb_dir = install_dir / "geoip" / "mmdb"
    name = provider.lower()
    if structure == "combined":
        return [mmdb_dir / f"{name}-combined.mmdb"]
    return [mmdb_dir / f"{name}-ipv4.mmdb", mmdb_dir / f"{name}-ipv6.mmdb"]


def _find_geoip_ipv4(install_dir: Path) -> Path | None:
    """활성 provider의 IPv4(또는 combined) GeoIP DB 경로를 반환."""
    provider = _read_geoip_config(install_dir)
    if not provider:
        return None
    info = _get_geoip_provider_info(provider)
    if info is None:
        return None
    structure, canonical = info
    paths = _expected_geoip_paths(install_dir, canonical, structure)
    for p in paths:
        if p.is_file():
            return p
    return None


def _find_geoip_mmdb(install_dir: Path) -> Path | None:
    """진단용: 아무 .mmdb 반환."""
    mmdb_dir = install_dir / "geoip" / "mmdb"
    candidates: list[Path] = []
    if mmdb_dir.is_dir():
        candidates = [f for f in mmdb_dir.iterdir() if f.is_file() and f.suffix == ".mmdb"]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _check_geoip(install_dir: Path) -> PreflightResult | None:
    """GeoIP DB 검증: YAML config + repos.yml 구조 + 정확 경로 + 개별 최신성."""
    provider = _read_geoip_config(install_dir)
    if not provider:
        return PreflightResult(
            PreflightStatus.GEOIP_MISSING,
            f"geoip/config.yml에 활성 provider가 없습니다.\n{INSTALL_GUIDE}",
        )

    info = _get_geoip_provider_info(provider)
    if info is None:
        return PreflightResult(
            PreflightStatus.GEOIP_MISSING,
            f"알 수 없는 GeoIP provider: '{provider}'. "
            f"repos.yml에 정의된 provider를 사용하세요.\n{INSTALL_GUIDE}",
        )

    structure, canonical = info
    expected = _expected_geoip_paths(install_dir, canonical, structure)
    missing = [p for p in expected if not p.is_file()]

    if missing:
        any_mmdb = _find_geoip_mmdb(install_dir)
        diag = f" 발견된 파일: {any_mmdb.name}" if any_mmdb else ""
        missing_names = ", ".join(p.name for p in missing)
        return PreflightResult(
            PreflightStatus.GEOIP_MISSING,
            f"provider '{provider}'의 DB가 없습니다: {missing_names}.{diag}\n{INSTALL_GUIDE}",
        )

    for db_file in expected:
        age_seconds = time.time() - db_file.stat().st_mtime
        age_days = age_seconds / 86400
        if age_days > GEOIP_MAX_AGE_DAYS:
            return PreflightResult(
                PreflightStatus.GEOIP_STALE,
                f"GeoIP DB가 {int(age_days)}일 전 것입니다 "
                f"(허용: {GEOIP_MAX_AGE_DAYS}일 이내). 갱신: "
                "SellerCollector.exe --setup-runtime 재실행 "
                "또는 CoupangRuntimeSetup.exe 재실행 / python -m camoufox fetch\n"
                f"파일: {db_file}",
            )

    return None


def check_runtime() -> PreflightResult:
    """Coupang 수집 시작 전 package/browser/GeoIP 상태 정밀 검사.

    다운로드를 유발하지 않는다. Camoufox API를 호출하지 않는다(완전 read-only).
    검사 항목:
      1. camoufox 패키지 존재 + 버전 == 0.5.4 (fail-closed)
      2. 브라우저: config.json active_version + version.json(version/build) + 실행 파일
      3. GeoIP: config.yml 활성 provider의 IPv4 DB + 30일 이내 갱신
    """
    pkg_err = _check_package_version()
    if pkg_err is not None:
        return pkg_err

    install_dir = _get_install_dir()
    if install_dir is None or not install_dir.exists():
        return PreflightResult(
            PreflightStatus.BROWSER_MISSING,
            f"Camoufox 데이터 디렉터리를 찾을 수 없습니다.\n{INSTALL_GUIDE}",
        )

    browser_err = _check_browser(install_dir)
    if browser_err is not None:
        return browser_err

    geoip_err = _check_geoip(install_dir)
    if geoip_err is not None:
        return geoip_err

    return PreflightResult(PreflightStatus.OK, "Coupang 런타임 준비 완료")
