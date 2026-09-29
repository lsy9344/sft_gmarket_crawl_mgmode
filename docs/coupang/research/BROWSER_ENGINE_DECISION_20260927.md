# 쿠팡 수집 브라우저 운영 판단과 Patchright 워크트리 비교

- 조사일: 2026-09-27 KST
- 원인 확정 후속: 같은 날 심야 실측으로 차단 원인을 확정했다 — [Camoufox 쿠팡 차단 원인 확정](CAMOUFOX_BLOCK_ROOT_CAUSE_20260927.md). §4의 미확정 항목("엔진만의 단독 영향" 등)이 해당 문서에서 분리·확정됐다.
- 대상: 현재 Windows Sandbox 배포 앱과 `experiment/coupang-patchright-canary` 워크트리
- 근거: 현재 코드, 이번 세션의 실제 비교 시험, 실험 워크트리의 과거 기록, 공식 프로젝트 문서
- 현재 앱 수정 및 실행 검증: [9월 27일 수정 보고서](../CHROMIUM_CATEGORY_FIX_20260927.md)
- 도구 구조 및 공식 출처: [공식 자료 조사](BROWSER_ENGINE_PRIMARY_SOURCES_20260927.md)

## 1. 결론

**현재 쿠팡 카테고리는 처음부터 Patchright + Chromium으로 사용하는 것이 합리적이다.** 이번 Windows Sandbox에서 목록부터 판매자 정보 저장까지 성공을 확인한 조합이다. Camoufox를 먼저 실행해야 한다는 근거는 없다.

- 현재 앱에 Camoufox → Chromium 자동 전환은 없다.
- 쿠팡 카테고리만 Patchright + Chromium을 사용한다. Camoufox 패키지와 다른 기능의 실행 경로는 남아 있다.
- 발견한 Patchright 워크트리는 같은 도구로 **설치된 Google Chrome**을 실행하는 별도 실험이다.
- 그 워크트리에는 작은 묶음 수집·휴식·이어받기 기록이 있다. 현재 앱보다 성공률이 높다는 동일 조건 비교 자료는 없다.
- 장시간 안정성을 높이는 다음 판단에는 브라우저 이름뿐 아니라 작업량, 대기, 중단 원인, 이어받기 결과를 함께 봐야 한다.

## 2. 이름을 구분하기

| 이름 | 역할 | 현재 비교에서의 의미 |
|---|---|---|
| Camoufox | Firefox를 바탕으로 수정한 브라우저 및 실행 도구 | 기존 쿠팡 경로 |
| Chromium / Google Chrome | 웹사이트를 실제로 여는 브라우저 | 현재 앱은 설치 도구가 내려받은 Chromium, 실험 워크트리는 설치된 Chrome |
| Patchright | Chromium 계열 브라우저를 조작하는 Playwright 수정판 | 현재 앱과 실험 워크트리 모두 이미 사용 |

따라서 “Chromium이 안 되면 Patchright를 써 본다”는 비교는 정확하지 않다. 현재 코드는 이미 `patchright.sync_api`로 `chromium.launch_persistent_context()`를 호출한다. 별도로 비교할 후보는 **현재 Patchright + Chromium과 실험판 Patchright + Google Chrome의 차이**다.

근거: [현재 브라우저 생성 코드](../../../app/core/coupang/crawler.py), [Patchright 공식 Python 설명](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python#usage), [Playwright의 브라우저 구분](https://playwright.dev/docs/browsers).

## 3. 현재 앱에서 무엇을 쓰는가

| 기능 | 현재 브라우저 / 확인 방식 |
|---|---|
| Coupang 카테고리의 상품 목록 | Patchright + Chromium, Decodo 한국 회선 |
| 같은 작업의 판매자 정보 | Patchright + Chromium, 이 PC 직접 회선 |
| 일반 Coupang 탭의 기존 수집 | Camoufox 기본값 유지 |
| 쿠팡 로그인 | Camoufox 유지 |
| 카테고리 목록 새로고침 | Camoufox 유지. 저장된 목록을 이용한 수집과 별도 경로 |
| 설정의 Decodo 저장 검사 | Decodo 연결·한국 회선 확인 + 실제 수집용 Chromium 설치 확인. 쿠팡 접속 성공 보장은 아님 |

코드 근거: [카테고리 실행 설정](../../../app/core/coupang/decodo_run.py), [기본 브라우저 설정](../../../app/models/coupang_records.py), [로그인](../../../app/core/coupang/login.py), [카테고리 목록 갱신](../../../app/core/coupang/categories.py), [설정 검사](../../../app/ui/widgets/brightdata_panel.py).

카테고리 실행은 `browser_engine="chromium"`을 지정한다. 새 회선에는 새 Chromium 프로필을 만들고, 목록용 `proxy`와 판매자용 `direct` 폴더를 분리한다. 진행 DB와 결과는 브라우저 프로필과 따로 보존한다.

재시도도 브라우저 자동 전환과 구분해야 한다. 현재는 재시도 가능한 실패이면 90초 뒤 **같은 Patchright + Chromium에서 다른 Decodo 회선으로**, 최초 실행 포함 최대 3회 시도한다. 한국 회선 확보 과정은 별도 최대 3회 확인이다. 직접 회선 차단·진행 기록 오류·사용자 취소 등은 재시도에서 제외한다. 설정 저장 성공을 모든 쿠팡 기능의 접속 성공으로 해석하면 안 된다.

## 4. 이번 시험에서 확인한 것과 모르는 것

| 증거 | 판단할 수 있는 내용 | 판단할 수 없는 내용 |
|---|---|---|
| 호스트 Windows에서 같은 Decodo 세션/IP로 Camoufox 홈 403, Patchright Chromium으로 실제 수집 성공 | IP 하나만으로 차이를 설명할 수 없음 | 쿠팡이 검사한 정확한 항목, 브라우저 엔진만의 단독 영향 |
| Sandbox의 새 앱에서 432481 카테고리 2페이지, 상품 14개·판매자 정보 14명 저장, 오류 0 | 실제 배포 환경에서 작은 작업이 끝까지 동작함 | 모든 카테고리·모든 PC·장시간 성공률 |
| 기존 완료 1/13개를 유지하고 2번째 카테고리에서 상품 5개 + 9개 수집 | 기존 결과 사용과 하위 카테고리 재개가 동작함 | 13개 전체 작업의 최종 완료 |
| Edge + Bright Data ISP에서 HTTP 200이지만 본문은 Access Denied | 상태 코드만으로 정상 화면을 판단할 수 없음 | 모든 Bright Data 상품/회선의 성능 |

브라우저 라이브러리·실제 브라우저·프로필·실행 설정은 함께 바뀐 부분이 있다. 따라서 “Camoufox라는 이름 때문에 차단됐다”, “Windows에서 Linux 지문이 나와서 반드시 차단됐다”, “Chromium이면 앞으로 막히지 않는다”는 결론은 내리지 않는다. 자세한 실험 환경과 파일은 [수정 보고서](../CHROMIUM_CATEGORY_FIX_20260927.md)에 기록했다.

## 5. 찾은 Patchright 워크트리

- 위치: `/home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode-patchright-canary`
- 브랜치: `experiment/coupang-patchright-canary`
- 조사 시 HEAD: `caf003b695979b1035b753e91403008be5b249ef`
- 현재 메인 HEAD: `6dee2dcf20bae8232bc52d8ef292ca4dedc06807` + 9월 27일 미커밋 수정
- 실험 워크트리에도 미커밋 코드·문서·새 파일이 있다. HEAD만 보면 최신 기록을 놓친다.

| 항목 | 현재 Sandbox 배포 앱 | Patchright 실험 워크트리 |
|---|---|---|
| 조작 도구 | Patchright | Patchright |
| 브라우저 | 설치 도구가 내려받은 Chromium | `channel="chrome"`으로 설치된 Google Chrome |
| 실행 화면 | 창 표시, `ko-KR`, 1280×800 | 창 표시, `no_viewport=True` |
| 회선 | 목록 Decodo → 판매자 직접 회선 | 확인한 생성 함수에는 프록시 인자 없음, 직접 접속 |
| 브라우저 기록 | 회선별 새 폴더, 프록시/직접 접속 분리 | 전용 Chrome 프로필 재사용 |
| 작업 범위 | 선택한 카테고리와 하위, 카테고리당 최대 200페이지 | 과일 12개 분류 중심, 카테고리당 최대 50페이지 |
| 실행량 | 기존 수집 흐름과 페이지 간 지연, 실패 시 제한된 회선 재시도 | 목록을 작은 묶음으로, 판매자 처리도 묶음으로 실행하고 예약 간격을 둠 |
| 이어받기 기록 | 현재 앱의 카테고리별 SQLite | 별도 JSON 상태·결과·예약 실행 기록 |
| 배포 방식 | 새 PC용 EXE와 런타임 설치 도구 | 개발 PC의 Python·Chrome·전용 경로·예약 스크립트에 의존하는 실험 코드 |

근거: [실험 브라우저 생성](../../../../sft_gmarket_crawl_mgmode-patchright-canary/app/core/coupang/patchright_canary.py), [실험 목록 수집](../../../../sft_gmarket_crawl_mgmode-patchright-canary/app/core/coupang/patchright_full_fruit.py), [실험 판매자 수집](../../../../sft_gmarket_crawl_mgmode-patchright-canary/app/core/coupang/patchright_full_sellers.py), [예약 실행 스크립트](../../../../sft_gmarket_crawl_mgmode-patchright-canary/scripts/prototypes/run_patchright_fruit_pipeline.ps1).

따라서 워크트리를 통째로 적용하는 것은 브라우저 하나를 바꾸는 작업보다 범위가 크다. 서로 다른 진행 파일을 그대로 호환된다고 가정할 수도 없다.

### 과거 기록에서 배울 점

[실험 보고서](../../../../sft_gmarket_crawl_mgmode-patchright-canary/docs/coupang/PATCHRIGHT_EXPERIMENT_REPORT.md)의 날짜별 후속 기록까지 읽었다. 상단 요약은 초기 시점 내용이므로 최신 상태로 해석하지 않았다.

- 9월 1일: 초기 14회에서 명시적 차단이 없었지만, 다음 연속 수집에서 감/홍시/곶감 2페이지가 HTTP 403으로 중단됐다.
- 9월 3~4일: 95분 간격의 판매자 묶음 9회 정상 처리 기록이 있다.
- 9월 8일: 61분 시험 5회와 후속 7회가 성공한 뒤, 다음 판매자 작업의 12번째 명시 API 요청에서 HTTP 503으로 중단됐다. 503 하나로 IP 차단을 확정할 수 없다.
- 9월 8일 16:06 기록: 80분 간격 작업 6회 성공, 카테고리 9/12개, 상품 7,426개·판매자 1,563명 누적. 이는 그 시점 기록이며 전체 완료 증거가 아니다.

이 수치는 **이번에 다시 실행해 얻은 값이 아니라 과거 보고서에서 확인한 값**이다. 보고서가 가리키는 `C:\Users\dltnd\Desktop\PatchrightFruit`는 이번 확인에서 존재하지 않았고, 두 워크트리에서도 해당 최종 결과 JSON을 찾지 못했다. 원본 데이터 전량 재검증이나 과거 예약의 현재 동작 확인은 수행하지 않았다.

조사한 미커밋 실험 보고서의 SHA-256: `ebed8d5bfe24eeb22229cde2ddee42a56bd7ad493d9cef05ef5e981237b0a63b`.

**시사점:** Patchright + Chrome에도 성공과 중단이 모두 있었다. 작은 묶음과 대기를 포함한 운영 기록은 참고할 가치가 있지만, 80분이 모든 PC에 맞는 정답이거나 현재 앱보다 항상 성공률이 높다는 뜻은 아니다.

## 6. Camoufox부터 시도하고 자동 전환해야 하는가

**현재 권고는 아니다.** 성공이 확인된 기본 경로를 먼저 쓰는 것이 타당하다. 최근 실패했던 경로를 매번 먼저 실행하면 시작이 늦어지고 실패 요청과 복구 단계만 추가될 수 있다.

자동 전환을 나중에 넣으려면 별도 비교와 구현 검증이 필요하다.

1. 연결/인증/설치/저장 오류와 사이트의 접속 거절을 구분한다. 비밀번호·잔액·설치 누락·쓰기 권한 문제는 브라우저 교체로 해결하지 않는다.
2. 같은 PC·회선·카테고리·작업량으로 후보를 비교한다. Chrome과 Chromium을 비교하면서 동시에 속도·프로필 정책까지 바꾸면 원인을 분리할 수 없다.
3. 홈 화면뿐 아니라 목록 읽기·판매자 저장·중단 후 재개까지 비교한다. 일시적인 성공을 성공률 수치로 바꾸지 않는다.
4. 대체 브라우저가 실제로 복구에 도움이 되는 경우를 확인한 뒤, 횟수·대기·중단 조건을 정한다. 인증 실패나 저장 실패까지 모든 오류에서 브라우저를 순환시키지 않는다.
5. 브라우저 프로필은 분리하고 진행 기록은 공유 가능한 형식으로 유지한다. 중복 저장과 누락, 종료 정리를 검증한다.

공식 Patchright 저장소는 실제 Chrome을 권장한다. 이것은 Chrome을 비교 후보로 삼을 근거이며, 우리 Sandbox에서 현재 Chromium보다 더 잘된다는 실측 증거는 아니다. [공식 권장 설명](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python#best-practice---use-chrome-without-fingerprint-injection).

## 7. 사용자가 앞으로 쓰는 순서

1. 새 PC에서는 최신 앱과 `CoupangRuntimeSetup.exe`를 준비하고, 앱을 사용할 같은 Windows 계정으로 런타임을 한 번 설치한다. `SellerCollector.exe --setup-runtime`도 같은 설치 진입점이다.
2. 설정에서 Decodo 계정을 저장해 연결·설치 검사 결과를 확인한다.
3. 쿠팡 카테고리에서 원하는 분류, 필터와 쓸 수 있는 출력 폴더를 선택하고 수집을 시작한다. 브라우저를 직접 고를 필요는 없다.
4. 중단했다가 다시 할 때는 같은 출력 폴더를 사용하고 안내에서 **이어서 수집**을 선택한다.
5. 최대 페이지에 도달한 '일부 수집', 인증 실패, 설치 오류, 접속 거절을 구분한다. 접속 거절이 반복되면 로그의 실패 단계와 저장 상태를 기준으로 다음 조치를 정한다.

설정의 연결·설치 확인은 현재 쿠팡 사이트 접근 성공이나 장시간 수집 완료까지 보증하지 않는다. 현재 확보한 확실한 근거는 9월 27일 Sandbox의 2페이지 저장 성공과 기존 기록 재개 성공이다.
