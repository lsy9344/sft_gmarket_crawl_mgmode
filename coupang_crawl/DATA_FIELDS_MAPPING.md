# Coupang OMP - Data Fields & Mapping

> **rev.6 (2026-07-26) — 구현 완료, 실측 매핑으로 대체.**
> - 사업자정보 확보처는 상세페이지가 아니라 **`getStoreReview` 스토어 API**
>   (상세페이지는 항상 403). 실측 확정 매핑은 **`CRAWL_RESULTS.md` §3.1**.
> - **이메일 수집 가능** — `repEmail` 100% 확보 (아래 rev.4의 "미공개/불가"
>   판단은 폐기). 대표자명(`repPersonName`)·전화(`repPhoneNum`, 마스킹 없음)
>   ·사업자번호(`businessNumber`)·주소(`repAddr1/2`) 모두 100%.
> - **수집 대상은 `/np/omp` '전체' 탭 하나** — 판매자 단위 수집이므로 상품
>   `url` 은 기본 레코드에 없음 (필수 7항목 중 유일한 미수집, §6·`CRAWL_RESULTS.md` §5).
> - 본 문서와 `CRAWL_RESULTS.md` 가 다르면 그것이 우선.
>
> 최종 개정: 2026-07-25 (rev.4)
> **rev.4 — 공정위(FTC) 연동 전면 제거 (필독).**
>
> 필수 수집 항목은 **Coupang 자체 상품 상세페이지 안에 전부 존재한다**
> (`PAGE_STRUCTURE.md` §3.2). 외부 정부 DB(공정위)를 조회·매칭할 이유가
> 없다. Gmarket이 `mg.gmarket.co.kr` 를 직접 읽어 판매자 정보를 얻듯,
> Coupang도 자신의 상품 상세페이지를 직접 읽어 얻는다 — 그게 전부다.
>
> **rev.4에서 제거된 것**: 공정위 CSV/API 연동, SQLite+FTS5 매칭 엔진,
> 상호명 퍼지 매칭, `domain`/`business_status`/`handled_products` 필드
> (공정위에서만 얻을 수 있었던 필드라 소스가 사라지면서 함께 제거),
> `thefuzz`/`python-Levenshtein` 의존성.
>
> rev.2에서 도입한 **동일 사업자번호 제거**(§3)와 **파워셀러**(§2)는
> 그대로 유효하다.

---

## 0. rev.4 요지

| 항목 | rev.2/3 (정정 전) | **rev.4 (최종)** |
|------|-------------------|-------------------|
| 사업자정보 확보 방법 | 공정위 DB 매칭(1차) 또는 상세 스크래핑+공정위 보강 | **Coupang 상세페이지 직접 스크래핑, 이것이 유일한 경로** |
| 상세 수집 대상 | 공정위 미매칭 판매자만 (rev.2) | **모든 고유 판매자** (D2 축약 이후 전체) |
| 공정위 DB | 필수 인프라 | **없음 — 완전 제거** |
| 외부 의존성 | SQLite+FTS5, thefuzz, python-Levenshtein | **없음** |
| 오매칭 리스크 | 동명 사업자 퍼지 매칭 모호성 | **없음 — 상세페이지 파싱 실패/차단만이 리스크** |
| domain/business_status/handled_products | 공정위 전용 필드로 존재 | **제거** (다른 소스 없음) |
| 프록시 의존도 | 일부 Stage는 프록시 불필요 | **프록시 없이는 아무 것도 수집 못함** |

---

## 1. 수집 필드 (Gmarket Parity)

전 필드가 **Coupang 자체 상품 상세페이지**에서 직접 확보된다.

| # | Field (EN) | Field (KR) | Gmarket Source | **Coupang Source (rev.4)** | Verified |
|---|-----------|-----------|----------------|------------------------------|----------|
| 1 | product_id | 상품ID | goodscode | `data-product-id` (리스팅) | ⚠ 미검증 |
| 2 | url | 상품URL | item.gmarket.co.kr | `/vp/products/{id}` 조합 | ✅ |
| 3 | store_name | 스토어명 | `h3.store-title` | `.prod-sale-vendor-name` (리스팅) | ⚠ 미검증 |
| 4 | company_name | 상호명 | mg.* seller label | **상세페이지 최하단 사업자정보 란** | ⚠ 미검증 (§4) |
| 5 | ceo_name | 대표자명 | (Gmarket 불가) | **상세페이지 직접 파싱** — Coupang은 Gmarket과 달리 이 필드를 노출 | ⚠ 미검증 |
| 6 | email | 이메일 | mg.* e-mail label | 미공개 | — |
| 7 | phone | 전화번호 | mg.* representative | **상세페이지 직접 파싱** (마스킹 가능) | ⚠ 미검증 |
| 8 | business_number | 사업자등록번호 | mg.* business reg no | **상세페이지 직접 파싱** | ⚠ 미검증 |
| 9 | address | 주소 | mg.* address label | **상세페이지 직접 파싱** | ⚠ 미검증 |
| 10 | collected_at | 수집시각 | local ISO | local ISO | ✅ |
| 11 | source | 출처카테고리 | category name | OMP 카테고리/정렬 라벨 | ✅ |

**Gmarket과의 대응 관계**

| Gmarket | Coupang (rev.4) |
|---------|------------------|
| `mg.gmarket.co.kr/SellerInfo?goodscode=` GET | 상품 상세페이지 fetch → **최하단 사업자정보 란** |
| 무보호 HTTP 엔드포인트, `requests` 로 충분 | Akamai 보호, `StealthySession` 필요 (또는 정적 HTML 시 requests) |
| `BeautifulSoup` 로 라벨→값 매핑 파싱 | 최하단 DOM 파싱 우선, XHR 캡처 폴백 |
| goodscode 단위 | **판매자 단위**(D2 축약 이후 대표 상품 1건) |

접근 방식(브라우저 필요 여부, 렌더링이 동적인지)은 다르지만 **원리는
동일하다** — 대상 사이트 자신이 공개한 판매자 정보 페이지를 직접 읽는다.

**Verified 열 판정 기준**: `✅` = URL 규칙으로 확인됨. `⚠ 미검증` =
셀렉터/XHR 패턴이 실측되지 않음 — 구현 전 스파이크
(`IMPLEMENTATION_PLAN.md` P0-3)에서 반드시 확정한다. **이 스파이크가
파이프라인 성립 여부를 전적으로 좌우한다** — 대체 경로(공정위 등)가
없으므로, 여기서 확인되지 않으면 계획을 재검토해야 한다(§17-Q1).

---

## 2. Coupang 전용 추가 필드

| # | Field | Source | Note | Verified |
|---|-------|--------|------|----------|
| 12 | vendor_item_id | listing `vendorItemId` | Variant-level ID | ⚠ |
| 13 | item_id | listing `itemId` | Item-level ID | ⚠ |
| 14 | price | `strong.price-value` | 현재가 | ⚠ |
| 15 | original_price | detail page | 할인 전 가격 | ⚠ |
| 16 | rating | `em.rating` | 별점 | ⚠ |
| 17 | review_count | `span.rating-total-count` | 리뷰 수 | ⚠ |
| 18 | rocket_delivery | badge | 로켓배송 플래그 | ⚠ |
| ~~19~~ | ~~mail_order_license~~ | ~~제거됨~~ | ~~통신판매업신고번호 — 수집 대상 아님~~ | — |
| **20** | **power_seller** | 리스팅/상세 `파워셀러` 배지 | 파워셀러 여부 (bool) | ⚠ 셀렉터 미확정 |
| **21** | **seller_grade** | 배지 원문 텍스트 | 등급 원문 보존 | ⚠ 셀렉터 미확정 |

> **rev.4 제거 항목**: `domain`, `business_status`, `handled_products`.
> 공정위 DB에서만 얻을 수 있던 필드였고, Coupang 상세페이지는 이 3개를
> 노출하지 않는다(`PAGE_STRUCTURE.md` §3.2에 나열된 7개 필드에 포함되지
> 않음). 소스가 사라졌으므로 필드 자체를 산출물에서 제거한다. 사용자가
> 나중에 필요하다고 판단하면 별도 논의 후 재도입한다.

### 수집 상태 필드 (감사용, rev.4 단순화)

| # | Field | 값 | 목적 |
|---|-------|-----|------|
| 22 | resolved | bool | 상세페이지에서 사업자정보를 확보했는지 |
| 23 | parse_method | `xhr` / `dom` / `none` | 어떤 방식으로 파싱됐는지 (셀렉터 변경 모니터링용) |

> rev.2/3의 `match_method`/`match_score`/`match_candidates`/`ftc_enriched`
> 는 전부 제거한다. 매칭 알고리즘 자체가 없으므로 "매칭 신뢰도"라는
> 개념도 존재하지 않는다 — 결과는 **확보했거나(그라운드 트루스) 못
> 했거나(공란)** 둘 중 하나다. `parse_method` 만 남기는 이유는 셀렉터/XHR
> 패턴이 언제 깨졌는지 운영상 추적하기 위함이다.

### 사업자 단위 집계 필드 (변경 없음)

| # | Field | 설명 |
|---|-------|------|
| 24 | store_names | 이 사업자가 운영하는 스토어명 전체 (`;` 결합) |
| 25 | store_count | 고유 스토어 수 |
| 26 | product_count | 관측된 상품 수 |
| 27 | sample_product_id | 대표 상품 ID (검증용 역추적 앵커) |
| 28 | sample_url | 대표 상품 URL |

---

## 3. 동일 사업자번호 제거 (변경 없음 — rev.2 유지, 소스만 재확인)

3단계 중복 제거(D1/D2/D3), 정규화, 체크섬 검증, 대표 레코드 선택, 병합
규칙, 미확정 분리, 실행 간 영속 제거 — 모두 `IMPLEMENTATION_PLAN.md`
AD-10 그대로 유효하다. rev.4에서는 D3 입력값의 출처가 **오직 하나**다:

```
business_number ← 상세페이지 직접 파싱 결과 (그라운드 트루스)
```

체크섬 검증은 여전히 필수다 — 직접 스크래핑도 DOM 오인식, 공백/특수문자
혼입 등으로 잘못된 문자열을 뽑을 수 있다. `123-45-67890` 과
`1234567890` 같은 표기 차이도 정규화가 필요하다.

### 대표 레코드 선택 규칙 (단순화)

같은 사업자번호를 가진 행이 여러 개일 때(한 사업자가 스토어를 여러 개
운영):

1. 채워진 핵심 필드 수 내림차순 (`company_name`, `ceo_name`, `phone`, `address`)
2. `collected_at` 오름차순 (먼저 수집한 것)

> rev.2/3의 `match_method` 우선순위(`detail_page` > `exact` > `fuzzy`)는
> 더 이상 의미가 없다 — 모든 레코드의 소스가 `detail_page` 로 동일하므로
> 그 기준으로는 우열을 가릴 수 없다. 위 두 기준만 남는다.

### 병합 규칙 (변경 없음)

| 필드 | 병합 방식 |
|------|----------|
| `store_names` | 중복 제거 후 `;` 결합 |
| `store_count` | 고유 스토어 수 |
| `product_count` | 관측 상품 수 합계 |
| `power_seller` | **OR** (하나라도 true면 true) |
| `seller_grade` | 관측 등급 전체 `;` 결합 |
| 그 외 사업자정보 | 대표 레코드 값 |

### 사업자번호가 없는 행

상세페이지 파싱이 실패했거나(차단/타임아웃/셀렉터 미스) 재시도 후에도
확보되지 않은 판매자는 `coupang_unresolved_{ts}` 로 분리 출력한다(§5).

---

## 4. 파워셀러 여부 (변경 없음 — rev.2 유지)

### 4.1 정의

플랫폼이 판매 실적·서비스 품질을 근거로 부여하는 우수 판매자 등급 표식.
Gmarket의 "파워딜러"에 대응하는 Coupang 측 표식을 수집한다.

### 4.2 확보 위치 후보

| 우선순위 | 후보 위치 | 추가 요청 | 커버리지 |
|---------|----------|----------|---------|
| 1 | 리스팅 아이템 배지 | 0 | 전 판매자 |
| 2 | 상품 상세 판매자 영역 | 0 | **전 판매자** (rev.4: 이제 전원 방문하므로) |
| 3 | 사업자정보 XHR 응답 JSON | 0 | 전 판매자 |
| 4 | 판매자 스토어 페이지 | 판매자당 +1 | 전 판매자 (단, 처리량 약 1/2) |

> **rev.4 코멘트.** 상세페이지(Stage C)를 모든 판매자가 어차피 방문하므로,
> 후보 2·3의 "커버리지 제약"은 사라졌다 — 리스팅 배지(후보 1)를 못 찾아도
> 상세 방문에서 확보하면 전 판매자를 커버한다. 후보 4(스토어 페이지)만
> 추가 요청이 발생한다.

### 4.3 데이터 설계 · 병합 · 미발견 처리

`IMPLEMENTATION_PLAN.md` AD-11 참조 — 원문 보존(`seller_grade`) + 파생
불리언(`power_seller`), 병합은 OR, 미발견 시 빈 값 + 경고 1회로 파이프라인을
막지 않는다.

---

## 5. 상세페이지 직접 스크래핑 (rev.4 유일한 사업자정보 확보 경로)

### 5.1 방법

`PAGE_STRUCTURE.md` §3~§4 상세 참조. 요지:

1. **최하단 DOM 직접 파싱 (1순위)** — 상세페이지 최하단 사업자정보 란을
   직접 파싱. 팝업/클릭 불필요, 페이지 로드 후 바로 접근 가능.
2. **XHR 캡처 (폴백)** — DOM 파싱 실패 시 `capture_xhr` 로 사업자정보
   AJAX 응답을 회수.

### 5.2 실패 시 처리

1. 같은 판매자에 대해 다른 대표 상품으로 **재시도**(최대 N회)
2. 그래도 실패하면 `resolved=False` 로 **미확정 목록**에 남긴다
   (`coupang_unresolved_{ts}`)
3. **대체 경로가 없다.** rev.2/3와 달리 "공정위로 폴백"이 존재하지
   않으므로, 상세 파싱 실패율이 곧 최종 커버리지 손실이다. 이는
   파싱 로직의 견고성(`adaptive=True`, XHR 우선)이 그만큼 더
   중요하다는 뜻이다.

---

## 6. 필드 확보 가능성 요약 (rev.6 실측 — `CRAWL_RESULTS.md` §5 재게재)

| Field | Gmarket (mg.*) | **Coupang (스토어 API, 실측)** | Final |
|-------|---------------|--------------------------------|-------|
| url | ✅ (goodscode) | **△ 미수집** — 판매자 단위 수집 (itemId/vendorItemId로 조합 가능) | △ |
| store_name | ✅ | ✅ 91% (`storeInfoArea.displayName`) | ✅ |
| company_name | ✅ | ✅ 100% (`name`) | ✅ |
| ceo_name | ❌ | ✅ 100% (`repPersonName`) — Gmarket에 없는 필드 | ✅ |
| email | ✅ | **✅ 100% (`repEmail`)** — rev.4 "불가" 판단 폐기 | ✅ |
| phone | ✅ | ✅ 100% (`repPhoneNum`, 마스킹 없음) | ✅ |
| business_number | ✅ | ✅ 100% (`businessNumber`) | ✅ |
| address | ✅ | ✅ 100% (`repAddr1`+`repAddr2`) | ✅ |
| power_seller | (파워딜러 상당) | ✅ 100% (`qualitySellerBadgeDto`) | ✅ |

**결론 (rev.6 실측).**

- **필수 7항목(COLLECTION_SPEC.md) 중 6개 100% 수집 성공.** 사업자명·이메일·
  대표자명·스토어·전화·사업자번호를 전부 확보했다. 대표자명과 이메일은
  Gmarket보다 오히려 우수하다(Gmarket은 대표자명 미수집).
- **유일한 미수집 필수항목은 `url`** — 수집 단위가 상품이 아니라 판매자
  (사업자)이기 때문이며 기술적 실패가 아니다. Phase 3에서 확보한
  `itemId`/`vendorItemId` 로 대표 상품 URL 조합이 필요하면
  `coupang_omp_crawler.py` 에 필드를 추가한다.
- 사업자정보의 실제 출처는 **`getStoreReview` 스토어 API** 하나다 —
  상세페이지 스크래핑(rev.4/5)은 403으로 성립하지 않는다.
