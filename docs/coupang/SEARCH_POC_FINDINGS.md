# Coupang 검색 크롤링 실측 결과 (rev.3 — 뷰티 검증 완료)

> 작성: 2026-08-13 (rev.1) · 08-14 오전 (rev.2) · **08-14 낮 (rev.3)**
> 워크트리: `coupang-search`

## 한 줄 요약

**'뷰티' 키워드로 전수 검증 완료. 검색 페이지 1장의 내장 JSON에서
itemId/vendorItemId/vendorId/로켓여부/가격/리뷰까지 전부 나오고,
정렬 변경으로 서로 다른 상품 셋을 얻어 키워드당 200개+ 수집 가능 확인.
기존 3-API 스토어 파이프라인과 그대로 연결된다.**

---

## rev.3 실측 (2026-08-14 12:00, poc7)

| 테스트 | 결과 |
|--------|------|
| `뷰티` 검색 (직접 URL, SSR) | ✅ **77개 상품**, HTML 2.4MB |
| href 스키마 | ✅ `itemId`·`vendorItemId` 포함 — 기존 파이프라인 호환 |
| `listSize=120` 파라미터 | ❌ 무시됨 (77개 그대로) |
| **정렬 변경 (`sorter=saleCountDesc`)** | ✅ 77개 중 **신규 42개** — 정렬마다 다른 셋 |
| 페이지 내장 JSON 파싱 | ✅ 아래 필드 전부 추출 가능 |

### 페이지 내장 JSON에서 추출 가능한 필드 (실측)

상품 객체마다:
- `legacyProductId`, `itemId`, `vendorItemId`, `vendors[0].id` (=vendorId)
- `title`, `salesPrice`, `reviewRatingAverage`, `reviewRatingCount`
- **`rocketDelivery: true/false`** ← 로켓 제외 처리에 사용
- `sponsored: true/false` (광고 표시)
- `link` (상품 URL)

→ **검색 페이지 1장 로드 = 상품 목록 + 판매자 매핑 키 전부 확보.
  추가 요청 없이 기존 `individualInfo`/`getStoreReview` API로 사업자정보 수집 가능.**

### 정렬 옵션 (실측 확인)

| sorter 값 | 이름 | 사용 |
|-----------|------|------|
| scoreDesc | 쿠팡 랭킹순 | ❌ 사용자 지정 제외 |
| saleCountDesc | 판매량순 | ✅ |
| salePriceAsc | 낮은가격순 | ✅ |
| salePriceDesc | 높은가격순 | ✅ |
| latestAsc | 최신순 | ✅ |

정렬 1종 = 최대 77개, 실측 판매량순에서 신규 42개 → **4종 정렬 병합 시
키워드당 고유 상품 150~250개 기대** (중복률 실측치 기준).

## 신뢰성 노트 (rev.2 → rev.3)

- rev.2의 "~73개 상한"은 표본 4건(에어프라이어)이었음 → rev.3에서 **뷰티로 재현**(77개). 상한 존재는 신뢰 가능
- 단, 상한 수치는 키워드·시기에 따라 ±수개 변동 가능 (엔진은 동적 감지로 대응)
- 페이지네이션 없음: `"disableFixedPagination":true` — **페이지 데이터 자체가 이를 명시** (추측 아님)
- 브라우저 크래시(Target crashed) 2회 관찰 — camoufox/xvfb 환경 간헐 불안정.
  엔진에 크래시 복구(페이지 재생성) 로직 필수

## 최종 엔진 설계 (rev.3 확정)

```
키워드 1개 수집 = 1 세션
├─ Phase 1  홈 웜업 (20초, v3 방식 재사용)
├─ Phase 2  검색 수집 — 정렬 4종 순회 (랭킹순 제외)
│    각 정렬: /np/search?q={kw}&sorter={s} 직접 로드 (SSR 확인됨)
│    → 페이지 내장 JSON 파싱 → 상품 수집
│    → deliveryType/rocketDelivery=ROCKET 상품 제외
│    → 15~20초 랜덤 딜레이 + 가벼운 스크롤
│    → 차단 감지 시 즉시 중단 (사용권한 키워드 포함)
│    → 크래시 시 1회 세션 재생성 후 재개
├─ Phase 3  판매자 중복 제거 (vendorId 기준) — 기존 Stage B 로직
├─ Phase 4  스토어 API 사업자정보 수집 — 기존 Phase 4~5 재사용
│    (individualInfo/products → getStoreReview)
└─ Phase 5  저장 — CoupangRecord 스키마 (기존과 동일 필드)
```

### 예상 처리량 (규율 준수 시)

| 항목 | 값 |
|------|-----|
| 키워드 1개 소요 | 약 4~6분 |
| 키워드당 수집량 | 150~250개 (로켓 제외 전) |
| 세션 간격 | ≥ 30분 |
| 일일 처리 | 키워드 6~10개 / 상품 1,000~2,500개 수준 |

### 미검증 (다음 실측)

- 낮은가격순/최신순의 신규 비중 (판매량순 42개는 확인, 나머지 2종은 크래시로 미완료)
- 한 세션에 키워드 2개 이상 처리 시 차단 영향
- 로켓 제외 후 실 수집량 (뷰티는 로켓 비중 높은 카테고리)

## 파일

- poc7: `coupang_search_poc7.py` — 뷰티 종합 검증
- 산출물: `output/poc7_*_t1.html` (뷰티 SSR 전체 — 파싱 개발용 기준 데이터)

---

# rev.4 — 구현 완료 기록 (2026-08-14)

## 구현 산출물 (워크트리 `coupang-search`)

| 파일 | 역할 |
|------|------|
| `app/core/coupang/search_parser.py` | 차단 감지 + DOM 추출 JS + href/가격 파싱 (순수 함수, 테스트 가능) |
| `app/core/coupang/search_crawler.py` | `SearchCrawler` 엔진 — CoupangCrawler 서브클래스, 스토어 API 단계 재사용 |
| `app/workers/search_worker.py` | Qt QThread 브리지 |
| `app/ui/search_panel.py` | "Coupang 검색" 탭 UI (키워드/로켓제외/딜레이 설정, 로그, 결과 테이블) |
| `app/ui/main_window.py` | 세 번째 탭 연결, 시작/일시정지/재개/취소/차단 경고 핸들러 |
| `coupang_crawl/coupang_search_crawler.py` | CLI 어댑터 (헤드리스 실행용) |
| `tests/test_coupang_search.py` | 단위 테스트 9건 — fake page 기반 (네트워크 불필요) |

## rev.3 대비 설계 변경 사항

- **파싱 방식**: 내장 JSON 파싱은 위젯(특가 모듈) 상품만 포함하고 메인 목록은
  DOM 카드로만 렌더링됨을 확인 → **DOM 카드 추출 방식으로 변경**
  (href 에 itemId/vendorItemId 전부 포함, 로켓 배지 img src 로 판별)
- 로켓 판별: 카드 내 `img[src*="rds/logo"]` — SSR 에 항상 렌더링되어 신뢰 가능

## 검증 상태

- 단위 테스트 9건 통과 (정렬 순회·로켓 제외·중복 제거·차단 감지·백오프·파서·설정 검증)
- 전체 스위트 227건 통과
- **실기(end-to-end) 검증은 미실시** — 다음 세션 규율(일일 ≤5) 소진으로 익일 진행

## 익일 실기 검증 계획

1. CLI 로 `--keyword 뷰티` 1회 실행 (정렬 4종)
2. 확인 항목: 정렬별 수집 수, 로켓 제외 수, 고유 vendorId 수,
   사업자정보 수집 성공률, 출력 파일 스키마(기존 CoupangRecord 와 일치)
3. 통과 시 탭 배포 준비 완료
