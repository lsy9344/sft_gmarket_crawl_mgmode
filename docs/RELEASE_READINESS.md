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

- Linux `pytest -q`: 633 passed, 1 skipped, 3 expected warnings, 27 subtests passed (100% 통과)
- Linux `unittest discover (AliExpress 전용)`: 87 passed (100% 통과)
- Windows clean `.venv-win` 테스트: 240 passed, 2 platform-specific tests skipped
- `ruff check app scripts tests coupang_crawl`: 통과
- `mypy --python-executable .venv/bin/python app scripts coupang_crawl`: 40 files 통과
- `pip check`, `compileall`, `git diff --check`: 통과
- 고정된 직접 의존성의 `pip install --dry-run`: 충돌 없음
- PyInstaller 6.21.0 / Python 3.12.3 clean build: 단일 바이너리 `SellerCollector` 및 `CoupangRuntimeSetup` 생성 성공
- 번들 아카이브 검증: `archive_viewer`로 `aliexpress_category_tree.json` 내장 확인 완료
- 런타임 스모크 검증: `./dist/SellerCollector --verify-runtime` 실행 성공 (exit 0)

## 배포 산출물 무결성 (2026-09-17 최신)

| 파일 | 플랫폼/유형 | 크기(bytes) | SHA-256 |
|---|---|---:|---|
| `SellerCollector` | Linux x86_64 ELF | 200,199,952 | `4ed69ccea2756805d28f0763ba5c1f022a1ae9fd5d5f924a35df8f50e5fb982c` |
| `CoupangRuntimeSetup` | Linux x86_64 ELF | 131,322,664 | `c1f86fa4c43d757b1f04f606d4162ae61963a8be7a0b2b8dc25b0248fe52ff53` |
| `SellerCollector.exe` | Windows x64 PE | 141,795,015 | `6a6552691a04a23f952600bfadea1d2b9335639456d9aadf6d7bfee3fe8ba027` |
| `SHA256SUMS.txt` | Manifest | 269 | `3bcbd41578c4ef41db0ae54be545b5df216f19c82e4d299271023f410e25bb58` |

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

---

## 2026-09-17 AliExpress 카테고리 수집 탭 인수 기준 점검 (Milestone R4)

| 기준 ID | 항목 | 상세 요구사항 | 검증 결과 | 증적 및 검증 내역 |
|---|---|---|:---:|---|
| **AC-ALI-01** | 리소스 번들 및 표시 무결성 | 단일 바이너리 및 개발 환경에서 'Ali 카테고리' 트리가 100% 정상 노출되어야 하며, 파일 손상 시 직접 URL 입력 모드로 크래시 없이 자동 전환 | **PASS** | `pyinstaller.spec` datas에 `aliexpress_category_tree.json` 등록 완료. `archive_viewer`로 바이너리 내 리소스 내장 입증. 4단계 이중화 경로 탐색 및 대체 탭 전환 UI 검증 완료 (`test_aliexpress_category_panel.py`) |
| **AC-ALI-02** | 프록시 운영 정책 및 진입 장벽 | Decodo 미등록 시 일방적 차단 대신 3지선다 대화상자(0원 로컬 안전 수집 vs 설정 탭 이동 vs 취소) 제공 및 안전 딜레이(3~4초) 적용 로컬 수집 지원 | **PASS** | `test_preflight_blocks_when_decodo_missing` 100% 통과. 0원 로컬 수집 시 3.5초 딜레이 적용 및 로컬 네트워크 완주 확인. 상단 배너 문구 현행화 완료 |
| **AC-ALI-03** | 중단 복원력 및 이어하기(Resume) | 대규모 수집 중 취소/비정상 종료 시 `resume.sqlite3`에 진행 상태 보존, 재시작 시 기수집 목록/판매자 고속 스킵 및 이어하기 지원 | **PASS** | `test_aliexpress_resume.py` 41개 테스트 및 복원력 테스트 100% 통과. 동일 카테고리 재실행 시 이어하기/새로시작(타임스탬프 백업 아카이빙) 분기 검증 완료 |
| **AC-ALI-04** | 데이터 품질 및 표준 필드 | 공정위 7대 필수 정보 포함 17개 표준 비즈니스 필드가 CSV(UTF-8 BOM) 및 JSON 동시 저장 | **PASS** | `test_core_7_business_fields_saved_accurately` 통과. 상호, 대표자, 사업자번호, 통신판매번호, 전화번호, 이메일, 사업장주소 등 17개 필드 1:1 매핑 및 영속성 확인 |
| **AC-ALI-05** | 배포 승인 및 패키징 게이트 | 전체 테스트 통과, PyInstaller 클린 빌드 성공, SHA-256 해시 갱신 및 릴리스 문서 공식 승인 | **PASS** | pytest 633개 / unittest 87개 100% 통과. `SellerCollector` 및 `CoupangRuntimeSetup` 빌드 완료. `SHA256SUMS.txt` 갱신 및 `DEPLOYMENT_APPROVAL.md` 공식 GO 판정 완료 |

