# Bright Data 계정 설정 기능 — API 키 입력·사용량 차감 (2026-09-09)

> 요구: "이 플랫폼(Bright Data)의 API를 넣을 수 있는 기능을 만들어서 그 키에서
> 차감되게끔 한다." 두 탭(Coupang 카테고리 / Gmarket 카테고리)에 Bright Data
> 기술이 들어가 있으므로, 앱 사용자가 **자신의 계정 키**를 입력하면 두 탭의
> 사용량이 그 키에서 차감되도록 한다.
> 근거 문서: [`docs/gmarket/ACCESS_ROUTES_RESEARCH_20260908.md`](gmarket/ACCESS_ROUTES_RESEARCH_20260908.md) §6·§8
> (Web Unlocker 스펙·실측), [`docs/coupang/BRIGHTDATA_AKAMAI_REVIEW_20260908.md`](coupang/BRIGHTDATA_AKAMAI_REVIEW_20260908.md) §3·§7·§9
> (ISP 프록시 하이브리드 실측·판정)

## 1. 요약 (한눈에)

| 항목 | 내용 |
|---|---|
| UI | 새 **"설정" 탭** — API 토큰·Unlocker 존·출발 국가, ISP 프록시(계정 ID/존/비밀번호) 입력, 잔액 조회·토큰 검증 버튼, 저장 버튼 |
| 저장 위치 | `output/brightdata_settings.json` (`.gitignore` — 저장소 밖, POSIX 0600) |
| Gmarket 카테고리 탭 | Phase A 리스팅이 입력된 토큰 + 입력된 존으로 `POST /request` 호출 — **요청 단위 과금이 입력 계정에서 차감** |
| Coupang 카테고리 탭 | 설정에서 활성화 시 Camoufox 실행에 ISP 프록시 주입 — **대역폭 과금이 입력 계정에서 차감** (기본값: 끔 — 아래 유의점) |
| 토큰 우선순위 | 설정 파일(UI 입력) → 환경변수 `BRIGHTDATA_API_TOKEN` → 기존 `output/brightdata_token.txt` (하위 호환) |
| 핵심 모듈 | `app/core/brightdata.py` (Qt 비의존), `app/ui/widgets/brightdata_panel.py` (탭 UI) |

## 2. 기능 상세

### 2.1 설정 탭 (신규)

- **계정 — Web Unlocker**: API 토큰(마스크 입력 + 표시 토글), Unlocker 존
  (기본 `gm_unlocker`), 출발 국가(기본 `kr`, 저장 시 소문자 정규화).
- **잔액 조회·토큰 검증 버튼**: `GET https://api.brightdata.com/customer/balance`
  (Bearer 토큰) → 잔액·다음 청구 예정 표시. 401/403 은 "토큰 거부"로 안내.
  공식 문서: docs.brightdata.com — Account Management API "Total balance".
- **ISP 프록시 — Coupang 카테고리 탭(선택)**: 사용 체크박스, 계정 ID(`/status`
  의 `customer`, 예 `hl_22fb0228`), ISP 존명, 존 비밀번호. **"토큰으로 가져오기"**
  버튼은 `GET /zone/passwords?zone=<존명>`(2026-09-08 실측 엔드포인트)으로
  비밀번호를 자동 채운다.
- **프록시 테스트 버튼 (2026-09-09 신규)**: 입력한 계정 ID·존·비밀번호로
  실제 프록시 연결을 1회 시험(`lumtest.com/myip.json`, 네트워크 오류 시
  ipify 폴백)해 출발 IP·국가를 표시한다. 잔액 조회는 계정 토큰만 검증하므로
  프록시 3요소 검증은 이 버튼이 유일하다. 407 은 "인증 실패 — ID/존/비밀번호
  확인", 시간 초과는 "존 상태(IP 할당) 확인"으로 원인을 분류해 안내한다.
  요청 1건 수 KB 의 대역폭이 과금된다.
- **저장 + 토큰 검증 연동 (2026-09-09 신규)**: 새 토큰이 입력된 상태로
  저장하면 먼저 잔액 조회로 검증한다 — ①통과 시 "검증 완료"로 저장,
  ②401/403 거부는 확인 대화상자(아니오 = 저장 취소, 예 = 사용자 확인으로
  저장), ③네트워크 오류 등 판정 불가는 저장하되 "미검증"으로 명시한다.
  빈 토큰 위젯은 기존 저장값 유지(검증 없이 저장).
- **ISP 불완비 저장 경고 (2026-09-09 신규)**: 프록시 사용 체크 상태로
  저장했는데 ID/존/비밀번호가 하나라도 비어 있으면, 저장은 되지만 상태에
  "Coupang 탭에서 프록시 없이(직접 접속) 진행된다"는 경고를 남긴다.
- **비동기 호출**: 위 검증·조회 버튼은 모두 QThread 워커로 실행된다 — 응답이
  늦어도 UI 가 멈추지 않고, 실행 중에는 동작 버튼이 잠겨 중복 호출을 막는다.
- **저장**: 즉시 파일 반영. 토큰·비밀번호 위젯은 보안상 전체 값을 복원하지
  않고 마지막 4자리 마스크만 보여주며, 빈 상태로 저장하면 기존 값이 유지된다.
  상태 라벨은 성공 초록/실패 빨강으로 구분 표시된다.
- 수집 실행 중에는 다른 탭과 같은 규율로 입력이 잠긴다.

### 2.2 Gmarket 카테고리 탭 연동 (Web Unlocker)

`GmarketCategoryLister.collect()` 가 실행 시점에 설정 파일을 읽어
`Authorization: Bearer <토큰>` + `{"zone": <입력 존>, "country": <입력 국가>}`
로 요청한다(기존 하드코딩 `config.BRIGHTDATA_ZONE` 는 입력이 없을 때의
폴백이 됨). 토큰이 전혀 없으면 설정 탭 안내와 함께 즉시 실패한다 —
요청당 약 $0.003 이 **입력된 계정 키**에서 차감된다.

### 2.3 Coupang 카테고리 탭 연동 (ISP 프록시 + 단계별 IP 분리)

- `CoupangRunConfig.proxy` (신규, playwright 형식 dict) — 설정 탭에서
  활성화 + 자격 완비(계정 ID/존/비밀번호)일 때만 `SearchRunConfig` 에 주입.
- `CoupangCrawler._create_browser()` 가 `Camoufox(..., proxy=...)` 로 전달.
  `geoip=True` 가 프록시 IP 기준 locale/타임존/지리를 자동 동기화한다
  (Akamai 교차 검증 신호 일관성 — BRIGHTDATA_AKAMAI_REVIEW §7.2 권장 조합).
- 영속 프로필 경로에도 동일하게 프록시가 적용된다(기동 실패 시 신규 세션
  폴백도 프록시 유지). 프록시 경유 사실은 로그에 사용자명만 남기고
  비밀번호는 기록하지 않는다.
- **단계별 IP 분리(2026-09-09 추가, 실측 §9)**: 1차 목록은 프록시 IP 로
  돌리고(회선 IP 보호), 2차 판매자정보 직전에 엔진이 프록시 세션을 닫고
  같은 영속 프로필의 **회선 IP 세션을 새로 열어** 웜업 후 진행한다 —
  판매자정보 API(`getStoreReview`)는 프록시 IP 에서 403, 회선 IP 에서
  200 실측. 프록시 미사용 시 기존처럼 단일 세션(회귀 없음).
- **부트스트랩 셸 규칙(§3·§8)**: 목록 로드에서 상품 0건 + 작은 본문
  (10KB 미만 — 정상 목록 ~600KB+, 셸 471B~3.4KB)은 차단이 아니라 JS 부트
  페이지로 보고 대기 후 같은 URL 을 재내비게이션한다(최대 2회, 1~2회
  회복 실측). 셸이 끝까지 지속하면 실제 목록 끝으로 판정해 빈 페이지로
  처리한다. 차단 키워드(Access Denied/사용권한 등)는 기존처럼 즉시 중단.
- **기본값이 '끔'인 이유**: 프록시는 회선 IP 를 소모하는 유료 자원이고
  로그인 세션과 조합하면 계정 보안 경보 가능성이 있다(§7.4). 로그인 강제
  수집 중 프록시를 켜면 엔진이 경고 로그를 남긴다.

### 2.4 보안·프라이버시

- 설정 파일은 `output/` 아래라 절대 커밋되지 않는다(.gitignore §output).
- 쓰기는 동일 폴더 임시 파일 → `os.replace` 원자적 교체, POSIX 에선 0600.
- 로그·UI 에는 토큰/비밀번호 마지막 4자리(`…xxxx`)만 노출한다.
- 토큰이 대화/문서에 노출된 이력이 있으므로(ACCESS_ROUTES §10 유의점) 운영
  안정화 후 재발급 권장 — 이 기능은 재발급 후 새 키를 즉시 교체 입력할 수
  있게 하는 것이기도 하다.

## 3. 검증 기록

| 검증 | 결과 |
|---|---|
| 전체 자동 테스트 (WSL venv, pytest) | **424 passed**, 27 subtests (신규 45건 포함: 설정 모듈 30 + 패널 UI 10 + 엔진/프록시 연동 5) |
| `ruff check` (변경 파일) | 베이스라인과 동일 — 신규 위반 0 |
| `mypy app scripts` | 26 errors — **main 베이스라인과 동일**(신규 파일 기여 0) |
| MainWindow 탭 구성 테스트 | 6탭(… "설정") 갱신 확인 |

신규 테스트: `tests/test_brightdata_settings.py` (저장/로드·우선순위·프록시
조립·계정 API 파싱), `tests/test_brightdata_panel.py` (offscreen UI),
`tests/test_coupang_crawler.py` 프록시 검증·Camoufox 전달, 
`tests/test_gmarket_category_crawler.py` 설정 존/토큰 사용.

## 4. 운영 절차 (배포 환경)

1. brightdata.com 가입 → 결제 수단 등록(존 생성·요청 과금에 필요) →
   Settings 에서 **API 토큰** 발급.
2. 앱 **설정 탭**에 토큰 입력 → "잔액 조회·토큰 검증"으로 확인 → 저장.
3. Web Unlocker 존 생성은 계정에서 1회(관리 API `POST /api/zone`, `plan`은
   객체 — ACCESS_ROUTES §6). 존 이름을 설정 탭의 "Unlocker 존"에 입력.
4. Coupang ISP 프록시를 쓰려면 ISP 존 생성(`plan.bandwidth:"payperusage"` +
   `country:"kr"` + `ips:1` 필수 — ACCESS_ROUTES §8) 후 설정 탭에
   계정 ID/존/비밀번호 입력. 비밀번호는 "토큰으로 가져오기"로 채움.
5. Gmarket 카테고리 탭은 즉시 입력 키로 차감되며, Coupang 카테고리 탭은
   "ISP 프록시 경유" 체크가 켜져 있을 때만 프록시로 나간다.

## 5. 남은 검증 (라이브)

- 배포 환경에서 사용자 소유 계정 키로 Unlocker 요청 1건 → `budget`/잔액으로
  차감 확인 (개발 계정이 아닌 별도 계정 필요 — 로컬 검증은 mock 응답 기반).
- ISP 존 보유 계정에서 Coupang 목록 수집 1사이클 → 차감·IP 확인.

## 6. 재배포 기록 (2026-09-09)

- 빌드 커밋: `b0687f7` (feature/brightdata-account-key)
- 빌드 환경: Windows Python 3.12.10, PyInstaller 6.21.0, 클린 `.venv-win`
- 빌드 내 자동 테스트: `Ran 424 tests — OK (skipped=2)` — 신규
  brightdata 설정/패널 테스트 포함
- Frozen 스모크: `SellerCollector.exe --verify-runtime` → exit 0
  (camoufox 0.5.4 + GeoIP + patchright Chromium 전부 OK)
- 산출물(`C:\Users\dltnd\Desktop\gmarket_build_bdapi_b0687f7\dist\`):

| 파일 | 크기(bytes) | SHA-256 |
|---|---:|---|
| `SellerCollector.exe` | 142,626,760 | `174d1add28ecc382a83e186b6924e3f7d0a0941b70ff2c20e3aafc7daf54189d` |
| `CoupangRuntimeSetup.exe` | 69,004,898 | `075968a56ae8f90aa8dc7ce4b292affd5a8a5bbd042a13dfceae376787be4df6` |

- 내부 서명 릴리스가 필요하면 `docs/DEPLOYMENT_APPROVAL.md` 절차로
  Authenticode 서명 후 hash를 다시 생성한다(기존 게이트 유지).

## 7. 재배포 기록 2 — 실측 규칙 앱 반영분 (2026-09-09)

- 빌드 커밋: `2365a7f` (feature/coupang-proxy-phase-split) — §2.3 의
  단계별 IP 분리 + 부트스트랩 셸 규칙 포함
  (상세: BRIGHTDATA_AKAMAI_REVIEW §10)
- 빌드 환경: Windows Python 3.12.10, PyInstaller 6.21.0, 클린 `.venv-win`
- 빌드 내 자동 테스트: `Ran 432 tests — OK (skipped=2)` — 셸 4건 +
  세션 분리 3건 + 프록시 인자 1건 신규 포함
- Frozen 스모크: `SellerCollector.exe --verify-runtime` → exit 0
- 산출물(`C:\Users\dltnd\Desktop\gmarket_build_coupang_split_2365a7f\dist\`):

| 파일 | 크기(bytes) | SHA-256 |
|---|---:|---|
| `SellerCollector.exe` | 142,630,512 | `772373b15c3f838938ab13ec9d48c25bb168135bf86901f190c194bb3cb860e3` |
| `CoupangRuntimeSetup.exe` | 69,005,501 | `86e4a205277ebba7b2425e0905799dd098451344bd38bd6534a724a5385abf7d` |

## 8. 재배포 기록 3 — 검토 반영분 (2026-09-09)

- 빌드 커밋: `9f17f16` (main) — 판매자 API 연속 실패 차단기(blockguard
  연동) + Unlocker 빈 껍데기 회복·`x-brd-error` 검사 + max_pages/딜레이
  기본값 정렬 + 시작 전 잔액 안내 (상세: BRIGHTDATA_AKAMAI_REVIEW §11)
- 빌드 환경: Windows Python 3.12, PyInstaller 6.21.0, 클린 `.venv-win`
- 빌드 내 자동 테스트: `Ran 441 tests — OK (skipped=2)` — 서킷 브레이커 4건 +
  blockguard 종단 1건 + 빈 껍데기 재요청 3건 + mg 연속 실패 중단 2건 신규 포함
- Frozen 스모크: `SellerCollector.exe --verify-runtime` → exit 0
- 산출물(`C:\Users\dltnd\Desktop\gmarket_build_review_20260909\dist\`):

| 파일 | 크기(bytes) | SHA-256 |
|---|---:|---|
| `SellerCollector.exe` | 142,632,792 | `936853cbda3f065713b3cfbb6f30c4ab83c79f4d2cfaeda76ebec377ca018454` |
| `CoupangRuntimeSetup.exe` | 69,005,704 | `3ba0e23f4d54cd7b229584bb81e2a6382fc0bd89fcb7966bece95800d77c800a` |

- 내부 서명 릴리스가 필요하면 `docs/DEPLOYMENT_APPROVAL.md` 절차로
  Authenticode 서명 후 hash를 다시 생성한다(기존 게이트 유지).
- 라이브 미확인 항목: 실제 프록시 키로 1차→2차 전환 1사이클, 2차 연속 403
  시 쿨다운 게이트 동작(유닛 테스트로는 검증됨), 빈 껍데기 회복률.

## 9. 재배포 기록 4 — 설정 탭 검증 흐름 보강 (2026-09-09)

- 커밋: 설정 탭 입력·검증 개선 — §2.1 의 신규 검증 경로 3건 포함
  (저장 전 토큰 검증 연동, 프록시 테스트 버튼, API 호출 비동기화)
- 빌드 환경: Windows Python 3.12, PyInstaller 6.21.0, 클린 `.venv-win`
- 빌드 내 자동 테스트: `Ran 453 tests — OK (skipped=2)` — 설정 탭 패널
  시나리오 12건 + `test_isp_proxy` 분류 5건 신규 포함
- Frozen 스모크: `SellerCollector.exe --verify-runtime` → exit 0
- 산출물(`C:\Users\dltnd\Desktop\gmarket_build_review_20260909\dist\`):

| 파일 | 크기(bytes) | SHA-256 |
|---|---:|---|
| `SellerCollector.exe` | 142,641,093 | `9d82b7b27c496150a8471f718276dd2ca41370393274ef061e4157ec47a23a4e` |
| `CoupangRuntimeSetup.exe` | 69,008,722 | `8911e02e7d9457ffc064bd86dbe7318a1e08a1e9e73c9bfe68f19d7a7532a928` |

- 라이브 미확인: 실제 자격으로의 프록시 테스트(출발 IP 표시)는 다음 실수집
  준비 시 1회 관찰 권장 — 유닛 테스트는 fake 응답으로 검증됨.
