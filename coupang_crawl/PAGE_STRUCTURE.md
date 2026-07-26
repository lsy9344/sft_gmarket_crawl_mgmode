# Coupang OMP - Page Structure & URL Patterns

> **rev.6 (2026-07-26) — 구현 완료. 본 문서 대부분이 실측으로 폐기됐다.**
> - **수집 대상: `https://www.coupang.com/np/omp` '전체' 탭 하나.** 접속 즉시
>   상품이 보이는 라이브딜 피드이며 **무한 스크롤**로 로드된다. 페이지네이션은
>   §1의 `?listSize=120&page={N}` 가 아니라 **`getPromotion` API의
>   `continuationToken` 커서** 방식이다.
> - **상품 상세페이지(`/vp/products/*`)는 항상 403** — 앱 레벨 차단으로 우회
>   불가. 따라서 §3·§4의 "상세페이지 최하단 사업자정보 란 스크래핑"은 전부
>   사용 불가. 사업자정보 실제 출처는 **`getStoreReview` 스토어 API**.
> - 리스팅은 JS 렌더링 div라 §2의 정적 셀렉터(`span.prod-sale-vendor-name` 등)
>   도 사용하지 않는다 — 상품→판매자 매핑은 `individualInfo/products` API.
> - 실측 URL/API 구조는 **`CRAWL_RESULTS.md`** §1·§3 참조. 본 문서와 다르면
>   그것이 우선.
>
> 최종 개정: 2026-07-25 (rev.4)
> rev.2: ①모든 셀렉터에 **검증 상태** 표기, ②판매자정보 렌더링 방식의
> 문서 내 모순 해소(§4), ③**파워셀러 배지 발견 체크리스트** 추가(§5),
> ④셀렉터 확정 스파이크 절차 명시(§7).
> **rev.4: 공정위(FTC) 연동 전면 제거.** §3.2 가 나열하는 7개 필드가
> 사업자정보의 **유일한 확보 경로**임을 명확히 한다 — 외부 DB로 대체하지
> 않는다(`DATA_FIELDS_MAPPING.md` rev.4). 따라서 이 페이지의 셀렉터/XHR
> 확정 스파이크(§7)가 전체 프로젝트 성립의 절대 전제조건이다.

---

## 0. 검증 상태 범례 ★ 먼저 읽을 것

| 표기 | 의미 |
|------|------|
| ✅ 확인 | URL 규칙 또는 공식 문서로 확인됨 |
| ⚠ 미검증 | 외부 스크래핑 리포트 인용 — **본 프로젝트가 실제 응답으로 확인한 적 없음** |
| ❓ 미확정 | 존재는 알지만 위치/표기를 모름 |

> **⚠/❓ 항목을 확정처럼 코딩하지 않는다.** 파서 구현 전에 반드시 §7 스파이크로
> 실측 픽스처를 확보하고, 그 픽스처를 근거로 셀렉터를 확정한 뒤 코드를 쓴다.
> 이전 판은 검증 상태를 구분하지 않아 추정 셀렉터가 확정 사실처럼 읽혔다.

---

## 1. URL Architecture

### OMP Category Listing — ✅ 확인
```
https://www.coupang.com/np/omp?listSize=120&page={N}&sorter=bestAsc
```

### Category Pages — ✅ 확인 (robots.txt 허용 경로)
```
https://www.coupang.com/np/categories/{categoryId}?listSize=120&page={N}
```

### Search — ✅ 확인 (robots.txt 허용 경로)
```
https://www.coupang.com/np/search?component=&q={keyword}&page={N}&listSize=72
```

### Product Detail — ✅ 확인
```
https://www.coupang.com/vp/products/{productId}?itemId={itemId}&vendorItemId={vendorItemId}
```

### Pagination Parameters — ✅ 확인

| Param | Values | Note |
|-------|--------|------|
| `page` | 1~N | 1-indexed |
| `listSize` | 36, 72, 120 | Items per page |
| `sorter` | `bestAsc`, `priceAsc`, `priceDesc`, `rateDesc` | Sort order |

---

## 2. Listing Page HTML Structure — ⚠ 미검증

```html
<ul id="productList">
  <li data-product-id="7058150034">
    <a href="/vp/products/7058150034?itemId=...&vendorItemId=...">
      <div class="name">상품명</div>
      <strong class="price-value">29,900</strong>
      <em class="rating">4.5</em>
      <span class="rating-total-count">(1234)</span>
      <span class="unit-price">(100g당 1,200원)</span>
      <span class="prod-sale-vendor-name">판매자명</span>
      <span class="prod-other-seller-count">+3</span>
      <!-- 파워셀러 배지: 위치 미확정 — §5 참조 -->
    </a>
  </li>
</ul>

<div class="product-list-paging" data-total="42"><!-- data-total = 전체 페이지 수 --></div>
```

### Listing CSS Selectors

| Data | Selector | Attribute | Verified |
|------|----------|-----------|----------|
| Product ID | `li[data-product-id]` | `data-product-id` | ⚠ |
| Title | `div.name` | text | ⚠ |
| Price | `strong.price-value` | text | ⚠ |
| Rating | `em.rating` | text | ⚠ |
| Review Count | `span.rating-total-count` | text | ⚠ |
| Unit Price | `span.unit-price` | text | ⚠ |
| **Seller Name** | `span.prod-sale-vendor-name` | text | ⚠ **핵심 필드** |
| Other Sellers | `span.prod-other-seller-count` | text | ⚠ |
| Total Pages | `div.product-list-paging` | `data-total` | ⚠ |
| **파워셀러 배지** | **미확정** | — | ❓ **§5** |
| itemId / vendorItemId | `a[href]` 쿼리스트링 파싱 | — | ✅ (URL 규칙) |

> **Seller Name 이 파이프라인의 조인 키다.** 이 셀렉터가 틀리면 판매자
> 단위 축약(D2) 자체가 성립하지 않는다. 스파이크 1순위 확인 대상.

---

## 3. Product Detail Page Structure

### 3.1 상품 기본 정보 — ⚠ 미검증

```python
DETAIL_SELECTORS = {          # 외부 리포트 인용 — 실측 확인 필요
    "title": "h2.prod-buy-header__title",
    "price": "span.total-price",
    "brand": ".prod-brand-name",
    "rating": "em.rating",
    "review_count": "span.count",
    "seller_name": ".prod-sale-vendor-name",
}
```

### 3.2 판매자 사업자정보 필드 (표시 항목) ★ 필수 수집 항목의 유일한 출처

| Field | Korean Label | 위치 |
|-------|-------------|------|
| Seller Name | 판매자 | "배송/교환/반품안내" 섹션 |
| Company Name | 상호(법인명) | **상세페이지 최하단 사업자정보 란** |
| CEO Name | 대표자 | **상세페이지 최하단 사업자정보 란** |
| Business Number | 사업자등록번호 | **상세페이지 최하단 사업자정보 란** |
| Address | 사업장 소재지 | **상세페이지 최하단 사업자정보 란** |
| Phone | 전화번호 | **상세페이지 최하단 사업자정보 란** (마스킹 가능) |

> **rev.5 정정.** 사업자정보는 "팝업"이 아니라 **상세페이지 최하단의
> 사업자정보 란**에 직접 표시된다. 통신판매업신고번호는 수집 대상이
> 아니며, **사업자등록번호**가 핵심 수집 항목이다.
>
> 필수 수집 항목(상호명·대표자명·사업자등록번호·주소·전화)이 상세페이지
> 최하단에 전부 존재한다. 외부 DB(공정위)를 조회할 이유가 없다 —
> Gmarket이 `mg.gmarket.co.kr` 를 직접 읽어 이 정보를 얻듯, Coupang도
> 자신의 상세페이지 최하단 사업자정보 란을 직접 읽어 얻는다.
> **이 란의 렌더링 방식(§4)과 셀렉터/XHR 패턴(§7)을 정확히 파악하는
> 것이 전체 프로젝트의 성패를 가른다.**

---

## 4. 판매자정보 렌더링 방식 — 문서 모순 해소 ★

### 4.1 이전 판의 모순

이전 판과 `SCRAPLING_TECH_ANALYSIS.md` §5.2 가 서로 다른 이야기를 했다.

| 문서 | 주장 |
|------|------|
| 본 문서 (이전 판) | "동적 렌더링(AJAX/JS) — **정적 HTML에 없음**" (팝업으로 오인) |
| `SCRAPLING_TECH_ANALYSIS.md` §5.2 | `.seller-company-name` 등 **정적 CSS 셀렉터로 파싱** |

**rev.5 정정.** 사업자정보는 "팝업"이 아니라 **상세페이지 최하단의
사업자정보 란**에 직접 표시된다. 따라서 정적 CSS 셀렉터로 파싱한다는
`SCRAPLING_TECH_ANALYSIS.md` 쪽이 더 사실에 가까울 수 있다.

### 4.2 해소 — 상세페이지 최하단 DOM 파싱을 1순위로 설계한다

사업자정보가 상세페이지 최하단에 직접 렌더링된다면, **페이지 로드
완료 후 해당 섹션의 DOM을 직접 파싱**하는 것이 1순위다. XHR 캡처는
보조 수단으로 유지한다.

`scrapling 0.4.11` 의 `capture_xhr` 도 여전히 유효한 폴백이다.

```python
# 1순위: 페이지 로드 후 최하단 사업자정보 섹션 DOM 파싱
with StealthySession(headless=True) as s:
    page = s.fetch(product_url, network_idle=True)
    # 최하단 사업자정보 란에서 직접 추출
    business_info = page.css_first("[class*='seller-business'], [class*='vendor-info']")

# 2순위 (폴백): XHR 캡처
with StealthySession(headless=True, capture_xhr=r"...사업자정보 엔드포인트...") as s:
    page = s.fetch(product_url, network_idle=True)
    for xhr in page.captured_xhr:
        ...  # JSON 페이로드에서 사업자정보 추출
```

**채택 전략 (우선순위 순)**

| 순위 | 방법 | 장점 | 리스크 |
|------|------|------|--------|
| 1 | **최하단 DOM 직접 파싱** | 페이지 로드 후 즉시 접근 가능, 클릭 불필요 | 셀렉터 변경에 취약 |
| 2 | XHR 캡처 (`capture_xhr`) | 구조화 JSON, 셀렉터 변경 무관 | 엔드포인트 패턴 미확정 |
| 3 | `page_action` 스크롤 후 DOM | 최하단까지 스크롤 필요 시 | 스크롤 실패 가능 |

**핵심 변경**: 팝업 클릭이 불필요하다. 사업자정보는 페이지 최하단에
이미 렌더링되어 있으므로, **스크롤하여 최하단에 도달하면 바로 파싱
가능**하다.

### 4.3 확정된 사실

- 사업자정보는 상세페이지 **최하단 사업자정보 란**에 직접 표시됨 (팝업 아님)
- 통신판매업신고번호는 수집 대상 아님 — **사업자등록번호**가 핵심
- JS 렌더링 여부 미확인 — 정적 HTML에 포함될 가능성 있음 (스파이크 필요)
- 정적 HTML에 포함 시 `requests` + BeautifulSoup만으로 추출 가능 (완전 무료)
- JS 렌더링 시 `StealthySession` 필요 (한국 가정용 IP로 무료 운용 가능)

---

## 5. 파워셀러 배지 발견 체크리스트 (rev.2 신규) — ❓ 미확정

### 5.1 현황

배지 문구는 **"파워셀러"** 로 확인됐으나, **DOM 위치·클래스명·표기 변형은
아직 실측되지 않았다.** 어디서 잡히느냐가 비용을 결정한다.

| 우선순위 | 확보 위치 | 추가 요청 | 커버리지 | 처리량 영향 |
|---------|----------|----------|---------|------------|
| **1** | **리스팅 아이템 배지** | **0** | **전 판매자** | **없음 — 사실상 무료** |
| 2 | 상품 상세 판매자 영역 | 0 | 상세 수집 대상만 | 없음 |
| 3 | 사업자정보 XHR JSON | 0 | 상세 수집 대상만 | 없음 |
| 4 | 판매자 스토어 페이지 | 판매자당 +1 | 전 판매자 | **약 1/2** |

**후보 1이 성립하면 파워셀러 필터로 고비용 구간 진입 전에 대상을 좁힐 수도
있다** — 사업적 가치가 가장 큰 시나리오다.

### 5.2 확인 절차

```
1. 리스팅 HTML 저장 → "파워셀러" 문자열 grep
   └ 히트 시: 해당 노드의 조상 체인을 따라 안정적인 클래스/속성 식별
   └ 미스 시: 배지가 CSS 배경/아이콘일 가능성 → class 명에 power/grade/badge
             포함 요소 전수 덤프

2. 상품 상세 HTML 동일 절차

3. captured_xhr JSON 페이로드에서 키 탐색
   grade / level / badge / power / sellerGrade / vendorGrade

4. 발견 시 §2 / §3.1 표에 셀렉터와 원문 표기를 확정 기록하고
   Verified 를 ✅ 로 갱신
```

### 5.3 표기 변형 대비

배지 문구는 정책에 따라 바뀐다. 파서는 **원문을 그대로 `seller_grade` 에
저장**하고 `power_seller` 는 파생 불리언으로 계산한다. 불리언만 저장하면
문구 변경 시 과거 데이터를 재해석할 수 없다.

### 5.4 미발견 시

`seller_grade=""`, `power_seller=false` + 로그 경고 1회.
**수집 파이프라인을 중단시키지 않는다.**

---

## 6. robots.txt

```
Allow: /np/categories/
Allow: /np/search
```

`/np/omp` 는 **명시적 허용 목록에 없다.**

**설계 반영**
- 기본값은 **허용 경로(`/np/categories/`, `/np/search`) 우선 사용**
- OMP 직접 스캔은 사용자가 설정에서 **명시적으로 켤 때만** 활성화
- 카테고리 경로로도 OMP 판매자 상품에 도달할 수 있으므로, 기본 경로만으로도
  파이프라인은 성립한다

---

## 7. 셀렉터 확정 스파이크 절차 (구현 선행 조건)

`IMPLEMENTATION_PLAN.md` P0-3. **이 절차를 마치기 전에는 파서를 작성하지 않는다.**

### 7.1 산출물

`coupang_crawl/fixtures/` 에 저장:

| 파일 | 용도 |
|------|------|
| `omp_listing_page1.html` | 리스팅 파서 회귀 테스트 |
| `product_detail.html` | 상세 DOM 폴백 파서 테스트 |
| `seller_info_xhr.json` | XHR 파서 테스트 (1순위 경로) |
| `blocked_403.html` | 차단 감지 테스트 |
| `soft_block_200.html` | soft block(200 but empty) 감지 테스트 |

> **픽스처에서 개인정보를 제거한다.** 실제 사업자 정보는 커밋하지 않고
> 값을 치환한 익명 버전만 저장한다.

### 7.2 확인 항목 체크리스트

- [ ] 리스팅: `product_id` 추출 셀렉터 확정
- [ ] 리스팅: **`store_name` 추출 셀렉터 확정** (조인 키 — 최우선)
- [ ] 리스팅: `itemId` / `vendorItemId` 쿼리스트링 파싱 확인
- [ ] 리스팅: 페이지네이션 총 페이지 수 확인
- [ ] 리스팅: **파워셀러 배지 존재 여부** (§5)
- [ ] 상세: 사업자정보 **XHR 엔드포인트 패턴** 확정
- [ ] 상세: XHR JSON 필드명 → 레코드 필드 매핑표 작성
- [ ] 상세: XHR 실패 시 DOM 폴백 셀렉터 확정
- [ ] 상세: 파워셀러/등급 필드 존재 여부
- [ ] 차단: 403/418/429 응답 본문 형태, soft block 임계 바이트 실측
- [ ] **(rev.5)** HTTP/2: Camoufox SETTINGS 프레임 순서 = 실제 Firefox 순서
- [ ] **(rev.5)** 헤더: Accept/Accept-Language/Sec-Fetch-* 순서 = Firefox 기본
- [ ] **(rev.5)** 핑거프린트: WebGL Renderer = 데스크톱 GPU (Apple M1 아님)
- [ ] **(rev.5)** 핑거프린트: Timezone = Asia/Seoul, Screen ≥ 1920×1080
- [ ] **(rev.5)** 웜업: 상호작용 주입 후 `_abck` valid 전환 확인
- [ ] **(rev.5)** 프록시: canary 요청이 flagged IP에서 403 반환 확인

### 7.3 스파이크 실패 시

프록시로도 접근이 안 되거나 상세페이지에서 사업자정보를 찾지 못하면
**대체 확보 경로가 없다** (rev.4: 공정위 폴백 제거). 이 경우 유료
스크래핑 API 도입 또는 프로젝트 범위 재검토가 유일한 대안이다
(`IMPLEMENTATION_PLAN.md` §17-Q1).

### 7.4 우회 메커니즘 검증 (rev.5 신규 — P0-4)

`BYPASS_TECHNICAL_GUIDE.md` §15 전체 절차 참조.

```
[HTTP/2 + 헤더 검증]
1. mitmproxy 시작 → Camoufox로 coupang.com 홈 fetch
2. 캡처된 HTTP/2 SETTINGS 프레임, HEADERS pseudo-header 순서 기록
3. 실제 Firefox(수동)로 동일 페이지 접근 → 동일 항목 기록
4. diff — 불일치 항목이 있으면 real_chrome=True 또는 설정 변경 검토

[핑거프린트 일관성]
1. StealthySession으로 browserleaks.com/canvas + browserleaks.com/webgl 방문
2. Canvas hash, WebGL Renderer/Vendor, Screen, Timezone, Platform 로깅
3. "한국 데스크톱" 프로파일과 일치 확인 (3회 반복 — 회전 + 일관성)

[웜업 상호작용 효과]
1. 상호작용 없이 15초 대기 → 후속 OMP 요청 → status 기록
2. warmup_interact() 주입 → 동일 요청 → status 기록
3. _abck 쿠키 페이로드 길이 비교 (valid = 긴 base64)
```
