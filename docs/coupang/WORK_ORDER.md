# 작업지시서: Gmarket 앱 Coupang 탭 통합

> 문서 버전: 1.0  
> 작성일: 2026-07-26  
> 상태: 구현 착수 가능  
> 범위: **정상 동작 중인 Gmarket PyQt6 앱에 Coupang 수집 탭을 추가**하고,
> 검증 완료된 Coupang v3 크롤러를 UI·워커·배포 구조에 통합한다.  
> 구현 기준: `docs/coupang/CRAWL_RESULTS.md`와
> `coupang_crawl/coupang_omp_crawler.py`가 최우선 진실 공급원이다
> (`docs/coupang/README.md:15-28`, `docs/coupang/CRAWL_RESULTS.md:3-10`).

---

## 0. 실행 지시 요약

이번 작업은 Coupang 수집 방식을 다시 연구하거나 새로 설계하는 작업이 아니다.
이미 실측에 성공한 v3 파이프라인을 **동작 변경 없이 먼저 엔진화**하고, 기존
Gmarket UI와 동일한 QThread 경계 안에서 실행되도록 연결하는 작업이다.

반드시 지킬 결론은 다음과 같다.

1. 메인 화면을 `QTabWidget` 기반의 **Gmarket / Coupang 2탭**으로 만든다.
2. Gmarket 코드 경로·저장 형식·버튼 상태·종료 안전성은 바꾸지 않는다.
3. Coupang은 `/np/omp`의 **'전체' 탭 하나만** 수집한다.
4. Coupang의 유일한 수집 경로는
   `getPromotion → individualInfo/products → getStoreReview` 3-API 흐름이다.
5. 상품 상세페이지, 카테고리/검색 페이지, 정적 CSS 셀렉터, 공정위 DB,
   프록시 필수화, Selenium 대체 경로는 이번 범위에 넣지 않는다.
6. 브라우저는 UI 스레드가 아니라 Coupang 전용 `QThread` 안에서 생성·사용·종료한다.
7. 플랫폼 간 동시 수집은 금지하고, 앱 전체에서 한 번에 워커 하나만 실행한다.
8. 성공·취소·오류 어느 경로에서도 브라우저를 닫고 이미 확보한 결과를 보존한다.
9. 자동 테스트에서는 실제 Coupang 네트워크를 호출하지 않는다. 실환경 검증은
   별도 수동 스모크/E2E 단계에서만 수행한다.
10. 아래 Acceptance Criteria를 모두 통과하기 전에는 완료로 판정하지 않는다.

---

## 1. 목표, 산출물, 비목표

### 1.1 목표

- 사용자가 한 앱에서 Gmarket 또는 Coupang을 탭으로 선택해 수집할 수 있다.
- Coupang 탭에서 v3의 실행 설정을 입력하고 시작·일시정지·재개·취소할 수 있다.
- 진행 단계, 상품 수, 판매자 수, 성공/스킵/오류 수, 로그와 수집 결과를 UI에서
  확인할 수 있다.
- Coupang 결과를 사용자가 선택한 폴더에 JSON(`utf-8`)과 CSV(`utf-8-sig`)로
  저장한다.
- 기존 CLI도 같은 코어 엔진을 호출하도록 유지해 UI와 CLI가 서로 다른
  크롤링 구현으로 갈라지지 않게 한다.

### 1.2 필수 산출물

- Coupang Qt 비의존 코어 엔진
- Coupang 실행 설정/결과 요약 데이터 계약
- Coupang `QThread` 워커
- Coupang 탭 패널과 결과 테이블
- MainWindow의 2탭 구성 및 전역 단일-워커 조정
- 성공·취소·오류 시 안전한 JSON/CSV 저장
- Camoufox 의존성/브라우저 설치 및 PyInstaller 반영
- 네트워크 없는 단위/통합/UI 회귀 테스트
- WSL2/Linux 및 Windows 배포 스모크 기록
- 기존 CLI 호환 어댑터와 사용자 문서 갱신

### 1.3 비목표

다음은 이번 작업에서 구현하지 않는다.

- Coupang 카테고리/검색/상품 상세페이지 수집
- `/vp/products/*` DOM 또는 XHR 파싱
- 공정위/외부 DB 연동과 퍼지 매칭
- 프록시 구매, 프록시 회전, 공유기 재부팅 자동화
- Selenium 3.9/Chromium 108 대체 실행기
- 과거 rev.1~5의 4단계 Stage A~D UI
- 가격·할인·별점·로켓배송 등 v3 결과에 없는 상품 분석 필드
- 실행 간 수집 이력, 재개 큐, 일일 예산, 장기 스케줄러
- Gmarket과 Coupang 간 사업자번호 교차 중복 제거
- 현재 CLI에 없는 자동 재시도/자가 우회 기능의 임의 추가

---

## 2. 분석 결론과 신뢰도

| 우선순위 | 결론 | 신뢰도 | 근거 |
|---|---|---|---|
| 1 | UI에는 검증된 v3 3-API 파이프라인을 그대로 이식해야 한다. | 높음 | 실측 문서가 이를 단일 진실 공급원으로 지정하고, 구현 코드와 156명 수집 결과가 일치한다 (`CRAWL_RESULTS.md:3-10,56-105,108-135`; `coupang_omp_crawler.py:48-143,170-294`). |
| 2 | 가장 큰 구현 위험은 크롤링 로직이 아니라 UI 스레드·취소·종료·패키징 경계다. | 높음 | 기존 앱은 QThread와 종료 경쟁 조건을 명시적으로 방어하지만, v3 CLI는 동기 실행·`print`·`time.sleep` 기반이고 취소 계약이 없다 (`app/workers/crawl_worker.py:25-80`; `app/ui/main_window.py:494-565`; `coupang_omp_crawler.py:145-330`). |
| 3 | 구형 연구 문서의 아키텍처 중 코어/워커/UI 분리와 보수적 웜업만 재사용할 가치가 있다. | 높음 | 6개 연구 문서는 폐기됐다고 스스로 명시하지만, Qt 비의존 코어와 워커 경계는 현재 Gmarket 구조에도 부합한다 (`docs/coupang/README.md:24-37`; `research/IMPLEMENTATION_ARCHITECTURE.md:35-77`). |
| 4 | 프록시·상세페이지·D3 사업자번호 병합을 이번 UI 통합에 끌어오면 검증된 v3와 다른 제품이 된다. | 높음 | 프록시와 상세페이지 전제는 rev.6에서 폐기됐고 현재 v3에도 D3 병합이 없다 (`CRAWL_RESULTS.md:43-52`; `coupang_omp_crawler.py:243-294`). |
| 5 | Windows PyInstaller에서 Camoufox 브라우저 자산이 정상 탐색되는지는 아직 미확정이다. | 중간 | 실측 환경은 WSL2+xvfb이며 현재 spec은 Camoufox를 수집하지 않는다 (`CRAWL_RESULTS.md:108-114`; `pyinstaller.spec:21-45`). |

### 확인된 사실(Evidence)

- 현재 앱은 단일 `MainWindow`에 Gmarket 위젯을 직접 배치하며 탭이 없다
  (`app/ui/main_window.py:34-121`).
- 현재 Gmarket 워커는 caller 소유 `Control`을 받아 엔진 콜백을 Qt signal로
  중계한다 (`app/workers/crawl_worker.py:25-80`,
  `app/workers/prescan_worker.py:20-59`).
- 창 닫기는 실행 중 워커에 취소를 요청한 뒤 실제 `finished`가 올 때까지
  보류한다 (`app/ui/main_window.py:494-565`).
- Coupang v3는 Camoufox headed 세션 하나에서 웜업, 요청 템플릿 캡처,
  3-API 호출, 결과 저장을 순서대로 수행한다
  (`coupang_omp_crawler.py:170-326`).
- 실측은 약 180개 상품, 156명 판매자, 0건 차단이며 필수 필드 7종을 확보했다
  (`CRAWL_RESULTS.md:108-135,160-218`).

### 설계 판단(Inference/Decision)

- Gmarket UI를 별도 패널로 대규모 이동하지 않고, 기존 위젯 트리를 첫 번째
  탭에 감싸는 방식이 가장 작은 무회귀 변경이다.
- Coupang은 별도 코어/워커/패널로 격리하되 MainWindow가 전역 동시 실행과
  창 닫기를 조정해야 한다.
- 기존 v3의 출력 단위인 `vendor_id`별 1행을 유지한다. 구형 문서의
  사업자번호 D3 병합은 현재 코드·실측 보고서의 실행 계약에 없으므로 별도
  요구가 생기기 전까지 추가하지 않는다.

### 미확정(Unknown)

- Windows GUI/PyInstaller 번들에서 Camoufox 실행 파일과 GeoIP DB를 어떤
  배포 방식으로 제공할지
- Coupang 정책/응답 변화에 따른 장기 안정성
- `/np/omp` 접근의 운영·법무 정책 판단. 기술적으로 수집이 가능하다는 사실과
  배포/운영 승인은 별개다.

미확정 항목은 추측으로 코드에 고정하지 말고 §11·§13의 릴리스 게이트로
검증한다. 특히 `/np/omp`는 과거 조사에서 robots.txt 명시 허용 경로가 아닌
것으로 기록돼 있으므로 (`PAGE_STRUCTURE.md:261-274`), 제품/운영 책임자의
서면 승인 기록이 없으면 라이브 E2E와 릴리스를 진행하지 않는다.

---

## 3. 문서 우선순위와 충돌 해소 규칙

### 3.1 진실 공급원 순서

구현 중 문서끼리 충돌하면 아래 순서로 판단한다.

1. `docs/coupang/CRAWL_RESULTS.md` rev.6 — 문서가 선언한 최우선 기준
2. 실제 동작 코드 `coupang_crawl/coupang_omp_crawler.py` v3.0 — 본 작업지시서가
   정한 실행 동작 기준
3. `docs/coupang/README.md`와 나머지 현재 문서의 rev.6 정정문 — 본 작업지시서가
   정한 보조 문서 기준
4. 현재 Gmarket 앱 코드와 테스트가 보장하는 통합 계약 — 본 작업지시서가 정한
   무회귀 기준
5. `docs/coupang/research/` rev.1~5 — 문서가 선언한 역사·위험 참고 자료

`README.md`는 rev.6이 research rev.1~5보다 우선한다고 직접 선언한다
(`docs/coupang/README.md:15-28,39-49`). 그 사이의 세부 우선순위 2~4는
동작 코드 보존과 Gmarket 무회귀를 위해 **본 작업지시서가 내린 결정**이다.

### 3.2 충돌별 최종 지시

| 쟁점 | 폐기된 주장 | 이번 작업의 확정 지시 |
|---|---|---|
| 대상 | 카테고리/검색/상세페이지 | `/np/omp` '전체' 탭 하나만 사용 (`CRAWL_RESULTS.md:25-39`). |
| 페이지네이션 | `?page=N` | 캡처한 `getPromotion` 요청 본문과 `continuationToken`/`nextPageKey`를 사용 (`CRAWL_RESULTS.md:65-73`). |
| 사업자정보 | 상세페이지 하단 DOM/XHR | `getStoreReview` API만 사용 (`CRAWL_RESULTS.md:75-84`). |
| 상세페이지 | 필수 수집 경로 | 항상 403이므로 호출 금지 (`CRAWL_RESULTS.md:43-52,222-230`). |
| 판매자 매핑 | 리스팅 CSS 셀렉터 | `individualInfo/products`의 `storeInfoArea` 사용 (`CRAWL_RESULTS.md:75-79,90-105`). |
| 이메일 | 수집 불가 | `repEmail` 매핑, 실측 100% (`CRAWL_RESULTS.md:94-104,123-135`). |
| 프록시 | 실행 필수/fail-closed | 기본값·필수 설정에서 제거. 검증 환경에서는 무프록시 성공 (`CRAWL_RESULTS.md:43-50,108-114`). |
| 브라우저 API | Scrapling `StealthySession` | 현재 검증 코드의 `camoufox.sync_api.Camoufox` 직접 사용 (`coupang_omp_crawler.py:25,170-180`). |
| 파워셀러 | DOM 배지 탐색 | `qualitySellerBadgeDto`를 bool/title로 매핑 (`CRAWL_RESULTS.md:90-105`). |
| URL | 수집 불가/상품 단위 | 판매자 대표 상품의 `productId`/`itemId`/`vendorItemId` 조합 (`CRAWL_RESULTS.md:196-218`). |
| 중복 제거 | 상품→스토어→사업자번호 3단계 | 이번 범위는 v3와 동일하게 `vendorItemId` 및 `vendorId` 실행 내 중복만 제거한다 (`coupang_omp_crawler.py:202-252`). |
| 실행 UI | Stage A~D 별도 버튼 | 검증된 파이프라인이 하나의 연속 흐름이고 피드가 유한하므로 단일 `수집 시작` 흐름으로 구현한다. 이는 UI 결정이다 (`CRAWL_RESULTS.md:56-88,160-192`). |

### 3.3 연구 문서에서 재사용 가능한 원칙

폐기 문서의 아래 원칙만 현재 증거와 충돌하지 않는 범위에서 재사용한다.

- 웜업은 단순 대기가 아니라 실제 마우스/스크롤 상호작용이어야 한다
  (`research/BYPASS_TECHNICAL_GUIDE.md:279-342`;
  현재 구현 `coupang_omp_crawler.py:33-45,182-193`).
- 브라우저 세션 안에서 쿠키·디바이스 핑거프린트·API 호출을 일관되게 유지한다.
- 코어는 Qt에 의존하지 않고, QThread 워커가 callback→signal 브리지를 담당한다
  (`research/IMPLEMENTATION_ARCHITECTURE.md:35-77`).
- 랜덤 딜레이와 보수적 요청 속도는 유지하되, 미검증 프록시/핑거프린트 조작은
  넣지 않는다.
- 브라우저와 요청 오류는 감추지 말고 운영 로그와 결과 요약에 남긴다.

---

## 4. 무회귀 불변식

### INV-1. Gmarket 동작 보존

- Gmarket의 사전 조사 → 계획 → 수집 흐름을 바꾸지 않는다.
- 기존 `SettingsPanel`, `PrescanWorker`, `CrawlWorker`, `Storage`, 출력 파일명과
  JSON/CSV 필드 의미를 바꾸지 않는다.
- 기존 MainWindow 속성명과 테스트가 참조하는 버튼 속성은 유지한다.

### INV-2. UI 스레드 비차단

- Camoufox 생성, 페이지 이동, `evaluate`, 지연, 파일 저장은 모두 Coupang
  워커 스레드에서 실행한다.
- UI 스레드는 signal 처리와 위젯 갱신만 한다.

### INV-3. 단일 실행

- Gmarket prescan/crawl 또는 Coupang crawl 중 하나가 실행 중이면 다른 플랫폼의
  시작 버튼을 비활성화한다.
- 탭 전환과 로그/결과 열람은 허용한다.

### INV-4. 종료 안전성

- 창 닫기 중 새 워커를 만들지 않는다.
- 실행 중 닫기 확인에서 사용자가 동의하면 해당 `Control`에 취소를 요청하고,
  워커의 실제 종료 signal을 받은 뒤에만 창을 닫는다.
- 브라우저 컨텍스트는 성공·취소·예외 모두에서 닫힌다.

### INV-5. 데이터 보존과 격리

- Gmarket 상태 파일과 Coupang 결과/부분 결과가 이름이나 경로로 충돌하지 않는다.
- 실제 사업자정보, 쿠키, `_abck`, 요청 템플릿은 저장소에 커밋하지 않는다.
- 로그에는 쿠키·전체 요청 본문·브라우저 핑거프린트 원문을 출력하지 않는다.

### INV-6. 검증된 수집 계약 보존

- `getPromotion` 성공은 반드시 `str(ret) == "0"`으로 판정한다
  (`coupang_omp_crawler.py:67-74`).
- 커서 소진, 빈 결과, 신규 0건, 동일 토큰을 정상 종료 조건으로 유지한다
  (`coupang_omp_crawler.py:202-228`).
- BrandSeller의 null 사업자정보는 오류 레코드로 만들지 않고 스킵 수로 집계한다
  (`CRAWL_RESULTS.md:222-230`; `coupang_omp_crawler.py:289-292`).

---

## 5. 목표 아키텍처

```text
MainWindow
├─ QTabWidget
│  ├─ Gmarket 탭: 기존 위젯/핸들러를 그대로 감쌈
│  └─ Coupang 탭: CoupangPanel
├─ 기존 PrescanWorker / CrawlWorker
└─ CoupangWorker
      └─ CoupangCrawler (Qt 비의존)
           ├─ Camoufox session + page
           ├─ getPromotion
           ├─ individualInfo/products
           ├─ getStoreReview
           └─ CoupangExporter → JSON/CSV
```

### 5.1 책임 분리

| 계층 | 책임 | 금지 |
|---|---|---|
| `MainWindow` | 탭 조립, 전역 단일-워커 정책, 창 닫기 조정 | Coupang API 파싱/파일 저장 |
| `CoupangPanel` | 입력 검증 전 표시, 버튼/진행/로그/표 렌더링 | 브라우저 실행, 네트워크 호출 |
| `CoupangWorker` | 백그라운드 실행, callback→signal 변환, 최종 summary 보관 | 데이터 파싱 규칙 복제 |
| `CoupangCrawler` | v3 파이프라인, 취소 safe point, 결과/요약 생성 | PyQt import, QMessageBox, 위젯 접근 |
| `CoupangExporter` | 안정된 필드 순서와 인코딩으로 원자적 저장 | Gmarket 상태 파일 사용 |
| CLI adapter | argparse→RunConfig 변환, 콘솔 callback, exit code | 별도 크롤러 구현 유지 |

### 5.2 권장 파일 구조

```text
app/
├─ core/
│  ├─ base.py                         # Control의 중단 가능 sleep 보강
│  └─ coupang/
│     ├─ __init__.py
│     ├─ crawler.py                   # v3 엔진 + API helper
│     └─ exporter.py                  # JSON/CSV 원자 저장
├─ models/
│  └─ coupang_records.py              # RunConfig, Record, RunSummary
├─ workers/
│  └─ coupang_worker.py               # QThread 브리지
└─ ui/
   └─ coupang_panel.py                # Coupang 전용 UI

coupang_crawl/
└─ coupang_omp_crawler.py             # 코어 엔진을 호출하는 CLI 호환 어댑터

tests/
├─ test_coupang_crawler.py
├─ test_coupang_exporter.py
├─ test_coupang_worker.py
├─ test_coupang_panel.py
└─ test_main_window.py                # 2탭/전역 worker/close 회귀 추가
```

구형 `src/coupang_crawl.py`와 다른 실험 스크립트는 이번 작업에서 삭제하거나
UI에서 import하지 않는다. 별도 정리 작업 전까지 역사 자료로 둔다.

---

## 6. 코어 데이터 계약

### 6.1 `CoupangRunConfig`

최소 필드는 다음과 같다.

| 필드 | 타입 | 기본값 | 검증 |
|---|---|---:|---|
| `output_dir` | `Path` | UI 선택값 | 생성/쓰기 가능해야 함 |
| `output_prefix` | `str \| None` | timestamp 기반 | 경로 구분자 금지, 빈 값은 기본명 |
| `max_scroll_pages` | `int` | 10 | 1 이상 |
| `batch_size` | `int` | 10 | 1 이상 |
| `warmup_time` | `float` | 20 | 0 이상 |
| `delay_min` | `float` | 1.0 | 0 이상 |
| `delay_max` | `float` | 2.5 | `delay_min` 이상 |

기본값은 현재 CLI와 실측 실행값을 유지한다
(`coupang_omp_crawler.py:145-155`; `CRAWL_RESULTS.md:233-252`).

### 6.2 `CoupangRecord`

출력 순서를 아래와 같이 고정한다.

1. `vendor_id`
2. `url`
3. `store_name`
4. `company_name`
5. `ceo_name`
6. `business_number`
7. `phone`
8. `email`
9. `address`
10. `ecommerce_report_number`
11. `power_seller`
12. `power_seller_title`
13. `rating_count`
14. `thumb_up_ratio`

필드 매핑은 현재 코드와 실측 표를 그대로 따른다
(`coupang_omp_crawler.py:261-284`; `CRAWL_RESULTS.md:90-105`).
구형 문서의 `price`, `original_price`, `rocket_delivery`, `resolved`,
`parse_method`, `store_names`, `product_count` 등은 추가하지 않는다.

### 6.3 `CoupangRunSummary`

최소 필드는 다음과 같다.

- `products_seen`
- `unique_vendors`
- `business_info_success`
- `brand_seller_skipped`
- `request_errors`
- `power_sellers`
- `store_name_present`
- `store_name_missing`
- `store_name_coverage` — `present / business_info_success`, 0건이면 0.0
- `duplicate_business_numbers_observed`
- `records`
- `json_path`, `csv_path`
- `cancelled`
- `error`
- `termination_reason`

`termination_reason`은 최소 `token_exhausted`, `no_new_items`,
`page_limit`, `no_items`, `template_not_captured`, `cancelled`, `error`를
구분한다. 정상 종료와 조용한 실패를 같은 0건 완료로 표시하지 않는다.

### 6.4 Callback 계약

Qt 비의존 코어는 다음 callback을 선택적으로 받는다.

- `on_phase(phase_name, current_phase, total_phases)`
- `on_progress(kind, current, total)`
- `on_log(message)`
- `on_record(record_dict)`
- `on_stats(summary_snapshot)`

콜백 예외가 브라우저/수집 루프를 죽이지 않도록 엔진 경계에서 격리하고 로그만
남긴다. Gmarket의 `_safe_callback` 철학을 재사용한다
(`app/core/crawler.py:549-554`).

---

## 7. Coupang 엔진 구현 지시

### 7.1 먼저 현재 v3 동작을 회귀 테스트로 잠근다

코드를 옮기기 전에 fake page/browser로 아래 현재 동작을 테스트한다.

- `getPromotion` 요청이 캡처 템플릿을 재사용하고 두 커서 값을 함께 갱신한다.
- 성공 `ret`가 문자열/정수 어느 쪽이어도 `str(...) == "0"`으로 처리된다.
- HTML 응답, 비정상 JSON, HTTP 오류는 빈 결과/명시적 오류로 분류된다.
- `individualInfo/products`가 `storeInfoArea`를 vendor ID별로 축약한다.
- `getStoreReview` 필드가 §6.2 스키마에 정확히 매핑된다.
- 대표 상품 URL 조합 규칙이 유지된다.
- BrandSeller null 결과는 스킵된다.
- vendorItemId 및 vendorId 실행 내 중복 제거가 유지된다.

### 7.2 추출 순서

1. 현재 v3 함수와 흐름을 `app/core/coupang/crawler.py`로 이동한다.
2. `print`를 callback으로 치환한다.
3. argparse/프로세스 종료를 코어에서 제거한다.
4. 브라우저 생성부는 주입 가능한 factory로 감싸 자동 테스트에서 fake를 쓴다.
5. 코어가 Camoufox를 실행하는 시점에 지연 import한다. Camoufox가 없으면 앱
   전체가 죽지 않고 Coupang 시작 시 설치 안내 오류를 반환한다.
6. 기존 CLI는 `CoupangRunConfig`와 콘솔 callback을 만들어 같은 엔진을 호출한다.
7. 기존 CLI 옵션명·기본값·JSON/CSV 결과를 유지한다.

### 7.3 브라우저와 세션

- `Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True)`를 유지한다
  (`coupang_omp_crawler.py:170-180`).
- 하나의 run은 하나의 browser/page/session만 사용한다.
- 홈 웜업 후 동일 page로 `/np/omp`와 shop API를 사용한다.
- request listener는 최초 `getPromotion` POST 본문만 캡처한다.
- 요청 템플릿/쿠키를 파일이나 로그에 쓰지 않는다.
- `with` 또는 동등한 `try/finally`로 브라우저를 항상 닫는다.

### 7.4 정확한 API 흐름

1. `coupang.com` 홈으로 이동하고 자연 상호작용을 수행한다.
2. `/np/omp`로 이동해 `getPromotion` POST 템플릿을 캡처한다.
3. 캡처 실패 시 `template_not_captured`로 실패하고 vendor API를 호출하지 않는다.
4. `getPromotion`을 커서로 반복하며 `vendorItemId` 기준 중복 제거한다.
5. 빈 items, 신규 0, 빈 next token, 동일 next token이면 정상 피드 종료한다.
6. page 상한 때문에 멈췄으면 `page_limit`으로 기록해 자연 종료와 구분한다.
7. shop 세션을 만든 뒤 `batch_size` 단위로 `individualInfo/products`를 호출한다.
8. `vendorId`별 대표 상품 메타데이터를 만든다.
9. 정렬된 vendor ID 순서로 `getStoreReview`를 호출한다.
10. `name`이 있는 응답만 `CoupangRecord`로 만들고, BrandSeller null과 오류를
    서로 다른 카운터/로그로 남긴다.
11. 최종 또는 부분 결과를 저장하고 summary를 반환한다.

### 7.5 취소와 일시정지

`Control`에 `sleep(seconds, poll_interval=0.1)` 같은 중단 가능한 분할 대기를
추가하고 Coupang의 모든 명시적 대기를 이 메서드로 치환한다. 기존 Gmarket의
`time.sleep`을 이번 작업에서 기계적으로 바꾸지 않는다.

safe point는 다음 위치에 둔다.

- 웜업 상호작용 루프의 각 action 전후
- 각 page 이동 직후
- 각 `getPromotion` page 전후
- 각 vendor batch 전후
- 각 `getStoreReview` 전후
- 랜덤 delay 중
- 저장 직전

일시정지는 새 요청을 시작하지 않는 상태다. 이미 진행 중인 `page.goto` 또는
`page.evaluate`를 강제 중단하지 않는다. 취소도 현재 브라우저 호출이 반환된 뒤
가장 가까운 safe point에서 적용한다. UI 문구는 이 한계를 숨기지 않고
“현재 요청 완료 후 취소”로 표시한다.

### 7.6 오류 처리

- 현재 helper의 넓은 `except Exception: return {}`는 제거하거나 최소한 오류를
  summary/log에 남긴다 (`coupang_omp_crawler.py:77-121,124-142`).
- 비정상 HTTP, HTML challenge, JSON decode, API code 오류, template capture 실패,
  output write 실패를 구분한다.
- 자동 우회나 프록시 전환을 시도하지 않는다.
- 일부 vendor 실패는 전체 run을 중단하지 않되, 초기 세션/템플릿/저장 실패는
  run 실패로 처리한다.
- 파일 저장 실패를 성공 완료로 표시하지 않는다.

---

## 8. 저장 및 결과 계약

### 8.1 출력 위치와 이름

- 기본 prefix: `coupang_omp_sellers_YYYYMMDD_HHMMSS`
- 최종: `{prefix}.json`, `{prefix}.csv`
- 취소/중도 오류에 레코드가 있으면:
  `{prefix}_partial.json`, `{prefix}_partial.csv`
- 사용자가 선택한 `output_dir`만 사용한다. 기존
  `coupang_crawl/output/`은 CLI 기본값 호환용으로만 남긴다.

### 8.2 종료/출력/CLI exit code 진실표

| 종료 상태 | 레코드 | 파일 | CLI exit | UI 판정 |
|---|---:|---|---:|---|
| `token_exhausted` / `no_new_items` | 1건 이상 | 최종 JSON+CSV | 0 | 성공 |
| `page_limit` | 1건 이상 | 최종 JSON+CSV | 0 | 경고 포함 완료(전량 보장 안 함) |
| `token_exhausted` / `no_new_items` / `page_limit` | 0건 | 빈 최종 JSON+header-only CSV | 2 | 실패(확보된 사업자 0명; 종료 원인 함께 표시) |
| `no_items` / `template_not_captured` | 0건 | 빈 최종 JSON+header-only CSV | 2 | 실패 |
| `cancelled` | 1건 이상 | `_partial` JSON+CSV | 130 | 취소·부분 저장 |
| `cancelled` | 0건 | 파일 없음 | 130 | 취소 |
| 그 밖의 run 오류 | 1건 이상 | `_partial` JSON+CSV | 1 | 실패·부분 저장 |
| 그 밖의 run 오류 | 0건 | 파일 없음 | 1 | 실패 |
| output 저장 오류 | 무관 | 성공한 경로만 summary에 표시, 임시파일 정리 | 3 | 저장 실패 |

현재 CLI는 0개 상품이면 파일 없이 반환하고 CSV는 결과가 있을 때만 생성하며,
`--output` 값도 경로 문자 검증 없이 받는다
(`coupang_omp_crawler.py:145-159,233-235,296-308`). 위 진실표, 고정 header,
원자 저장, prefix 검증은 **의도적인 호환 예외/안전성 개선**이다. CLI의 옵션명,
성공 레코드 schema와 기본 수집값은 유지하지만 이 종료/실패 동작까지 과거와
동일하다고 주장하지 않는다.

### 8.3 인코딩과 원자성

- JSON: `utf-8`, `ensure_ascii=False`, indent 2
- CSV: `utf-8-sig`, newline 빈 문자열, §6.2 고정 header
- 0건 파일 생성 여부는 §8.2 진실표를 따른다.
- 임시 파일에 쓴 뒤 `os.replace`로 최종 경로에 승격한다.
- JSON 저장 성공/CSV 저장 실패를 전체 성공으로 표시하지 않는다.

### 8.4 중복 정책

- D1: `vendorItemId` 기준 실행 내 중복 제거
- D2: `vendorId` 기준 실행 내 중복 제거
- 대표 상품은 현재 v3가 선택한 vendor 매핑을 유지한다.
- 사업자번호 기준 D3 병합, 체크섬, 실행 간 영속 dedup은 이번 범위에서 제외한다.
- 동일 사업자번호가 여러 vendor ID에서 관측돼도 원본 행을 보존한다. 다만
  summary의 `duplicate_business_numbers_observed`는 반드시 계산한다.
- 진단 계산은 사업자번호에서 숫자만 남긴 값이 정확히 10자리인 레코드만 대상으로
  하며, **둘 이상의 서로 다른 vendor ID에 등장한 정규화 사업자번호의 고유 개수**를
  센다. 예를 들어 `123-45-67890`과 `1234567890`이 2~3개 vendor에 나타나도
  진단 수는 1이다. 빈 값/10자리가 아닌 값은 진단에서 제외한다.
- 이 진단은 관측 알림일 뿐 체크섬 검증, 대표행 선택, 병합, 삭제를 수행하지 않는다.

### 8.5 개인정보/민감정보

- 실데이터 출력 폴더는 gitignore 대상이어야 한다.
- 테스트 픽스처는 실제 이름·전화·이메일·주소·사업자번호를 익명 값으로 치환한다
  (`CRAWL_RESULTS.md:137-158`; `PAGE_STRUCTURE.md:282-295`).
- 쿠키, 토큰, 캡처된 POST template은 결과/로그/예외 메시지에 포함하지 않는다.

---

## 9. QThread 워커 계약

`CoupangWorker(QThread)`는 Gmarket 워커와 같은 패턴을 따른다.

### 9.1 signals

- `phase_changed = pyqtSignal(str, int, int)`
- `progress_changed = pyqtSignal(str, int, int)`
- `log_message = pyqtSignal(str)`
- `item_collected = pyqtSignal(dict)`
- `stats_changed = pyqtSignal(object)`
- `error_occurred = pyqtSignal(str)`
- `finished_crawl = pyqtSignal(object)` — `CoupangRunSummary`

`QThread.finished`를 가리지 않는다. 기존 Gmarket도 같은 이유로
`finished_crawl`을 사용한다 (`app/workers/crawl_worker.py:9-10,33-41`).

### 9.2 worker 책임

- caller가 만든 `Control`과 검증된 `CoupangRunConfig`를 받는다.
- `run()` 안에서 엔진을 만들고 실행한다.
- 모든 코어 callback을 signal로 전달한다.
- 예외 경계에서 traceback은 개발 로그로 보내되 사용자 메시지는 요약한다.
- summary를 속성에 보관하고 종료 signal을 정확히 한 번 emit한다.
- 성공/실패와 무관하게 QThread 자체는 정상적으로 끝나 MainWindow가 닫힐 수 있게 한다.

---

## 10. UI 작업지시

### 10.1 MainWindow 최소 변경

현재 `_build_ui()`가 만드는 Gmarket 전체 콘텐츠를 새 `gmarket_tab`의 layout에
그대로 넣고, 그 widget과 `CoupangPanel`을 `QTabWidget`에 추가한다
(`app/ui/main_window.py:65-121`). 기존 Gmarket 위젯을 별도 클래스로 이동하는
대규모 리팩터는 하지 않는다.

필수 변경:

- window title: `판매자 정보 수집기 v3.0`
- application name도 플랫폼 중립 이름으로 변경 (`app/main.py:48-64`).
- `self.coupang_worker`, `self.coupang_control`, `self.coupang_panel` 추가
- `_active_worker()`가 Gmarket crawl, Gmarket prescan, Coupang crawl을 모두 검사
- close/cancel 경로가 실제 활성 플랫폼의 Control에 취소 요청
- 각 worker 생성 직후 `_maybe_close_after_worker`를 `finished`에 연결
- `_closing` 또는 `_close_prompt_active`이면 어느 탭에서도 새 실행 금지
- 플랫폼 하나가 실행 중이면 다른 플랫폼의 시작 계열 control 비활성화

### 10.2 Coupang 탭 화면

위에서 아래 순서로 구성한다.

1. **대상 안내**: `/np/omp '전체' 탭`, 카테고리 선택 없음
2. **실행 설정**
   - 출력 폴더 + 찾아보기
   - 최대 피드 페이지(기본 10)
   - vendorItem 배치 크기(기본 10)
   - 웜업 초(기본 20)
   - 요청 지연 최소/최대(기본 1.0/2.5)
   - 출력 prefix(선택)
3. **버튼**: `수집 시작`, `일시정지`, `재개`, `취소`, `결과 열기`
4. **단계/진행 표시**
   - 현재 phase
   - 상품 발견 수
   - 고유 판매자 수
   - 사업자정보 성공/BrandSeller 스킵/오류
   - 파워판매자 수
5. **로그**: 민감정보 없는 요약 로그
6. **결과 표**: §6.2 필드 중 사용자 핵심 열을 표시하고 전체 값은 export에 보존

### 10.3 Coupang UI 상태

| 상태 | 시작 | 일시정지 | 재개 | 취소 | 설정 |
|---|---:|---:|---:|---:|---:|
| `idle` | O | X | X | X | O |
| `running` | X | O | X | O | X |
| `paused` | X | X | O | O | X |
| `cancelling` | X | X | X | X | X |
| `finished` | O | X | X | X | O |
| `failed` | O | X | X | X | O |
| `external_busy` | X | X | X | X | 보기만 허용 |

상태 변경은 한 함수에서만 버튼 활성화/문구를 갱신한다. worker signal handler가
제각각 버튼을 직접 조작하지 않는다.

### 10.4 사용자 메시지

- 템플릿 캡처 실패: “Coupang 피드 요청을 확인하지 못했습니다. 브라우저/네트워크
  상태를 확인한 뒤 다시 실행하세요.”
- Camoufox 미설치: package와 브라우저 설치 단계를 명시한다.
- 취소: “현재 요청 완료 후 취소 중” → 부분 저장 경로 표시
- BrandSeller: 실패 dialog를 반복하지 않고 스킵 수와 로그에 표시
- page limit 종료: “전체 피드 자연 종료”로 오인하지 않게 상한 도달을 표시
- 저장 실패: 수집 성공과 분리한 critical 오류로 표시

---

## 11. 의존성과 패키징

### 11.1 검증 환경 기준

현재 프로젝트 venv에서 확인된 개발 기준은 다음과 같다.

- Python 3.12 venv
- `camoufox[geoip]==0.5.4`
- Camoufox browser `official/stable v152.0.4-beta.28`
- `scrapling==0.4.11`

현재 `requirements.txt`에는 `camoufox` 직접 의존성이 없고
`scrapling[fetchers]>=0.4.11`만 있다 (`requirements.txt:1-11`). 따라서
성공한 venv의 lock/freeze를 근거로 **`camoufox[geoip]==0.5.4`**를 직접
의존성으로 명시한다. `geoip=True`는 `geoip` extra가 없으면 즉시 실패한다
(`camoufox/geolocation.py:120-127`, 설치된 0.5.4 기준). 최신 버전을 임의로
선택하지 않는다.

### 11.2 설치 흐름

개발/배포 문서에 다음 단계를 포함한다.

```bash
python -m pip install -r requirements.txt
python -m camoufox sync
python -m camoufox set official/stable/152.0.4-beta.28
python -m camoufox fetch
python -m camoufox version
```

`camoufox fetch`는 `geoip` extra가 설치된 경우 GeoIP DB도 내려받는다
(`camoufox/__main__.py:300-326`, 설치된 0.5.4 기준). browser와 GeoIP DB는
프로젝트 폴더가 아니라 운영체제의 per-user Camoufox cache에 저장된다
(`camoufox/pkgman.py:54-74,721-790`, 설치된 0.5.4 기준).

앱은 수집 시작 중에 package/browser/GeoIP 자산을 암묵적으로 다운로드하지
않는다. Coupang 시작 전에 package, pinned browser, GeoIP DB 존재/갱신 필요
상태를 검사하고 부족하면 위 설치 명령을 안내한 뒤 시작을 거부한다. 오프라인
상태에서는 Gmarket 탭과 앱 자체는 정상 기동해야 하며, Coupang은 “인터넷 연결
또는 runtime 준비 필요”로 실패해야 한다. `geoip=False`로 조용히 강등하지 않는다.

Linux에서 디스플레이 없는 실환경 테스트만 `xvfb-run`을 사용한다. Windows GUI
실행 경로에 `xvfb`를 요구하지 않는다.

### 11.3 PyInstaller

현재 spec은 PyQt6와 BrowserForge 데이터만 명시하고 실행 파일 이름도
`GmarketSellerCollector`다 (`pyinstaller.spec:21-41,54-73`). 다음을 반영한다.

- 앱 이름을 플랫폼 중립 이름으로 변경
- Camoufox/Playwright hidden import와 필요한 package data 조사·반영
- **배포 계약:** PyInstaller 앱 exe에는 약 1.2GB browser/GeoIP cache를 직접
  넣지 않는다. `scripts/setup_coupang_runtime.py`를 콘솔형
  `CoupangRuntimeSetup.exe`로 함께 빌드해 package에 포함하고, 이 setup 도구가
  pinned browser와 GeoIP DB를 per-user cache에 설치/검증한다. 앱은 설치를
  수행하지 않고 preflight와 안내만 담당한다. 따라서 지원 배포물은
  `SellerCollector.exe + CoupangRuntimeSetup.exe + 안내문`이며 bare 앱 exe
  단독은 Coupang 지원 배포물이 아니다.
- 브라우저 미설치 상태에서 Gmarket 탭은 실행 가능하고 Coupang 탭은 명확한
  안내를 표시하는지 검증
- Windows에서 실제 빌드 후 Gmarket/Coupang 양쪽 스모크 수행

clean Windows 사용자 프로필(기존 Camoufox cache 없음)에서 아래 순서를
릴리스 테스트한다.

1. 앱 exe만 실행: 앱/Gmarket 정상, Coupang 시작 차단 및 정확한 setup 안내
2. `CoupangRuntimeSetup.exe` 실행: pinned browser/GeoIP 설치와 버전 확인
3. 앱 재실행: Coupang preflight 통과 및 최소 수집 성공
4. cache를 일시적으로 사용할 수 없게 한 상태: 앱/Gmarket 정상, Coupang만 실패

브라우저 자산 배포 방식이 확정되지 않으면 개발 완료가 아니라 **릴리스 보류**다.

---

## 12. 구현 순서

### P0. 기준선과 의존성 게이트

1. 변경 전 전체 테스트를 실행해 결과를 기록한다.
2. 현재 CLI를 검증 venv에서 한 번 실행해 v3 출력 schema/CLI snapshot을 남긴다.
3. 실데이터가 아닌 익명 API 응답/fake payload fixture를 만든다.
4. Camoufox package/browser 버전과 설치 명령을 기록한다.
5. Windows 배포 방법의 검증 책임자를 명확히 한다.
6. 제품/운영 책임자가 `/np/omp` 대상, robots.txt 비명시 허용 상태, 적용 약관을
   검토하고 이름·날짜·근거 링크/티켓이 있는 release sign-off를 남긴다.

**종료 조건**: v3 회귀 테스트가 실패하거나 기존 Gmarket 테스트가 깨져 있으면
P1로 넘어가지 않는다. 운영 sign-off가 없으면 네트워크 없는 P1~P3 개발은 할 수
있지만 P4 라이브 E2E와 배포는 금지한다.

### P1. Qt 비의존 Coupang 코어

1. config/record/summary 계약 구현
2. v3 helper와 파이프라인을 주입 가능한 코어로 추출
3. callback, safe point, 중단 가능한 sleep 적용
4. 명시적 오류/종료 사유 적용
5. exporter와 부분 저장 구현
6. CLI를 코어 어댑터로 전환하고 회귀 테스트

**종료 조건**: 네트워크 없는 코어/저장/CLI 테스트가 모두 통과하고, CLI 옵션
이름·기본값과 성공 레코드 출력 필드가 이전과 동일해야 한다. 0건/취소/오류의
파일과 exit code는 §8.2의 의도적 호환 예외를 따라야 한다.

### P2. Worker 브리지

1. `CoupangWorker` 구현
2. signal과 summary 전달 테스트
3. 성공/취소/엔진 예외 시 종료 signal 1회 보장
4. browser factory가 worker thread에서 호출되는지 테스트

**종료 조건**: UI event loop를 막지 않고 worker가 모든 종료 경로에서 끝난다.

### P3. 2탭 UI

1. 기존 Gmarket 위젯 트리를 첫 탭에 감싼다.
2. CoupangPanel을 두 번째 탭으로 추가한다.
3. 설정 검증, 상태 전이, progress/log/result wiring을 연결한다.
4. 전역 단일 worker 정책과 cross-tab 비활성화를 연결한다.
5. close dialog 경쟁 조건 테스트에 Coupang worker를 추가한다.

**종료 조건**: 기존 Gmarket UI 테스트와 신규 Coupang UI 테스트가 모두 통과한다.

### P4. 라이브 스모크/E2E

1. WSL2/Linux GUI 또는 xvfb 환경에서 기본값 full run
2. 웜업 중 취소, pagination 중 취소, vendor 수집 중 취소
3. Camoufox 누락, template 미캡처, output 쓰기 실패 시나리오
4. Gmarket 수집 후 Coupang 수집, Coupang 후 Gmarket 수집
5. 실행 중 다른 탭 시작 차단과 창 닫기 검증

**종료 조건**: §13의 live/UX 기준을 만족하고 민감정보가 로그/저장소에 남지 않는다.

### P5. 배포와 문서

1. requirements/설치 가이드/README 갱신
2. PyInstaller spec 갱신
3. Windows build 및 양 탭 스모크
4. CLI 도움말과 GUI 기본값 일치 확인
5. 최종 테스트 결과와 알려진 제한 기록

---

## 13. 검증 계획과 Acceptance Criteria

### 13.1 자동 검증 명령

```bash
.venv/bin/python -m unittest discover -s tests -v
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  tests.test_main_window tests.test_coupang_panel tests.test_coupang_worker -v
.venv/bin/python -m compileall app coupang_crawl tests
.venv/bin/ruff check app coupang_crawl tests
```

ruff가 venv에 없으면 새 의존성을 추가하지 말고 현재 프로젝트의 기존 lint 명령을
사용하거나 검증 공백을 보고한다.

### 13.2 자동 테스트 기준

- **AC-01** 기존 테스트가 변경 전후 모두 통과한다.
- **AC-02** 앱 시작 시 `QTabWidget`에 Gmarket/Coupang 탭이 정확히 하나씩 있다.
- **AC-03** Gmarket 초기 버튼 상태와 기존 수집/저장 계약이 그대로다.
- **AC-04** Camoufox가 설치되지 않아도 앱과 Gmarket 탭은 기동한다.
- **AC-05** Coupang 설정의 숫자 범위와 output 경로가 실행 전에 검증된다.
- **AC-06** instrumented browser factory, fake API/page, exporter가 각 호출의
  thread ID를 기록하고, 이 ID가 `CoupangWorker.run()`의 thread ID와 같으며
  QApplication/UI thread ID와 다름을 assertion한다.
- **AC-07** `getPromotion`의 문자열 `"0"`, 커서 갱신, 종료 조건이 테스트된다.
- **AC-08** 3-API 응답이 §6.2의 14개 필드에 정확히 매핑된다.
- **AC-09** vendorItemId와 vendorId 중복이 실행 내에서 제거된다.
- **AC-10** BrandSeller null, request 오류, 정상 레코드가 각각 다른 결과로 집계된다.
- **AC-11** JSON/CSV 인코딩, 고정 header, output_dir, 원자 저장이 테스트된다.
- **AC-12** 성공·취소·오류 모두 browser/context를 닫는다.
- **AC-13** 취소 시 새 요청을 시작하지 않고 확보 레코드를 `_partial` 파일로 보존한다.
- **AC-14** worker는 모든 종료 경로에서 `finished_crawl`을 정확히 한 번 emit한다.
- **AC-15** 한 플랫폼 실행 중 다른 플랫폼 시작이 차단된다.
- **AC-16** close dialog가 열린 동안 새 worker가 시작되지 않는다.
- **AC-17** close 승인 후 실제 worker 종료 전에는 창이 닫히지 않는다.
- **AC-18** fake 응답에 `SECRET_COOKIE_SENTINEL`,
  `SECRET_TEMPLATE_SENTINEL`, 익명 fixture marker를 넣고, log/error/export
  캡처에서 secret sentinel이 0회인지 검사한다. 모든 Coupang fixture는
  `tests/fixtures/coupang/manifest.json`에 `synthetic: true`로 등록하고 CI에서
  미등록 fixture와 금지 secret key/raw request dump 패턴을 탐지한다.
- **AC-19** UI와 CLI가 같은 코어를 호출하고 기본값/출력 schema가 일치한다.
- **AC-20** 구형 상세페이지/카테고리/FTC/proxy 코드가 신규 app 경로에서 import되지 않는다.
- **AC-21** `store_name_present`, `store_name_missing`, `store_name_coverage`가
  0건/일부 공란/전부 채움 fixture에서 정확히 계산된다.
- **AC-22** §8.2의 각 종료 상태에 대해 파일명, 빈 파일 여부, exit code와 UI
  판정이 table-driven test로 검증된다.
- **AC-23** runtime preflight가 package 누락, browser cache 누락, GeoIP 누락/
  갱신 필요, 정상 준비 상태를 구분하며 어느 실패에서도 Gmarket import/기동을
  막지 않는다.
- **AC-24** 동일한 10자리 사업자번호의 하이픈 유무, 2개/3개 vendor 중복,
  서로 다른 중복 번호 2종, 빈 값/잘못된 길이 fixture에서
  `duplicate_business_numbers_observed`가 “중복된 정규화 번호의 고유 개수”로
  계산되고 출력 행 수/내용은 변하지 않는다.

### 13.3 라이브 검증 기준

라이브 수치는 시점에 따라 달라지므로 178/180 또는 156을 고정 assertion으로
쓰지 않는다. 대신 아래를 증명한다.

- **AC-L01** 기본 설정으로 홈 웜업과 `/np/omp` request template 캡처에 성공한다.
- **AC-L02** `getPromotion`이 한 페이지 이상 반환하고 커서 또는 명시적 종료
  조건까지 진행한다.
- **AC-L03** `individualInfo/products`로 한 명 이상의 vendor를 매핑한다.
- **AC-L04** marketplace seller 한 명 이상에서 필수 사업자 필드를 확보한다.
- **AC-L05** resolved marketplace 레코드의 `url`, `company_name`, `ceo_name`,
  `business_number`, `phone`, `email`은 비어 있지 않다.
- **AC-L06** `store_name` 공란은 허용하되 coverage를 summary에 표시한다.
- **AC-L07** page limit 도달과 token 자연 소진이 UI에서 구분된다.
- **AC-L08** proxy 없이 검증을 먼저 수행하고, 차단 시 자동 우회하지 않고
  명확한 실패를 반환한다.
- **AC-L09** 전체 run 종료 후 JSON/CSV가 열리고 행 수와 필드 순서가 일치한다.
- **AC-L10** 취소/창 닫기 후 Camoufox/Qt worker 프로세스가 남지 않는다.

### 13.4 Windows 배포 기준

- **AC-W01** PyInstaller 빌드가 성공한다.
- **AC-W02** 새 exe 이름/윈도우 title이 플랫폼 중립 이름이다.
- **AC-W03** Gmarket 탭의 사전 조사와 최소 수집 스모크가 통과한다.
- **AC-W04** Camoufox 브라우저 설치 상태에서 Coupang 최소 수집 스모크가 통과한다.
- **AC-W05** 브라우저 미설치 상태의 안내가 실행 가능한 설치 절차를 제시한다.
- **AC-W06** 빈 Windows 사용자 cache에서 §11.3의 실패→setup→성공 순서를
  재현하고 package/browser/GeoIP의 실제 버전과 경로를 테스트 기록에 남긴다.

### 13.5 운영 승인 기준

- **AC-P01** P4 라이브 E2E 전에 제품/운영 책임자의 `/np/omp` 수집 승인 기록이
  존재한다. 기록에는 책임자, 날짜, 검토한 robots.txt/약관 기준, 허용 범위,
  중단 조건이 있어야 한다.
- **AC-P02** 승인 범위는 현재 3-API·보수적 딜레이·무프록시 수집에 한정한다.
  프록시, 상세페이지, 카테고리 확장은 자동 승계하지 않고 새 검토 대상으로 둔다.

---

## 14. 리스크와 대응

| 리스크 | 수준 | 대응 |
|---|---|---|
| 엔진 추출 중 검증된 v3 동작 변경 | 높음 | 추출 전 fake 기반 회귀 테스트, CLI snapshot, helper 시그니처 단위 비교 |
| UI thread에서 Camoufox 실행 | 높음 | browser factory 호출 thread 테스트, UI latency 스모크 |
| 창 닫기 중 worker/브라우저 잔류 | 높음 | 기존 close race 테스트를 Coupang까지 확장, finished 기반 종료만 허용 |
| 부분 결과 유실 | 높음 | 취소/예외 경로의 finally 저장, 원자 파일 승격 테스트 |
| Camoufox/PyInstaller 자산 누락 | 높음 | Windows build를 완료 조건으로 승격, 미설치 안내 fallback |
| GeoIP extra/DB 누락 또는 30일 갱신 실패 | 높음 | `camoufox[geoip]` pin, runtime preflight, 앱 내 암묵적 다운로드 금지, clean-cache 테스트 |
| Akamai/응답 계약 변경 | 중간 | API별 종료/오류 분류, full response/쿠키 미로깅, 라이브 스모크 분리 |
| `/np/omp` 운영 승인 미확정 | 높음 | AC-P01 sign-off 전 라이브 E2E/릴리스 금지 |
| 구형 문서 구현 재유입 | 높음 | §3 충돌표와 AC-20, 신규 import 금지 검사 |
| 플랫폼 동시 실행으로 자원/상태 충돌 | 중간 | MainWindow 전역 단일-워커 invariant와 UI 테스트 |
| 민감정보가 fixture/git에 포함 | 높음 | 익명 fixture review, output gitignore, staged diff 검사 |
| 현재 `store_name` 91%를 실패로 오판 | 낮음 | 공란 허용, coverage만 표시, 임의 보정 금지 |

---

## 15. 변경 파일별 지시

| 파일 | 작업 | 검증 |
|---|---|---|
| `app/core/base.py` | Coupang이 사용할 중단 가능 sleep 추가. 기존 API/동작 유지. | control sleep pause/resume/cancel 단위 테스트 |
| `app/core/coupang/crawler.py` | v3 흐름 추출, callback/summary/Control/browser factory 적용 | API helper/종료/취소/cleanup 테스트 |
| `app/core/coupang/exporter.py` | 고정 schema JSON/CSV 원자 저장 | encoding/header/partial/write failure 테스트 |
| `app/models/coupang_records.py` | config/record/summary dataclass 및 검증 | validation/serialization 테스트 |
| `app/workers/coupang_worker.py` | QThread callback bridge | signal 1회/예외/취소 테스트 |
| `app/ui/coupang_panel.py` | 설정, 버튼, progress, log, result table | offscreen 상태 전이 테스트 |
| `app/ui/main_window.py` | QTabWidget, worker 등록, cross-tab/close 조정 | 기존+신규 close race 테스트 |
| `app/main.py` | 앱 이름을 플랫폼 중립화 | startup smoke |
| `coupang_crawl/coupang_omp_crawler.py` | 같은 코어를 쓰는 CLI adapter로 축소 | CLI args/default/schema 회귀 |
| `requirements.txt` | 검증된 Camoufox 직접 의존성 명시 | clean venv install |
| `pyinstaller.spec` | 이름/hidden import/data/browser 정책 반영 | Windows build/smoke |
| `README.md` | 2탭 사용법, 설치, 출력, 제한 갱신 | 명령 실행 가능성 확인 |
| `scripts/setup_coupang_runtime.py` | pinned browser/GeoIP를 per-user cache에 설치/검증하는 console setup | `CoupangRuntimeSetup.exe` 빈 Windows cache 테스트 |
| `tests/*` | §13 기준 자동화 | 전체 discover 통과 |

`app/core/storage.py`, `app/core/crawler.py`, Gmarket model/schema는 이번 작업의
핵심 변경 대상이 아니다. 변경이 필요해지면 먼저 기존 테스트로 동작을 잠그고,
Coupang 공용화를 위한 대규모 `PlatformProfile` 리팩터 대신 플랫폼별 격리를
우선한다.

---

## 16. 11개 문서 추적표

| 문서 | 이번 작업에 반영한 내용 | 적용 지위 |
|---|---|---|
| `docs/coupang/README.md` | rev.6 우선순위, 2탭 통합 목표, canonical 구현체 | **권위 기준** (`README.md:3-28`) |
| `docs/coupang/CRAWL_RESULTS.md` | 대상, 3-API, 필드, 실측, 제약, CLI 기본값 | **최우선 구현 계약** (`CRAWL_RESULTS.md:25-39,56-105,108-135,222-252`) |
| `docs/coupang/COLLECTION_STRATEGY.md` | rev.6 정정문의 전체 탭/3-API/무프록시 | rev.6 정정만 채택 (`COLLECTION_STRATEGY.md:3-16`) |
| `docs/coupang/DATA_FIELDS_MAPPING.md` | rev.6 API 필드와 파워셀러 의미 | rev.6 §6만 채택; 구형 D3는 제외 (`DATA_FIELDS_MAPPING.md:225-248`) |
| `docs/coupang/PAGE_STRUCTURE.md` | 구형 selector 금지, API 기반 접근 | rev.6 정정만 채택 (`PAGE_STRUCTURE.md:1-15`) |
| `research/PLATFORM_ANALYSIS.md` | Akamai 위험과 차단 모니터링 배경 | 역사 참고 (`PLATFORM_ANALYSIS.md:13-59`) |
| `research/BYPASS_TECHNICAL_GUIDE.md` | 실제 상호작용 웜업 | §11 웜업 원칙만 참고하고 proxy/randomizer/backoff 제안은 거부 (`BYPASS_TECHNICAL_GUIDE.md:279-342`) |
| `research/FREE_BYPASS_RESEARCH.md` | 무프록시 가능성 가설이 rev.6 실측으로 확인됨 | 대체 실행기/공유기 자동화는 제외 (`FREE_BYPASS_RESEARCH.md:79-140,143-203,207-287`) |
| `research/IMPLEMENTATION_ARCHITECTURE.md` | 코어/워커/UI 분리, Gmarket 격리 | 구조 원칙만 채택; 4-stage 폐기 (`IMPLEMENTATION_ARCHITECTURE.md:35-77,234-420`) |
| `research/IMPLEMENTATION_PLAN.md` | QTabWidget, 테스트/close/cancel 위험 목록 | UI·테스트 아이디어만 채택; Stage A~D·proxy 폐기 (`IMPLEMENTATION_PLAN.md:1156-1238,1261-1319`) |
| `research/SCRAPLING_TECH_ANALYSIS.md` | Camoufox/세션/행동 시뮬레이션 배경 | 현재 direct Camoufox와 맞는 부분만 참고 (`SCRAPLING_TECH_ANALYSIS.md:159-180,646-744`) |

---

## 17. 구현 금지 체크리스트

코드 리뷰에서 아래 항목이 보이면 구현을 중단하고 제거한다.

- [ ] 신규 app 코드가 `src/coupang_crawl.py`를 import한다.
- [ ] `/vp/products/*`를 사업자정보 수집용으로 요청한다.
- [ ] `li[data-product-id]`, `.prod-sale-vendor-name` 등 구형 selector를 쓴다.
- [ ] `?page=N`, `listSize=120`, category/search를 수집 대상으로 쓴다.
- [ ] 프록시가 없다는 이유로 실행을 거부한다.
- [ ] FTC DB, fuzzy matching, thefuzz, Levenshtein을 추가한다.
- [ ] Gmarket Storage의 `collected_ids.json`을 Coupang이 공유한다.
- [ ] UI 스레드에서 Camoufox 또는 파일 저장을 실행한다.
- [ ] 쿠키/POST template/사업자 실데이터를 로그나 fixture에 남긴다.
- [ ] 사업자번호 기준 행 병합으로 v3 결과를 조용히 바꾼다.
- [ ] 동작 근거 없이 새 dependency나 추상화 계층을 추가한다.

---

## 18. 완료 정의(Definition of Done)

다음 조건을 모두 만족해야 작업 완료다.

1. §13의 AC-01~24, AC-L01~10, AC-W01~06, AC-P01~02가 통과하거나, 실행
   불가능한 항목은 이유·대체 증거·릴리스 차단 여부가 명시돼 있다. AC-P01과
   clean-Windows runtime 검증은 대체 증거만으로 릴리스 완료 처리할 수 없다.
2. Gmarket 기존 테스트와 실제 스모크가 통과한다.
3. Coupang UI와 CLI가 같은 코어를 사용한다.
4. `/np/omp` 3-API 외 구형 수집 경로가 신규 앱 코드에 없다.
5. 성공·취소·오류 후 worker와 Camoufox 프로세스가 남지 않는다.
6. JSON/CSV와 부분 결과가 지정 경로에 올바른 인코딩/필드 순서로 저장된다.
7. Windows 배포 또는 명시적인 릴리스 보류 결정이 기록돼 있다.
8. 실사업자정보·쿠키·요청 template이 git diff에 없다.
9. `README.md`에 설치, 2탭 사용법, 출력, 알려진 제한이 반영돼 있다.
10. 변경 파일, 테스트 결과, 남은 위험을 최종 보고서에 기록한다.

이 조건을 만족하기 전에는 “Coupang 탭 추가 완료”로 보고하지 않는다.
