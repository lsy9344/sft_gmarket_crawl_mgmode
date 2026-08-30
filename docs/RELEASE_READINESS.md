# 배포 준비 점검

점검일: 2026-07-28
후보 기준: `HEAD 51d6ad0` + 현재 작업 트리 수정분
검증 VM: Hyper-V `gmarket-live-vpn` (`GMARKET-VPN`, Windows 10.0.26200)

## 판정

- **서명된 기술 배포 후보: PASS**
- **내부 설치·GUI·오프라인 인수 검증: GO** — 아래 SHA-256과 일치하는 후보만 사용한다.
- **라이브 자동 수집: HOLD** — Gmarket과 Coupang의 일반 robots 규칙이 전 경로를
  제한하므로 내부 책임자의 승인만으로 실행 범위를 만들 수 없다.
- **조직 외부 공개 배포: HOLD** — 현재 서명은 내부 자체서명 인증서다. 공개 신뢰
  코드 서명과 SmartScreen 평판을 제공하지 않는다.

2026-07-28에 서명된 단일 EXE 최종 후보를 만든 뒤 VM을 다시 `clean-baseline`으로
되돌렸다. 서명·hash 확인 → 설치 전 실패 → 단일 EXE 런타임 설치 → 설치 후 검증 → 실제 콘솔 GUI
기동·종료 → 정책 안전한 랜덤 최소 카테고리 오프라인 검증 → 잔류 프로세스 확인을
재현했다. 최종 상태는 `final-single-exe-validated-20260728` 체크포인트로
보존한다. 이전 라이브 기술 검증 상태는 `latest-rc2-validated-20260728-1304`, 중간 후보는
`latest-rc-validated-20260728`, 초기화 전 상태는
`pre-latest-audit-20260728-1217` 체크포인트로 각각 보존되어 있다.

## 자동 검증

- Linux `unittest discover`: 240 passed
- Linux `pytest -q`: 240 passed, 3 expected warnings, 21 subtests passed
- Windows clean `.venv-win` 테스트: 240 passed, 2 platform-specific tests skipped
- `ruff check app scripts tests coupang_crawl`: 통과
- `mypy --python-executable .venv/bin/python app scripts coupang_crawl`: 40 files 통과
- `pip check`, `compileall`, `git diff --check`: 통과
- 고정된 직접 의존성의 `pip install --dry-run`: 충돌 없음
- PyInstaller 6.21.0 / Python 3.12.10 clean build: 단일 Windows EXE 생성 성공
- Windows frozen smoke: `--setup-runtime`, `--verify-runtime`, GUI 기동·종료 성공

## Windows 후보와 무결성

호스트 후보 디렉터리: `C:\Users\dltnd\Desktop\gmarket_build\dist`
게스트 배포 디렉터리: `C:\Users\Public\SellerSingle`

| 파일 | 크기(bytes) | SHA-256 |
|---|---:|---|
| `SellerCollector.exe` | 141,696,136 | `6F5A9F6B71F352F31E6D26CE155FDF679EBF364767001B14CAB0E38FC2D3D5CF` |
| `SellerCollector-Internal-Release.cer` | 1,066 | `C217C8AD29FDA60CEAD814B62C5B502E0B155C1A7635E3401CC8F3E95CC47EAA` |
| `SHA256SUMS.txt` | 87 | `858FB4A4B7950CE643EB4F958C13239C456243C6389087F7D58540870F2187F9` |
| `SOURCE_BUILD_INPUTS_20260728.tar.gz` | 126,373 | `CA6DC32907A098A66E0007F1F58172BDABB885A1E6A44445DD17756CB1650C59` |

`dist` 안의 EXE가 `SellerCollector.exe` 하나뿐임을 확인했고, 호스트 산출물,
manifest, 게스트에 복사된 단일 EXE의 SHA-256이 일치했다.
소스 archive는 `app`, `coupang_crawl`, `scripts`, `tests`, `pyinstaller.spec`,
`requirements.txt`, `.gitattributes`만 포함하며 로컬 IDE/에이전트 설정과 cache를
제외한다. `__pycache__`와 `.pyc/.pyo`가 0개임을 다시 확인했고, 필수 신규
`gmarket_preflight` 모듈·테스트·`.gitattributes`가 포함됐다. dirty-tree 후보의
build input을 이 archive와 hash로 고정했다.

## Windows clean-cache 검증

최종 검증은 게스트 `C:\Users\Public\SellerSingle`의 단일 EXE로 수행하고
`final-single-exe-validated-20260728` 체크포인트에 보존했다.

1. `clean-baseline` 복원 직후 Camoufox/Patchright cache가 없음을 확인했다.
2. 설치 전 `SellerCollector.exe --verify-runtime`은 예상대로 exit 1을 반환했다.
   Camoufox와 Gmarket Chromium 누락을 각각 보고했다.
3. 같은 단일 EXE의 `--setup-runtime` 모드로 Camoufox, GeoIP, Patchright
   Chromium을 설치했다.
4. 설치 후 `--verify-runtime`은 exit 0을 반환했다.
   - Camoufox package: `0.5.4`
   - Camoufox browser: `152.0.4-beta.28-386fc2f4`
   - Camoufox config:
     `C:\Users\gmarket-admin\AppData\Local\camoufox\camoufox\Cache\config.json`
   - GeoIP: IPv4 DB 존재 및 2026-07-28 갱신 확인
   - Gmarket Chromium:
     `C:\Users\gmarket-admin\AppData\Local\ms-playwright\chromium-1228\chrome-win64\chrome.exe`
5. `SellerCollector.exe`를 로그인된 실제 콘솔 세션에서 실행했다. Windows UI
   Automation으로 `판매자 정보 수집기 v3.0`, 활성 창, 하위 요소 84개, 버튼 7개,
   탭 2개를 확인하고 표준 close로 정상 종료했다.
6. 22개 카테고리에서 난수로 `디지털/가전` 1개를 선택해 네트워크 요청 없이
   1행 JSON/CSV를 생성했다. 11필드 순서, JSON UTF-8, CSV UTF-8 BOM, 왕복 읽기,
   행 수와 SHA-256을 확인했다.
   - seed: `655885663`, index: `6`, source: `best`
   - JSON: `867EF64A73F4F4F5858BA517F6906F8D1BE27565EA06AB85E39DF736A8705614`
   - CSV: `84E317394581BE922B90221D2A1EABC004CB372D7052A5A34F9BBAC9E337DD99`

따라서 AC-W01~AC-W06은 모두 PASS다.

## 이전 라이브 기술 검증

아래 결과는 정책 검토 전에 수행한 기능 증적이며 현재의 라이브 실행 허가가 아니다.
최종 초기화 VM 검증에서는 robots/약관 제한을 존중해 다시 실행하지 않았다.

### Gmarket

- 사전 조사: 22개 카테고리 확인
- 22개 카테고리에서 각 최대 10건, 성공 220건 / 실패 0건
- 최신 전체 검증 쌍: JSON 220행 / CSV 220행
- 두 파일의 필드 수·이름·순서 일치(11개):
  `goodscode`, `url`, `store_name`, `company_name`, `ceo_name`, `email`,
  `phone`, `business_number`, `address`, `collected_at`, `source`
- Patchright Chromium 프로세스의 실제 기동 확인

### Coupang

- 제품 기본 홈 웜업 20초, 무프록시, 최대 1페이지, batch 5,
  요청 딜레이 0.5~1.0초로 실행
- Akamai 통과 및 `/np/omp` request template 캡처 성공
- vendorItemId 48개, 고유 판매자 45개, 최종 레코드 45개
- 최신 검증 쌍: JSON 45행 / CSV 45행
- 두 파일의 필드 수·이름·순서 일치(14개):
  `vendor_id`, `url`, `store_name`, `company_name`, `ceo_name`,
  `business_number`, `phone`, `email`, `address`,
  `ecommerce_report_number`, `power_seller`, `power_seller_title`,
  `rating_count`, `thumb_up_ratio`
- `vendor_id` 및 `business_number`: 45/45 존재
- power seller: 16행
- 완료 후 Camoufox 프로세스: 0

첫 Coupang 시도에서 테스트용 웜업을 1초로 줄였을 때 request capture가 실패했고
빈 결과 쌍이 생성되었다. 제품 기본값 20초로 재실행하여 성공했으므로 기본값을
유지해야 한다. AC-L01~AC-L09는 PASS다.

## 종료·잔류 검증

실수집 완료 뒤 앱 창을 UI Automation의 표준 close 동작으로 닫고 20초 후 확인했다.

- `SellerCollector`: 0
- `camoufox`: 0
- `chrome`: 0
- `node`: 0
- Windows 출력 디렉터리의 `.gmarket_fast.lock`: 없음(named mutex 사용)

AC-L10은 PASS다. QThread/브라우저 드라이버에 별도 강제 종료 프로세스 경계가 없는
구조적 위험은 남지만, 이번 실제 종료 검증에서는 잔류가 재현되지 않았다.

## 검증 중 발견하고 수정한 문제

- `scripts/build_windows.bat`의 parenthesized block과 안내 문구 괄호가 Windows
  `cmd.exe`와 `call` 이중 파싱에서 깨지던 문제를 label/goto 흐름과 괄호 없는
  문구로 수정했다.
- `.gitattributes`에 `*.bat text eol=crlf`를 추가해 Windows 배치 파일의 개행을
  고정했다.
- Windows CP949 기본 locale에서 두 테스트의 `read_text()`가 UTF-8 결과 파일을
  잘못 해석하던 문제를 명시적 `encoding="utf-8"`로 수정했다.
- Coupang 결과 저장도 Gmarket과 같은 출력 디렉터리 프로세스 잠금을 사용한다.
- 같은 prefix가 겹치면 `_2`, `_3`을 붙여 기존 JSON/CSV를 보존한다.
- Gmarket은 번들된 Patchright `browsers.json`의 정확한 Chromium revision과
  실행 파일을 확인한다.
- 손상되거나 읽을 수 없는 Patchright package metadata도 UI 예외로 새지 않고
  설치 안내가 포함된 fail-closed 결과로 바꾼다.
- NaN/무한대 timing 값은 브라우저 실행 전에 설정 오류로 거부한다.

## 운영 승인과 정책 게이트

### AC-P01: 제품/운영 승인 기록

책임자 승인, 승인일, 허용 범위, 정책 근거와 중단 조건은
[`DEPLOYMENT_APPROVAL.md`](DEPLOYMENT_APPROVAL.md)에 기록했다. AC-P01의 **내부
책임자 기록 요건은 PASS**다. 다만 이 승인은 제3자 사이트의 허가가 아니다.

2026-07-28 KST 기준 Gmarket과 Coupang 공식 robots.txt는 일반
`User-agent: *`에 `Disallow: /`를 선언한다. Coupang 이용약관(2026-05-01 시행)과
서비스 이용정책도 비정상적 접근 및 게시 정보의 무단 복제 등에 제한을 둔다.
따라서 사이트 소유자의 서면 허가 또는 허용 정책을 다시 확인하기 전까지 라이브
자동 수집 게이트는 `HOLD`다.

### 코드 서명

단일 `SellerCollector.exe`를 `CN=SellerCollector Internal Release` 자체서명 코드 서명 인증서
(thumbprint `E9A454D2B5FE0BE5A8D10D7ACD965F7E80EB322A`, RSA 3072/SHA-256)로
서명하고 DigiCert 타임스탬프를 추가했다. 공개 인증서를 초기화 VM의
TrustedPublisher 및 LocalMachine Root에 설치한 뒤 서명이 `Valid`임을 다시
확인했다.

이는 내부 무결성 서명이다. 공개 CA 인증서가 아니므로 인증서를 배포하지 않은
Windows의 공개 신뢰 또는 SmartScreen 평판은 해결하지 않는다. 모든 전이 의존성의
hash lock도 아직 없으므로 조직 외부 공개 배포는 계속 `HOLD`다.

## 배포 조건

내부 설치·오프라인 인수 검증은 위 SHA-256 후보에 한해 가능하다. 현재 출력 잠금은 같은
Windows 세션의 동일한 로컬 경로를 전제로 하므로 UNC/공유 폴더, 드라이브 별칭,
다중 RDP 세션은 지원 범위에서 제외한다. 브라우저 작업은 강제 종료 가능한 별도
프로세스 경계가 없으므로 무인 장기 실행도 현재 승인 범위가 아니다.

라이브 자동 수집은 사이트 소유자의 서면 허가 또는 정책상 허용 근거가 확보된 뒤,
조직 외부 배포는 공개 신뢰 코드 서명과 전이 의존성 lock을 완료한 뒤 이 문서의
판정을 갱신한다. 프록시, 상세페이지, 카테고리 확장은 현재 검증 범위에 포함되지
않으며 별도 검토가 필요하다.
