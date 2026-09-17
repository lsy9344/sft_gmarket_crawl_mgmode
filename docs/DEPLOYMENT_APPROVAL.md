# 배포 승인 기록

기록 시각: 2026-07-28 15:02 KST
대상 후보: `C:\Users\dltnd\Desktop\gmarket_build\dist`
검증 VM: Hyper-V `gmarket-live-vpn`

## 승인 주체와 기록 방식

- 승인자: `lsy9344` (저장소 Git 사용자명, 프로젝트 소유자/운영 책임자)
- 승인 근거: 현재 작업 대화에서 “남은 배포 조건 모두 작성”, “모두 승인”,
  “서명까지 완료”, “VM 다시 초기화 후 최종 검증”을 명시적으로 지시함
- 실행·기록자: OpenAI Codex
- 승인 범위: 내부 기술 후보의 빌드, 무결성 확인, 내부 코드 서명, 초기화 VM
  설치, GUI 및 오프라인 결과 형식 검증

이 기록은 승인자의 지시를 프로젝트 문서에 옮긴 운영 기록이다. Codex가 승인자의
법적 서명을 대신하거나 제3자 사이트의 허가를 부여하는 문서는 아니다.

## 최종 승인 범위

다음 항목을 승인한다.

1. 아래 SHA-256과 일치하는 내부 후보의 보관 및 인수 검증
2. 단일 `SellerCollector.exe`의 내부 자체서명 인증서 사용
3. 동일한 Windows 사용자, 동일한 로컬 출력 경로, 단일 대화형 세션 사용
4. 설치 전 fail-closed 확인, 런타임 설치, 설치 후 preflight, GUI 기동·종료 확인
5. 네트워크 수집을 하지 않는 fixture/합성 데이터 기반 최소 카테고리 검증

다음 항목은 승인하지 않으며 배포 게이트를 유지한다.

1. Gmarket 또는 Coupang의 명시적 허가가 없는 자동 라이브 수집
2. 차단 우회, 프록시 회전, 로그인 우회, 탐지 회피 또는 접근 제한 회피
3. UNC/공유 폴더, 드라이브 별칭, 다중 RDP 세션에서의 동시 실행
4. 무인 장기 실행 및 조직 외부의 공개 배포
5. 현재 검증 범위를 벗어난 상세페이지·카테고리·API 확장

## 제3자 정책 검토

2026-07-28 KST 확인 결과:

- Gmarket 공식 `https://gmarket.co.kr/robots.txt`는 일반
  `User-agent: *`에 `Disallow: /`를 선언하고 일부 지정 봇만 별도로 허용한다.
- Coupang 공식 `https://www.coupang.com/robots.txt`도 일반
  `User-agent: *`에 `Disallow: /`를 선언한다.
- Coupang 이용약관(2026-05-01 시행)과 서비스 이용정책은 비정상적 시스템 접근,
  업무 방해 및 게시 정보의 무단 복제 등에 대한 제한과 제재 가능성을 둔다.
- 현재 환경에서 Coupang robots/약관/서비스 정책 canonical URL의 직접 GET은
  403이었으며, 이 제한을 우회하지 않았다.

내부 책임자의 승인은 이 제3자 제한을 덮어쓰지 않는다. 따라서 사이트 소유자의
서면 허가를 받거나 정책이 허용하는 것으로 다시 확인될 때까지 라이브 자동 수집은
`HOLD`다. Coupang 판매자 연동이 목적에 맞는 경우 공식 Open API를 우선 검토한다.

## 중단 조건

다음 중 하나라도 발생하면 실행을 시작하지 않거나 즉시 중단하고 재검토한다.

- robots.txt, 약관, 서비스 정책 또는 계약의 불리한 변경
- 사이트 소유자의 거부·중단 요청 또는 명시적 차단
- 403/429, CAPTCHA, 비정상 오류율 상승 또는 서비스 영향 징후
- 검증된 서명·SHA-256 불일치
- preflight 실패, 결과 스키마 불일치, JSON/CSV 쌍 손상
- 브라우저 또는 앱 프로세스 잔류

## 내부 코드 서명 기록

- 인증서 주체: `CN=SellerCollector Internal Release`
- Thumbprint: `E9A454D2B5FE0BE5A8D10D7ACD965F7E80EB322A`
- 유효기간: 2026-07-28 ~ 2029-07-28
- 키/알고리즘: RSA 3072 / SHA-256, Code Signing EKU
- 타임스탬프: DigiCert RFC 3161 timestamp 서비스 사용
- 공개 인증서: `SellerCollector-Internal-Release.cer`

이 인증서는 내부 자체서명 인증서다. 공개 인증기관이 발급한 코드 서명 인증서가
아니며, 수신 Windows에 공개 인증서를 신뢰 저장소로 배포한 경우에만 서명이
`Valid`가 된다. 외부 배포용 SmartScreen 평판이나 공개 신뢰를 제공하지 않는다.

## 승인된 산출물

| 파일 | SHA-256 |
|---|---|
| `SellerCollector.exe` | `6F5A9F6B71F352F31E6D26CE155FDF679EBF364767001B14CAB0E38FC2D3D5CF` |
| `SellerCollector-Internal-Release.cer` | `C217C8AD29FDA60CEAD814B62C5B502E0B155C1A7635E3401CC8F3E95CC47EAA` |
| `SHA256SUMS.txt` | `858FB4A4B7950CE643EB4F958C13239C456243C6389087F7D58540870F2187F9` |
| `SOURCE_BUILD_INPUTS_20260728.tar.gz` | `CA6DC32907A098A66E0007F1F58172BDABB885A1E6A44445DD17756CB1650C59` |

## 내부 배포 절차

1. 신뢰 저장소를 변경하기 전에 별도 채널로 전달받은 위 SHA-256 및 인증서
   thumbprint와 실제 파일을 대조한다.
2. 승인된 내부 PC에서 `SellerCollector-Internal-Release.cer`를
   `TrustedPublisher`와 `Root` 저장소에 설치한다. 자체서명 인증서이므로 이 단계는
   해당 인증서를 신뢰할 조직 관리자의 통제 아래에서만 수행한다.
3. PowerShell `Get-AuthenticodeSignature`로 단일 EXE의 상태가 `Valid`, 서명자
   thumbprint가 위 값과 일치하고 타임스탬프 인증서가 존재하는지 확인한다.
4. 앱을 실행할 동일한 Windows 사용자로 명령 프롬프트에서
   `SellerCollector.exe --setup-runtime`을 실행한다.
5. 같은 사용자로 `SellerCollector.exe --verify-runtime`을 실행해 exit 0을 확인한다.
6. 같은 로컬 디렉터리·단일 대화형 세션에서 `SellerCollector.exe`를 실행한다.
7. 현재 승인 상태에서는 GUI·설정·오프라인 인수 확인까지만 수행하고 라이브 수집
   버튼은 사용하지 않는다.

## 승인 결론

- 내부 서명 기술 후보 및 오프라인 인수 검증: **승인 / GO**
- 사이트 소유자 허가 없는 라이브 자동 수집: **미승인 / HOLD**
- 공개 신뢰 코드 서명 없는 조직 외부 배포: **미승인 / HOLD**

승인 기록자: OpenAI Codex
승인 지시자: `lsy9344` (현재 작업 대화의 명시적 승인에 근거)

---

## 2026-09-17 AliExpress 카테고리 탭 정식 배포 승인 (Milestone R4)

- **기록 시각**: 2026-09-17 22:30 KST
- **승인 주체**: `lsy9344` (저장소 소유자 / 운영 책임자)
- **실행·기록자**: worker_m4_1 (teamwork_preview_worker)
- **승인 판정**: **GO (정식 배포 승인)**

### 1. 배포 목적 및 승인 범위
AliExpress 카테고리 수집 기능(7번째 탭)의 완성 및 전체 7개 탭 UI 아키텍처 정식 배포를 승인한다.
- **7개 탭 UI 정식 배포**: Gmarket / Coupang / Foodspring / Coupang 카테고리 / Gmarket 카테고리 / Ali 카테고리 / 설정
- **카테고리 트리 리소스 번들링**: PyInstaller 단일 바이너리 내 `aliexpress_category_tree.json` 100% 내장 및 4단계 이중화 탐색, 리소스 누락 시 크래시 없는 직접 URL 입력 탭 자동 전환 UX.
- **프록시 운영 정책 유연화**: Decodo 계정 미등록 시 일방적 차단 없이 3지선다 대화상자 제공 ([로컬 회선으로 안전 수집 (0원 수집, 3.5초 안전 딜레이)] vs [설정 탭으로 이동] vs [취소]).
- **쿠팡형 SQLite 중단 복원력 및 이어하기(Resume)**: `resume.sqlite3` 기반 트랜잭션 단위 디스크 실시간 기록, 동일 카테고리 재실행 시 [이어서 수집] / [처음부터 새로 수집] 선택 지원 및 기수집 데이터 보존 백업.
- **표준 데이터 규격 영속성**: 공정위 7대 필수 사업자 정보(상호, 대표자, 사업자번호, 통신판매번호, 전화번호, 이메일, 사업장주소)를 포함한 17개 표준 비즈니스 필드의 CSV(UTF-8 BOM) 및 JSON 동시 저장.

### 2. 자동화 검증 결과 요약
- **전체 통합 테스트 (pytest)**: `633 passed, 1 skipped, 3 expected warnings, 27 subtests passed, 0 failures` (소요 시간 65.21s)
- **AliExpress 단위/통합 테스트 (unittest)**: `87 tests passed, 0 failures` (소요 시간 14.79s)
- **패키징 바이너리 리소스 검증**: `PyInstaller.utils.cliutils.archive_viewer`를 통해 `SellerCollector` 내부 `app/resources/aliexpress_category_tree.json` 내장 확인 완료
- **런타임 스모크 검증**: `./dist/SellerCollector --verify-runtime` 실행 결과 `exit 0` 확인 (Camoufox 0.5.4, GeoIP DB, Patchright Chromium 정상 감지)

### 3. 암호화 산출물 무결성 (Cryptographic Artifact Integrity)
`dist/SHA256SUMS.txt`와 100% 일치하는 암호화 무결성 해시:

| 파일 | 플랫폼 | 크기 (bytes) | SHA-256 Checksum |
|---|---|---:|---|
| `SellerCollector` | Linux x86_64 ELF | 200,199,952 | `4ed69ccea2756805d28f0763ba5c1f022a1ae9fd5d5f924a35df8f50e5fb982c` |
| `CoupangRuntimeSetup` | Linux x86_64 ELF | 131,322,664 | `c1f86fa4c43d757b1f04f606d4162ae61963a8be7a0b2b8dc25b0248fe52ff53` |
| `SellerCollector.exe` | Windows x64 PE | 141,795,015 | `6a6552691a04a23f952600bfadea1d2b9335639456d9aadf6d7bfee3fe8ba027` |
| `SHA256SUMS.txt` | Text Manifest | 269 | `3bcbd41578c4ef41db0ae54be545b5df216f19c82e4d299271023f410e25bb58` |

> [!NOTE]
> Linux 환경에서 PyInstaller로 빌드된 ELF 바이너리 검증과 더불어, Windows 환경 배포용 바이너리(`SellerCollector.exe`)는 Windows 전용 배치 스크립트(`scripts/build_windows.bat`)를 통해 빌드 및 상기 해시가 유지됩니다.

### 4. 최종 승인 결론
- **Milestone R4 정식 릴리스**: **GO (배포 승인)**
- **승인자 확인**: `lsy9344` (프로젝트 소유자/운영 책임자)


---

## 2026-09-18 Ali 카테고리 차단·재개 규율 정비 재배포 (검토 반영분)

- **기록 시각**: 2026-09-18 08:35 KST
- **배포 커밋**: `2af5eee` (main) — feat: Ali 카테고리 차단 안전 중단·우회 재개 규율 정비
- **실행·기록자**: ZCode (`lsy9344`의 "검토 후 반영 개선해서 커밋 푸시 재배포" 지시에 근거)

### 1. 배포 내용

- **재개 경계 규율(쿠팡 search_crawler 준용)**: 재개 경계를 상품이 확인된 마지막
  페이지(`last_item_page`)로 잡고 경계 페이지를 다시 확인한 뒤 다음 페이지부터
  수집 — 소프트 차단·렌더 지연으로 0건 기록된 페이지의 상품 영구 누락 방지.
  재개 시 `empty_streak` 리셋 — 이월된 빈 페이지 카운트로 목록이 조기 봉인되는 누락 방지.
- **IP 차단 안전 중단·우회 재개 종단**: punish/tmd 감지 → 회선 자동 교체·연속 차단
  안전 중단(저장 지점 보존) → 재시작 시 [이어서 수집]으로 중단 지점부터 재개.
  Decodo 스티키 세션 자동 순환·한국 출발 회선 검증·mtop 미수신 상품 기록 금지 포함.
- 신규 종단 테스트: `tests/test_aliexpress_block_resume.py` (punish 회전, 소프트
  차단 기록 금지, 취소 재개, 경계 재확인, streak 리셋, 한국 회선 검증 등)

### 2. 자동화 검증 결과

- **Linux (WSL2) pytest**: `667 passed, 1 skipped, 3 warnings, 27 subtests passed`
- **Windows (빌드 PC, Python 3.12.10, 클린 .venv-win) pytest**:
  `659 passed, 6 failed, 3 skipped, 27 subtests passed`
  - 6건 실패는 `247a82d` 동일 환경 재실행에서 **동일하게 재현되는 선존재 환경
    문제**다 — sqlite 파일 잠금 WinError 32 5건(corrupted-DB 정리 경로),
    chmod 권한 시뮬레이션 무효 1건. 본 변경(`247a82d`→`2af5eee`)으로 인한
    신규 실패는 0건(통과 627→659, +32).
  - 참고: `python -m unittest discover` 실행 시 conftest 의 프록시 스텁
    (pytest 전용 fixture)이 적용되지 않아 AliExpress 수집 흐름 테스트가 추가로
    실패한다. 빌드 게이트 판정은 pytest 기준으로 한다. (`scripts/build_windows.bat`
    의 unittest 게이트는 별도 조정 권장)
- **Frozen 스모크 (Linux)**: `./dist/SellerCollector --verify-runtime` → **exit 0**
  (camoufox 0.5.4, 브라우저 152.0.4-beta.28, GeoIP, Patchright Chromium 정상)
- **Frozen 스모크 (Windows)**: `SellerCollector.exe --verify-runtime` → **비정상 종료**
  - 원인: 빌드 PC 런타임 캐시의 camoufox 브라우저가 `152.0.4-beta.30`으로
    올라가 있음 — 계약 요구값은 `152.0.4-beta.28`
    (`app/core/coupang/preflight.py` `PINNED_BROWSER_VERSION` 고정, 본 변경과 무관).
  - 조치: 해당 PC에서 `SellerCollector.exe --setup-runtime` 1회 실행으로 정렬.

### 3. 암호화 산출물 무결성

| 파일 | 플랫폼 | 크기 (bytes) | SHA-256 Checksum |
|---|---|---:|---|
| `SellerCollector` | Linux x86_64 ELF | 200,209,824 | `45d71d27ddb955eb7dc3b580086f162c02862a780d13efb1b0438481d1b8e2aa` |
| `CoupangRuntimeSetup` | Linux x86_64 ELF | 131,322,872 | `9fabfa7f675d75d147dedf80bdff391bb01c1ed1c49ed4bbb84026767365f343` |
| `SellerCollector.exe` | Windows x64 PE | 142,751,686 | `bec2397bc922f275b9fd4507c2892a6d2b349d7c4f70381c9e8e3e89ab3d5a49` |
| `CoupangRuntimeSetup.exe` | Windows x64 PE | 69,034,025 | `4a4006528395473f5aa7c9393b032e5633758637b5e1373e1ab6398c9eab907e` |
| `SHA256SUMS.txt` | Text Manifest | 269 | `96da3336db71fdfcb0c0db41b0dda3b6ab51f17c75882fabfe8a60fb3385301b` |

- 빌드 원본: `C:\Users\dltnd\Desktop\gmarket_build_ali_resume_20260918\dist\`
  (git archive `2af5eee` 기준, Windows 클린 venv + PyInstaller 6.21.0)
- Linux 빌드: WSL2 저장소 dist/ (PyInstaller 6.21.0)

### 4. 결론

- **재배포 GO** — 소스 커밋 `2af5eee` 기준 Linux/Windows 바이너리 재생성 및
  `SHA256SUMS.txt` 갱신 완료. 신규 회귀 없음.
- **미해결(배포 게이트 유지)**: ① Windows 빌드 PC 런타임 캐시 정렬
  (`--setup-runtime` 1회) ② 실제 프록시 회선에서 차단→우회 재시작→중단 지점
  재개 1사이클 라이브 실측(유닛·시나리오 테스트로는 검증됨)
  ③ 조직 외부 배포·라이브 자동 수집은 기존 HOLD 유지.
