@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM ============================================================
REM  SellerCollector (Gmarket + Coupang) - Windows 빌드 스크립트
REM  Windows에서 이 파일을 더블클릭하거나 터미널에서 실행하세요.
REM  수행: Windows용 venv 생성 -> 의존성 설치 -> PyInstaller 빌드
REM  결과: dist\SellerCollector.exe , dist\CoupangRuntimeSetup.exe
REM ============================================================

REM --- 프로젝트 루트로 이동 (이 스크립트는 scripts\ 안에 있음) ---
pushd "%~dp0.."
echo [정보] 빌드 작업 폴더: %CD%
echo.

REM --- 1) Python 탐지 (py 런처 우선, 없으면 python) ---
set "PY="
py -3 --version >nul 2>nul
if %errorlevel%==0 (
    set "PY=py -3"
) else (
    python --version >nul 2>nul
    if %errorlevel%==0 set "PY=python"
)

if not defined PY (
    echo [오류] Windows용 Python 을 찾지 못했습니다.
    echo.
    echo   아래 명령으로 설치한 뒤, 새 터미널을 열어 이 스크립트를 다시 실행하세요:
    echo       winget install -e --id Python.Python.3.12
    echo.
    echo   (설치 후에는 PATH 반영을 위해 반드시 창을 새로 열어야 합니다.)
    goto :error
)
echo [정보] Python 사용: %PY%
%PY% --version
echo.

REM --- 2) Windows 전용 venv 생성 (.venv-win: WSL의 .venv와 분리) ---
if not exist ".venv-win\Scripts\python.exe" (
    echo [정보] 가상환경 .venv-win 생성 중...
    %PY% -m venv .venv-win
    if errorlevel 1 goto :error
) else (
    echo [정보] 기존 .venv-win 재사용
)

call ".venv-win\Scripts\activate.bat"
if errorlevel 1 goto :error
echo.

REM --- 3) 의존성 설치 (앱 실행 패키지 + 빌드 도구 PyInstaller) ---
echo [정보] pip 업그레이드...
python -m pip install --upgrade pip
if errorlevel 1 goto :error

echo [정보] requirements.txt 설치 (PyQt6 / scrapling / camoufox 등)...
python -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo [정보] PyInstaller 설치...
python -m pip install pyinstaller
if errorlevel 1 goto :error
echo.

REM --- 4) 빌드 실행 (앱 GUI exe + Coupang 런타임 setup exe 동시 생성) ---
echo [정보] PyInstaller 빌드 시작 (수 분 소요될 수 있음)...
python -m PyInstaller --noconfirm --clean pyinstaller.spec
if errorlevel 1 goto :error
echo.

REM --- 5) 결과 확인 ---
echo ============================================================
if exist "dist\SellerCollector.exe" (
    echo [성공] 앱 실행 파일:        %CD%\dist\SellerCollector.exe
) else (
    echo [경고] dist\SellerCollector.exe 를 찾지 못했습니다. 위 로그를 확인하세요.
)
if exist "dist\CoupangRuntimeSetup.exe" (
    echo [성공] 쿠팡 런타임 설치기: %CD%\dist\CoupangRuntimeSetup.exe
)
echo ============================================================
echo.
echo [다음 단계]
echo   - 먼저 CoupangRuntimeSetup.exe 를 1회 실행하세요. Gmarket용 patchright
echo     Chromium + Coupang용 Camoufox 브라우저 + GeoIP DB(합계 약 1.4GB)를
echo     내려받아 설치합니다. (Gmarket/Coupang 두 탭 모두 이 설치가 필요합니다.)
echo   - 이후 SellerCollector.exe 를 실행해 두 탭을 사용하세요.
echo.
echo [배포 유의사항]
echo   - 인터넷 필수: 설치기는 GitHub/jsdelivr/Playwright CDN 에서 약 1.4GB 를 내려받습니다.
echo   - 두 exe 는 같은 Windows 사용자 계정으로 실행하세요(설치기만 관리자 권한으로
echo     실행하면 캐시 경로가 달라져 앱이 브라우저를 못 찾습니다).
echo   - 서명이 없어 새 PC 첫 실행 시 SmartScreen 경고가 뜨면 [추가 정보]-[실행].
echo   - Gmarket 첫 수집 시 Chromium("Google Chrome for Testing") 방화벽 팝업이 뜰 수
echo     있습니다. 아웃바운드 수집은 정상 동작하므로 닫아도 됩니다.
echo   - GeoIP DB 는 30일 후 만료됩니다. 만료 시 CoupangRuntimeSetup.exe 를 다시
echo     실행해 갱신하세요.
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
