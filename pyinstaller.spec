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

import importlib.util
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata

block_cipher = None
project_root = Path(SPECPATH)

# browserforge → apify_fingerprint_datapoints 데이터 파일 (network-definition.zip 등)
_afd_spec = importlib.util.find_spec("apify_fingerprint_datapoints")
_afd_dir = Path(_afd_spec.submodule_search_locations[0]) / "data"

# camoufox package data (repos.yml, fingerprint JSON, fonts, webgl, gui assets 등)
_camoufox_spec = importlib.util.find_spec("camoufox")
_camoufox_dir = Path(_camoufox_spec.submodule_search_locations[0])
_camoufox_datas = [
    (str(_camoufox_dir / f), "camoufox/" + str(Path(f).parent))
    for f in [
        "repos.yml", "browserforge.yml", "fonts.json", "voices.json",
        "warnings.yml", "territoryInfo.xml", "launchServer.js",
        "fingerprint-presets.json", "fingerprint-presets-v150.json",
        "webgl/webgl_data.db",
        "gui/assets/SegUIVar.ttf", "gui/assets/icon.ico",
        "gui/assets/segmdl2.ttf", "gui/qml/main.qml",
    ]
    if (_camoufox_dir / f).exists()
]

a = Analysis(
    [str(project_root / "app" / "main.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        (str(project_root / "app" / "ui" / "styles" / "theme.qss"), "app/ui/styles"),
        (str(_afd_dir), "apify_fingerprint_datapoints/data"),
    ] + _camoufox_datas + copy_metadata("camoufox"),
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
    ],
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
    console=False,
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
    binaries=[],
    datas=[] + _camoufox_datas + copy_metadata("camoufox"),
    hiddenimports=[
        "camoufox",
        "camoufox.__main__",
        "camoufox.pkgman",
        "camoufox.geolocation",
        "camoufox.multiversion",
        "camoufox.addons",
        "camoufox.sync_api",
        "rich_click",
        "click",
    ],
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
