@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM ============================================================
REM  SellerCollector (Gmarket + Coupang) - Windows 빌드 스크립트
REM  Windows에서 이 파일을 더블클릭하거나 터미널에서 실행하세요.
REM  수행: Windows용 venv 생성 -> 의존성 설치 -> PyInstaller 빌드
REM  결과: dist\SellerCollector.exe + dist\CoupangRuntimeSetup.exe
REM ============================================================

REM --- 프로젝트 루트로 이동 (이 스크립트는 scripts\ 안에 있음) ---
pushd "%~dp0.."
echo [정보] 빌드 작업 폴더: %CD%
echo.

REM --- 1) 검증 기준 Python 3.12 탐지 ---
set "PY="
py -3.12 --version >nul 2>nul
if not errorlevel 1 set "PY=py -3.12"
if defined PY goto :python_found

python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto :python_found

if exist "%LocalAppData%\Programs\Python\Python312\python.exe" (
    set PY="%LocalAppData%\Programs\Python\Python312\python.exe"
)
if defined PY goto :python_found

echo [오류] Windows용 Python 을 찾지 못했습니다.
echo.
echo   아래 명령으로 설치한 뒤, 새 터미널을 열어 이 스크립트를 다시 실행하세요:
echo       winget install -e --id Python.Python.3.12
echo.
echo   설치 후에는 PATH 반영을 위해 반드시 창을 새로 열어야 합니다.
goto :error

:python_found
echo [정보] Python 사용: %PY%
%PY% --version
echo.

REM --- 2) 매 빌드마다 깨끗한 Windows 전용 venv 생성 ---
if exist ".venv-win" (
    echo [정보] 이전 빌드 가상환경 제거 중...
    rmdir /s /q ".venv-win"
    if exist ".venv-win" goto :error
)
echo [정보] 가상환경 .venv-win 생성 중...
%PY% -m venv .venv-win
if errorlevel 1 goto :error

call ".venv-win\Scripts\activate.bat"
if errorlevel 1 goto :error
echo.

REM --- 3) 의존성 설치 (앱 실행 패키지 + 빌드 도구 PyInstaller) ---
echo [정보] pip 업그레이드...
python -m pip install --upgrade pip
if errorlevel 1 goto :error

echo [정보] requirements.txt 설치: PyQt6 / scrapling / camoufox 등...
python -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo [정보] PyInstaller 설치...
python -m pip install pyinstaller==6.21.0
if errorlevel 1 goto :error
echo.

REM --- 4) 자동 회귀 테스트 ---
echo [정보] 자동 테스트 실행...
python -m unittest discover -s tests -v
if errorlevel 1 goto :error
echo.

REM --- 5) 빌드 실행 (GUI + 런타임 설치 모드를 포함한 단일 exe 생성) ---
echo [정보] PyInstaller 빌드 시작 - 수 분 소요될 수 있음...
if exist "dist" rmdir /s /q "dist"
if errorlevel 1 goto :error
if exist "build" rmdir /s /q "build"
if errorlevel 1 goto :error
python -m PyInstaller --noconfirm --clean pyinstaller.spec
if errorlevel 1 goto :error
echo.

REM --- 6) 결과 확인 및 SHA-256 manifest 생성 ---
echo ============================================================
if exist "dist\SellerCollector.exe" (
    echo [성공] 앱 실행 파일:        %CD%\dist\SellerCollector.exe
) else (
    echo [오류] dist\SellerCollector.exe 를 찾지 못했습니다. 위 로그를 확인하세요.
    goto :error
)
if exist "dist\CoupangRuntimeSetup.exe" (
    echo [성공] 런타임 설치 파일:     %CD%\dist\CoupangRuntimeSetup.exe
) else (
    echo [오류] dist\CoupangRuntimeSetup.exe 를 찾지 못했습니다. 위 로그를 확인하세요.
    goto :error
)
powershell -NoProfile -Command "$expected = @('CoupangRuntimeSetup.exe', 'SellerCollector.exe'); $actual = @(Get-ChildItem 'dist' -File -Filter '*.exe' | Sort-Object Name | ForEach-Object Name); if (Compare-Object $expected $actual) { Write-Error 'dist EXE set mismatch'; exit 1 }"
if errorlevel 1 goto :error
powershell -NoProfile -Command "$lines = Get-ChildItem 'dist' -File -Filter '*.exe' | Sort-Object Name | ForEach-Object { $h = Get-FileHash -Algorithm SHA256 $_.FullName; '{0}  {1}' -f $h.Hash.ToLower(),$_.Name }; $lines | Set-Content -Encoding ASCII 'dist\SHA256SUMS.txt'"
if errorlevel 1 goto :error
echo [성공] SHA-256 목록:         %CD%\dist\SHA256SUMS.txt
echo ============================================================
echo.
echo [다음 단계]
echo   - 먼저 SellerCollector.exe --setup-runtime 을 1회 실행하세요. Gmarket용 patchright
echo     Downloads Chromium, Camoufox, and GeoIP data, about 1.4 GB total.
echo     Both application tabs require this runtime.
echo   - SellerCollector.exe --verify-runtime 으로 설치 상태를 확인할 수 있습니다.
echo   - 이후 SellerCollector.exe 를 인자 없이 실행해 두 탭을 사용하세요.
echo.
echo [배포 유의사항]
echo   - 인터넷 필수: 설치 모드는 GitHub/jsdelivr/Playwright CDN 에서 약 1.4GB 를 내려받습니다.
echo   - 설치 모드와 GUI는 같은 Windows 사용자 계정으로 실행하세요. 설치 모드만 관리자
echo     권한으로 실행하면 캐시 경로가 달라져 GUI가 브라우저를 못 찾습니다.
echo   - 이 스크립트는 서명 전 빌드와 hash를 만듭니다. 내부 릴리스는
echo     docs\DEPLOYMENT_APPROVAL.md 절차로 Authenticode 서명 후 hash를 다시 생성하세요.
echo   - On first Gmarket collection, a Windows Firewall prompt may appear.
echo     You may close the inbound firewall prompt.
echo   - GeoIP DB 는 30일 후 만료됩니다. 만료 시 SellerCollector.exe --setup-runtime
echo     을 다시 실행해 갱신하세요.
echo.
popd
echo 완료. 창을 닫으려면 아무 키나 누르세요.
pause >nul
exit /b 0

:error
echo.
echo [실패] 빌드가 중단되었습니다. 위의 오류 메시지를 확인하세요.
popd
pause >nul
exit /b 1
