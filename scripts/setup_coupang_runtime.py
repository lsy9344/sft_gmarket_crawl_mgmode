"""Coupang 런타임 설치/검증 도구 (WORK_ORDER §11.3).

Camoufox pinned browser와 GeoIP DB를 per-user cache에 설치/검증한다.
PyInstaller로 CoupangRuntimeSetup.exe로 빌드하여 배포물에 포함한다.

사용법:
    python scripts/setup_coupang_runtime.py [--verify-only]

검증 계약 (postcondition):
  - camoufox 패키지 버전 == 0.5.4
  - 브라우저 채널 official, 버전 152.0.4-beta.28 설치
  - GeoIP .mmdb 존재 + 30일 이내 갱신
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.coupang.preflight import (
    PINNED_BROWSER_CHANNEL,
    PINNED_BROWSER_VERSION,
    PINNED_CAMOUFOX_VERSION,
    _check_browser,
    _check_geoip,
    _check_package_version,
    _find_browser_version,
    _find_geoip_ipv4,
    _get_install_dir,
)

PINNED_BROWSER = f"{PINNED_BROWSER_CHANNEL}/stable/{PINNED_BROWSER_VERSION}"


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def run_camoufox_cmd(args: list[str], desc: str) -> bool:
    """Run a camoufox CLI command."""
    if _is_frozen():
        return _run_frozen(args, desc)
    cmd = [sys.executable, "-m", "camoufox"] + args
    print(f"  [{desc}] {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(f"  FAILED: {result.stderr.strip()}")
        return False
    if result.stdout.strip():
        print(f"  {result.stdout.strip()}")
    return True


def _run_frozen(args: list[str], desc: str) -> bool:
    """Frozen mode: invoke camoufox click CLI in-process (no subprocess)."""
    print(f"  [{desc}] camoufox {' '.join(args)} (frozen)")
    try:
        from camoufox.__main__ import cli
        old_argv = sys.argv
        sys.argv = ["camoufox"] + args
        try:
            cli.main(args=args, standalone_mode=False)
        finally:
            sys.argv = old_argv
        return True
    except SystemExit as e:
        return e.code == 0 or e.code is None
    except ImportError as e:
        print(f"  FAILED (import): {e}")
        return False
    except Exception as e:  # noqa: BLE001 - click CLI 실행 경계
        print(f"  FAILED: {e}")
        return False


def verify_postcondition() -> bool:
    """설치 계약 postcondition을 정밀 검증 (frozen/non-frozen 공용).

    preflight와 동일한 파일시스템 검사를 사용하되, 각 단계를 상세 출력한다.
    Fail-closed: 버전 확인 불가 시 실패 처리.
    """
    ok = True

    # 1. Package version + GeoIP extra (shared with app preflight)
    # Fail-fast: 이후의 browser/GeoIP 검사는 camoufox 를 다시 import 한다
    # (_get_install_dir → camoufox.pkgman, repos.yml 조회 → camoufox).
    # package 선행 조건이 실패했는데 계속 진행하면 방금 구조화한 네이티브
    # OSError 가 그 재-import 에서 그대로 다시 전파된다 — check_runtime()
    # 과 같은 순서로 여기서 즉시 중단한다.
    pkg_err = _check_package_version()
    if pkg_err is not None:
        print(f"  [FAIL] {pkg_err.message.splitlines()[0]}")
        return False
    print(f"  [OK] camoufox=={PINNED_CAMOUFOX_VERSION} + maxminddb")

    # 2. Browser (same strict check as app preflight)
    install_dir = _get_install_dir()
    if install_dir is None or not install_dir.exists():
        print("  [FAIL] Camoufox 데이터 디렉터리 없음")
        return False

    browser_err = _check_browser(install_dir)
    if browser_err is not None:
        print(f"  [FAIL] {browser_err.status.value}: {browser_err.message.splitlines()[0]}")
        ok = False
    else:
        found = _find_browser_version(install_dir)
        print(f"  [OK] 브라우저: {found}")

    # 3. GeoIP (same strict check as app preflight)
    geoip_err = _check_geoip(install_dir)
    if geoip_err is not None:
        print(f"  [FAIL] {geoip_err.status.value}: {geoip_err.message.splitlines()[0]}")
        ok = False
    else:
        mmdb = _find_geoip_ipv4(install_dir)
        if mmdb:
            import time as _t
            age = int((_t.time() - mmdb.stat().st_mtime) / 86400)
            print(f"  [OK] GeoIP IPv4: {mmdb.name} ({age}일 전)")
        else:
            print("  [OK] GeoIP IPv4")

    return ok


def main() -> int:
    verify_only = "--verify-only" in sys.argv

    print("=" * 60)
    print("Coupang Runtime Setup")
    print("=" * 60)

    print("\n[1/4] Camoufox 패키지 확인...")
    pkg_err = _check_package_version()
    if pkg_err is not None:
        print(f"  ERROR: {pkg_err.message.splitlines()[0]}")
        print("  설치: pip install camoufox[geoip]==0.5.4")
        return 1
    try:
        from importlib.metadata import version as pkg_version
        print(f"  camoufox installed: {pkg_version('camoufox')}")
    except Exception:  # noqa: BLE001 - 표시용 metadata fallback
        print(f"  camoufox installed: {PINNED_CAMOUFOX_VERSION}")

    if verify_only:
        print("\n[검증] Postcondition 정밀 검증...")
        if not verify_postcondition():
            print("\n[실패] 검증 실패 — 설치 계약을 만족하지 않습니다.")
            return 1
        print("\n[완료] 검증 성공 — 설치 계약 만족.")
        return 0

    print("\n[2/4] Camoufox sync...")
    if not run_camoufox_cmd(["sync"], "sync"):
        return 1

    print(f"\n[3/4] Pinned browser 설정: {PINNED_BROWSER}")
    if not run_camoufox_cmd(["set", PINNED_BROWSER], "set"):
        return 1

    print("\n[4/4] Browser + GeoIP DB 다운로드...")
    if not run_camoufox_cmd(["fetch"], "fetch"):
        return 1

    print("\n[검증] Postcondition 정밀 검증...")
    if not verify_postcondition():
        print("\n[실패] 설치 후 검증 실패.")
        return 1

    print("\n" + "=" * 60)
    print("[완료] Coupang 런타임 설치 성공.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
