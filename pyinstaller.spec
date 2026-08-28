# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 빌드 스펙 (WORK_ORDER §7.1 / §13 P3).

사용법:
    pip install pyinstaller
    pyinstaller pyinstaller.spec

2026-07-24: WSL2 Linux 환경에서 venv + pip install(requirements.txt, pyinstaller)
후 본 spec으로 빌드 검증 완료 — ELF 실행 파일 생성, QT_QPA_PLATFORM=offscreen
기동 스모크 정상(크래시 없음). PyInstaller는 크로스 컴파일을 지원하지 않으므로
Windows 대상 배포 전 반드시 Windows 환경에서 별도로 빌드·실행 스모크 테스트를
거칠 것.
"""

from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

block_cipher = None
project_root = Path(SPECPATH)

# camoufox / browserforge / geoip / scrapling 런타임 데이터 파일 일괄 수집.
# 개별 파일을 손으로 나열하면 apify_fingerprint_datapoints·language_tags·tld 등
# 하위 의존성 데이터가 누락돼 frozen 실행 시 FileNotFoundError 가 난다.
# collect_data_files 로 패키지별 데이터를 통째 수집한다.
_pkg_datas = (
    collect_data_files("camoufox")
    + collect_data_files("browserforge")
    + collect_data_files("apify_fingerprint_datapoints")
    + collect_data_files("language_tags")
    + collect_data_files("tld")
    + collect_data_files("scrapling")
    + copy_metadata("camoufox")
)

# scrapling.fetchers 는 모듈 로드 시점에 patchright 를 import 한다(StealthySession 의
# stealth 백엔드는 이미 설치된 camoufox 를 쓰지만, fetchers 모듈 자체가 patchright 에
# 의존). patchright 를 번들하지 않으면 Gmarket 사전조사가
# `ModuleNotFoundError: No module named 'patchright'` 로 즉시 실패한다(2026-07-27 VM 실측).
_patchright_datas, _patchright_bins, _patchright_hidden = collect_all("patchright")
_scrapling_hidden = collect_submodules("scrapling")
# playwright 드라이버(node.exe + package/cli.js)를 명시 번들한다. patchright 를 추가한
# 뒤 PyInstaller 가 playwright 의 driver 를 빠뜨리고 patchright 것만 번들해, camoufox
# (Coupang)가 playwright 노드 드라이버 실행 시 `FileNotFoundError [WinError 2]` 로
# 실패했다(2026-07-27 클린 VM 재검증에서 발견). 두 드라이버를 모두 보장한다.
_playwright_datas, _playwright_bins, _playwright_hidden = collect_all("playwright")

a = Analysis(
    [str(project_root / "app" / "main.py")],
    pathex=[str(project_root)],
    binaries=list(_patchright_bins) + _playwright_bins,
    datas=[
        (str(project_root / "app" / "ui" / "styles" / "theme.qss"), "app/ui/styles"),
        (str(project_root / "app" / "resources" / "coupang_category_tree.json"),
         "app/resources"),
    ] + _pkg_datas + _patchright_datas + _playwright_datas,
    hiddenimports=[
        "PyQt6.QtCore",
        "PyQt6.QtGui",
        "PyQt6.QtWidgets",
        "apify_fingerprint_datapoints",
        "browserforge",
        "browserforge.headers",
        "browserforge.fingerprint",
        "camoufox",
        "camoufox.sync_api",
        "camoufox.pkgman",
        "camoufox.geolocation",
        "camoufox.multiversion",
        "camoufox.addons",
        "playwright",
        "playwright.sync_api",
        "scrapling",
        "scrapling.fetchers",
        "patchright",
        "patchright._impl._driver",
        "curl_cffi",
        # SellerCollector.exe --setup-runtime / --verify-runtime 모드가
        # scripts.setup_coupang_runtime 을 런타임에 import 한다(2026-08-28 이식).
        # `python -m camoufox fetch` 하위 프로세스용 camoufox.__main__ 과
        # rich_click/click 도 함께 필요하다.
        "camoufox.__main__",
        "click",
        "rich_click",
        "scripts.setup_coupang_runtime",
    ] + _patchright_hidden + _scrapling_hidden + _playwright_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="SellerCollector",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # 설치 모드(--setup-runtime/--verify-runtime)는 콘솔 진행 로그가 필요하다.
    # 일반 GUI 모드는 app.main 이 시작 직후 이 콘솔을 숨기므로 사용자에게는
    # 기존 windowed 앱처럼 보인다.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# --- CoupangRuntimeSetup: console setup tool for browser/GeoIP installation ---
setup_a = Analysis(
    [str(project_root / "scripts" / "setup_coupang_runtime.py")],
    pathex=[str(project_root)],
    binaries=list(_patchright_bins),
    datas=list(_pkg_datas) + _patchright_datas,
    hiddenimports=[
        "camoufox",
        "camoufox.__main__",
        "camoufox.pkgman",
        "camoufox.geolocation",
        "camoufox.multiversion",
        "camoufox.addons",
        "camoufox.sync_api",
        "apify_fingerprint_datapoints",
        "browserforge",
        "browserforge.headers",
        "browserforge.fingerprint",
        "rich_click",
        "click",
        "patchright",
        "patchright._impl._driver",
    ] + _patchright_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PyQt6", "tkinter"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

setup_pyz = PYZ(setup_a.pure, setup_a.zipped_data, cipher=block_cipher)

setup_exe = EXE(
    setup_pyz,
    setup_a.scripts,
    setup_a.binaries,
    setup_a.zipfiles,
    setup_a.datas,
    [],
    name="CoupangRuntimeSetup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
