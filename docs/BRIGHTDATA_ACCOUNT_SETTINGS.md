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
  (기본 `gm_unlocker`), 출발 국가(기본 `kr`).
- **잔액 조회·토큰 검증 버튼**: `GET https://api.brightdata.com/customer/balance`
  (Bearer 토큰) → 잔액·다음 청구 예정 표시. 401/403 은 "토큰 거부"로 안내.
  공식 문서: docs.brightdata.com — Account Management API "Total balance".
- **ISP 프록시 — Coupang 카테고리 탭(선택)**: 사용 체크박스, 계정 ID(`/status`
  의 `customer`, 예 `hl_22fb0228`), ISP 존명, 존 비밀번호. **"토큰으로 가져오기"**
  버튼은 `GET /zone/passwords?zone=<존명>`(2026-09-08 실측 엔드포인트)으로
  비밀번호를 자동 채운다.
- **저장**: 즉시 파일 반영. 토큰·비밀번호 위젯은 보안상 전체 값을 복원하지
  않고 마지막 4자리 마스크만 보여주며, 빈 상태로 저장하면 기존 값이 유지된다.
- 수집 실행 중에는 다른 탭과 같은 규율로 입력이 잠긴다.

### 2.2 Gmarket 카테고리 탭 연동 (Web Unlocker)

`GmarketCategoryLister.collect()` 가 실행 시점에 설정 파일을 읽어
`Authorization: Bearer <토큰>` + `{"zone": <입력 존>, "country": <입력 국가>}`
로 요청한다(기존 하드코딩 `config.BRIGHTDATA_ZONE` 는 입력이 없을 때의
폴백이 됨). 토큰이 전혀 없으면 설정 탭 안내와 함께 즉시 실패한다 —
요청당 약 $0.003 이 **입력된 계정 키**에서 차감된다.

### 2.3 Coupang 카테고리 탭 연동 (ISP 프록시)

- `CoupangRunConfig.proxy` (신규, playwright 형식 dict) — 설정 탭에서
  활성화 + 자격 완비(계정 ID/존/비밀번호)일 때만 `SearchRunConfig` 에 주입.
- `CoupangCrawler._create_browser()` 가 `Camoufox(..., proxy=...)` 로 전달.
  `geoip=True` 가 프록시 IP 기준 locale/타임존/지리를 자동 동기화한다
  (Akamai 교차 검증 신호 일관성 — BRIGHTDATA_AKAMAI_REVIEW §7.2 권장 조합).
- 영속 프로필 경로에도 동일하게 프록시가 적용된다(기동 실패 시 신규 세션
  폴백도 프록시 유지). 프록시 경유 사실은 로그에 사용자명만 남기고
  비밀번호는 기록하지 않는다.
- **기본값이 '끔'인 이유(실측)**: 목록 수집과 판매자 매핑 API는 프록시 IP로
  200 이지만, **판매자정보 API(`getStoreReview`)는 프록시 IP에서 403** 이다
  (BRIGHTDATA_AKAMAI_REVIEW §9). 사업자정보까지 수집하려면 프록시를 끄고
  회선 IP로 2차를 돌려야 한다. 대량 목록 수집으로 회선 IP를 보호할 때만 켠다.

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
