결론: Foodspring 탭은 기본 흐름은 갖췄지만, 취소 시 결과 유실과 네트워크·파싱 실패를 정상 완료로 오인하는 문제가 있어 출시 전 보완이 필요합니다.

## Review

### Correct
- 상품은 `nid` 기준으로 중복 제거됩니다: `app/core/foodspring/engine.py:199-207`.
- 셀러 상세 정보가 없더라도 빈 셀러 레코드를 만들어 개별 누락을 보존합니다: `app/core/foodspring/engine.py:340-358`.
- 병렬 오류 카운터는 잠금으로 보호됩니다: `app/core/foodspring/engine.py:278-290`.
- Excel은 같은 폴더의 임시 파일에 쓴 뒤 `os.replace()`로 교체하며 실패 시 임시 파일을 정리합니다: `app/core/foodspring/exporter.py:101-189`.
- Foodspring 실행 중 Gmarket/Coupang 시작을 막고, 워커 종료 후 UI 잠금을 해제하는 연결은 구성되어 있습니다: `app/ui/main_window.py:761-777`, `app/ui/main_window.py:868-884`.
- 워커 바깥 예외도 fallback summary를 발행하므로 일반적인 엔진 예외에서는 UI 상태가 복구됩니다: `app/workers/foodspring_worker.py:38-59`.

### Fixed
- 없음. 요청에 따라 코드 수정이나 테스트 실행 없이 독립 정적 리뷰만 수행했습니다.

### Blocker
- 치명 등급의 즉시 실행 불가 문제는 코드에서 확정하지 못했습니다.
- 다만 아래 중요 등급 상위 3건은 결과 유실·허위 완료를 일으키므로 출시 전 해결을 권고합니다.

### Findings

#### F1. 중요 — 취소 시 중간 결과가 저장되지 않고, 판매자 배치 중 취소·일시정지가 오래 지연됨
- **위치:** `app/core/foodspring/engine.py:323-359`, `app/core/foodspring/engine.py:426-428`, `app/ui/main_window.py:907-923`
- **발생 시나리오:** 판매자 수집 중 취소하거나 창을 닫습니다. 한 배치의 모든 future가 이미 제출되며 작업 내부에는 `Control` 확인이 없습니다. 각 작업은 대표 상품과 대체 상품 요청을 계속하고, executor 종료 시까지 대기합니다.
- **문제:** 일시정지 후에도 현재 배치 레코드가 계속 수집될 수 있고, 취소는 수분간 지연될 수 있습니다. 이후 `CancelledError` 처리에서는 `cancelled`만 설정하고 exporter를 호출하지 않으므로 수집된 상품·셀러 결과가 전부 유실됩니다. 닫기 대화상자의 “중간 저장한 뒤 종료” 안내와 실제 동작도 다릅니다.
- **수정 제안:** 각 판매자 요청 및 대체 요청 전에 checkpoint를 확인하고, 취소 시 아직 시작하지 않은 future를 취소하십시오. `products`와 `infos`를 run 범위에 유지해 `CancelledError`에서도 부분 Excel을 저장하고 통계·경로를 summary에 기록해야 합니다.

#### F2. 중요 — 세션·네트워크·파싱 장애가 불완전한 Excel의 “정상 완료”로 처리됨
- **위치:** `app/core/foodspring/engine.py:188-230`, `app/core/foodspring/engine.py:241-294`, `app/core/foodspring/engine.py:394-425`
- **발생 시나리오:**
  1. 여러 목록 페이지를 모은 뒤 FS_TOKEN 만료나 네트워크 장애가 발생합니다.
  2. 상세 페이지 구조 변경으로 `__NEXT_DATA__`가 없거나 JSON 파싱이 실패합니다.
  3. 모든 판매자 상세 요청이 HTTP 오류로 실패합니다.
- **문제:** 목록은 같은 세션으로 5회 재시도 후 단순 `break`하여 이미 받은 일부 상품을 계속 처리합니다. 상세 파싱 실패는 예외가 아니라 `None`이므로 요청 오류에도 포함되지 않습니다. 모든 상세 요청이 실패해도 빈 셀러 행을 저장하고, xlsx가 존재한다는 이유로 SUCCESS가 됩니다. 연속 판매자 실패 조기 종료도 없습니다.
- **사용자 영향:** 10,000개 전체를 받지 못했거나 사업자정보가 전부 비어 있어도 “완료”로 표시될 수 있습니다.
- **수정 제안:** 목록 종료 사유를 `natural_end/page_limit/network_error/session_expired`로 구분하고 장애 종료 시 SUCCESS를 금지하십시오. 인증 오류에는 세션을 1회 재확보하고, 판매자 결과도 `found/missing/parse_error/request_error`로 구분해 연속 실패 또는 높은 실패율에서 조기 중단해야 합니다.

#### F3. 중요 — 실제 Excel 저장 실패가 SAVE_ERROR로 매핑되지 않음
- **위치:** `app/core/foodspring/engine.py:406-434`, `app/core/foodspring/outcome.py:20-31`, `app/ui/main_window.py:841-845`
- **발생 시나리오:** 디스크 부족, 열린 Excel 파일, 권한 문제 또는 `os.replace()` 실패가 발생합니다.
- **문제:** exporter 예외는 바깥의 일반 예외 처리에서 `summary.error`로만 저장됩니다. `summary.save_error`는 어디에서도 설정되지 않아 UI의 SAVE_ERROR 분기와 전용 대화상자는 도달할 수 없습니다.
- **수정 제안:** exporter 호출을 별도 `try/except`로 감싸 `save_error`, `termination_reason="save_error"`를 설정하십시오. 엔진이 실제 저장 실패를 만드는 테스트도 추가해야 합니다.

#### F4. 중요 — 하나의 `requests.Session`을 여러 worker 스레드가 공유함
- **위치:** `app/core/foodspring/engine.py:85-88`, `app/core/foodspring/engine.py:274-290`, `app/core/foodspring/engine.py:326-332`
- **발생 시나리오:** 여러 상세 응답이 쿠키를 갱신하거나 Session 내부 상태를 동시에 건드리는 경우입니다.
- **문제:** `requests.Session`은 스레드 간 공유 안전성을 보장하지 않습니다. 오류 카운터는 잠금 처리됐지만 Session은 동일 객체로 병렬 호출됩니다.
- **수정 제안:** thread-local Session을 만들고 초기 headers/cookies를 복사하거나, 각 작업에 독립 Session을 사용하십시오.

#### F5. 중요 — Foodspring preflight가 필수 Chromium 런타임 누락을 발견하지 못함
- **위치:** `app/core/foodspring/preflight.py:30-48`, `app/core/foodspring/engine.py:124-130`
- **발생 시나리오:** 패키지는 번들되어 있지만 Patchright Chromium이 설치되지 않은 PC에서 시작합니다.
- **문제:** preflight는 `find_spec()`와 일반 HTTP 연결만 확인합니다. 실제 세션 확보에는 `StealthyFetcher`의 Chromium이 필요합니다. 같은 브라우저를 검사하는 기존 구현은 `app/core/gmarket_preflight.py:72-98`에 있지만 재사용하지 않습니다.
- **사용자 영향:** 사전 확인은 통과하지만 실제 수집 시작 직후 브라우저 실행 오류가 납니다.
- **수정 제안:** Gmarket Chromium 검사를 공용 런타임 검사로 추출해 Foodspring에서도 호출하고, `scrapling.fetchers`를 실제 import할 수 있는지도 확인하십시오.

#### F6. 경미 — 대체 상품 목록에 대표 상품이 다시 포함됨
- **위치:** `app/core/foodspring/engine.py:273-286`, `app/core/foodspring/engine.py:307-315`
- **발생 시나리오:** 대표 상품에서 파싱에 실패해 대체 상품을 시도합니다.
- **문제:** `alts`를 만들 때 대표 상품을 제외하지 않아 같은 URL을 두 번 요청합니다. 따라서 “대표 1개 + 대체 3개”가 아니라 최대 3개의 고유 상품만 확인합니다.
- **수정 제안:** `nid != entry["pid"]`인 상품만 `alts`에 추가하십시오.

#### F7. 경미 — 상품 상한과 상품 수 집계가 ID 타입에 민감함
- **위치:** `app/core/foodspring/engine.py:199-213`, `app/core/foodspring/engine.py:361-369`
- **발생 시나리오:** `limit_products=1`로 실행하거나 vendor nid가 문자열로 반환됩니다.
- **문제:** 한 페이지를 모두 추가한 뒤 상한을 검사하여 최대 80개까지 초과할 수 있습니다. `_product_count`는 원본 sid로 집계한 뒤 `int(sid)`로 조회하여 문자열 ID이면 0이 되고, 비숫자 ID면 실행 오류가 납니다.
- **수정 제안:** 추가 단계에서 상한에 맞춰 자르고, 모든 상품·셀러 ID를 처음부터 문자열로 정규화해 중복 제거·집계·조회에 동일 타입을 사용하십시오.

#### F8. 경미 — QThread 시작 자체가 실패하면 탭 잠금이 복구되지 않음
- **위치:** `app/ui/main_window.py:773-777`
- **발생 시나리오:** OS 스레드 자원 부족 등으로 `worker.start()`가 예외를 냅니다.
- **문제:** start 호출 전에 UI를 running으로 바꾸고 다른 탭을 잠그며, 예외를 복구하는 `try/except`가 없습니다.
- **수정 제안:** `worker.start()`를 감싸 실패 시 Foodspring을 failed 상태로 바꾸고 Gmarket/Coupang 잠금을 해제하십시오.

### 테스트 공백
`tests/test_foodspring.py:47-66`은 수동 생성 summary의 outcome만 검사하고, `tests/test_foodspring.py:206-297`은 정상 전체 실행만 검사합니다. 다음 테스트가 빠져 있습니다.

- 판매자 배치 중 pause/cancel 및 부분 저장
- exporter가 예외를 낼 때 `save_error` 매핑
- 목록 연속 5회 실패 후 불완전 결과 판정
- 전체 상세 HTTP 실패와 `__NEXT_DATA__` 파싱 실패
- vendor 누락 상품과 문자열 ID의 `_product_count`
- Foodspring 실행 중 다른 탭 잠금 및 종료 후 복구
- Chromium 런타임 누락 preflight

## 우선 해결 상위 3개
1. **F1:** 취소·닫기 시 부분 저장과 배치 내부 취소 반응성
2. **F2:** 네트워크·세션·파싱 장애를 정상 완료로 처리하는 문제
3. **F3:** Excel 저장 실패의 `SAVE_ERROR` 매핑

## Residual risks
- 네트워크와 실제 브라우저는 실행하지 않아 실사이트 응답 구조 및 Scrapling 런타임 실행 여부는 검증하지 않았습니다.
- PyQt signal 순서와 OS별 파일 잠금 동작은 정적 코드만 검토했습니다.
- 저장 파일을 사용자가 Excel에서 연 상태의 Windows 동작은 별도 수동 검증이 필요합니다.