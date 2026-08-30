"""Gmarket Patchright/Chromium 런타임의 read-only 사전 검증."""

from __future__ import annotations

import json
import os
import platform
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

PINNED_PATCHRIGHT_VERSION = "1.61.2"

INSTALL_GUIDE = (
    "명령 프롬프트에서 SellerCollector.exe --setup-runtime 을 먼저 실행하세요.\n"
    "설치 확인: SellerCollector.exe --verify-runtime\n"
    "소스 실행: python scripts/setup_coupang_runtime.py"
)


@dataclass(frozen=True)
class GmarketPreflightResult:
    ok: bool
    message: str


def browsers_dir() -> Path | None:
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if configured and configured != "0":
        return Path(configured)
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        return Path(local) / "ms-playwright"
    if sys.platform == "win32":
        return None
    return Path.home() / ".cache" / "ms-playwright"


def expected_chromium_revision() -> str | None:
    """번들된 Patchright browsers.json의 Chromium revision을 반환."""
    try:
        from patchright._impl._driver import compute_driver_executable

        _, driver_cli = compute_driver_executable()
        data = json.loads(Path(driver_cli).with_name("browsers.json").read_text(encoding="utf-8"))
        browsers = data.get("browsers")
        if not isinstance(browsers, list):
            return None
        for browser in browsers:
            if isinstance(browser, dict) and browser.get("name") == "chromium":
                revision = browser.get("revision")
                return revision if isinstance(revision, str) and revision.isdigit() else None
    except (ImportError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return None


def chromium_installed(root: Path | None, revision: str | None) -> bool:
    if root is None or revision is None or not root.is_dir():
        return False
    chrom = root / f"chromium-{revision}"
    if sys.platform == "win32":
        candidates = (chrom / "chrome-win64" / "chrome.exe",)
    elif platform.machine().lower() in ("aarch64", "arm64"):
        candidates = (chrom / "chrome-linux" / "chrome",)
    else:
        candidates = (chrom / "chrome-linux64" / "chrome",)
    return any(candidate.is_file() for candidate in candidates)


def check_gmarket_runtime() -> GmarketPreflightResult:
    try:
        installed_version = metadata.version("patchright")
    except metadata.PackageNotFoundError:
        return GmarketPreflightResult(False, f"Patchright 패키지가 없습니다.\n{INSTALL_GUIDE}")
    except (OSError, ValueError):
        return GmarketPreflightResult(
            False,
            f"Patchright 패키지 메타데이터가 손상되었거나 읽을 수 없습니다.\n{INSTALL_GUIDE}",
        )
    if installed_version != PINNED_PATCHRIGHT_VERSION:
        return GmarketPreflightResult(
            False,
            f"Patchright 버전이 맞지 않습니다: {installed_version} "
            f"(필요: {PINNED_PATCHRIGHT_VERSION}).\n{INSTALL_GUIDE}",
        )

    revision = expected_chromium_revision()
    root = browsers_dir()
    if not chromium_installed(root, revision):
        location = str(root) if root is not None else "확인 불가"
        return GmarketPreflightResult(
            False,
            f"Gmarket Chromium 런타임이 없거나 버전이 맞지 않습니다 "
            f"(필요 revision: {revision or '확인 불가'}).\n경로: {location}\n{INSTALL_GUIDE}",
        )
    return GmarketPreflightResult(True, "Gmarket 런타임 준비 완료")
