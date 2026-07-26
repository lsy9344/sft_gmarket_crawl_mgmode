# Coupang OMP 수집 실측 결과 보고서 (Implementation Results)

> 작성일: 2026-07-26 (rev.6 — 구현 완료 후 실측 기록)
> 상태: **구현 완료 · 수집 성공**
> 구현체: `coupang_crawl/coupang_omp_crawler.py` (v3.0, worktree `coupang-crawler`)
>
> **이 문서가 rev.1~5 설계 문서보다 우선한다.** rev.1~5는 구현 *이전*의
> 연구/설계 문서로, 실측 결과 여러 전제가 틀렸음이 확인됐다. 현재 시점의
> 단일 진실 공급원(source of truth)은 본 문서와 실제 동작하는 코드
> `coupang_omp_crawler.py` 다.

---

## 0. 한 줄 요약

**`https://www.coupang.com/np/omp` 페이지의 '전체' 탭**(무한 스크롤 피드)에서
상품을 스크롤로 모아들인 뒤, **Coupang 자체 스토어 API 3개**를 호출해
판매자 사업자정보를 수집하는 데 성공했다. 카테고리 페이지도, 상품
상세페이지도 사용하지 않는다. **필수 수집 항목 7개 전부를 확보**했다
(이메일 포함 — 기존 설계의 "이메일 불가" 판단은 틀렸음). `url` 은 판매자당
대표 상품 1건의 URL(`itemId`/`vendorItemId` 조합)로 채운다.

---

## 1. 수집 대상 (확정)

| 항목 | 값 |
|------|-----|
| **유일한 수집 대상** | `https://www.coupang.com/np/omp` 의 **'전체' 탭** |
| 페이지 성격 | 접속 즉시 상품이 표시되는 라이브딜 피드 (클라이언트 렌더링) |
| 상품 로드 방식 | **무한 스크롤** — 스크롤할 때마다 `getPromotion` API가 다음 페이지를 로드 |
| 카테고리 | **불필요** — '전체' 탭 하나만 수집 대상. 카테고리/검색/상세페이지 미사용 |
| 페이지네이션 | `?page=N` 방식 **아님** — `continuationToken`/`nextPageKey` 커서 기반 |

> **rev.1~5와의 차이.** 기존 문서는 `/np/omp?listSize=120&page={N}` URL
> 페이지네이션과 카테고리 페이지, 상품 상세페이지 최하단 사업자정보 란
> 스크래핑을 설계했으나, 실측 결과 이들은 모두 사용 불가하거나 불필요했다.
> **수집 대상은 /np/omp '전체' 탭 하나로 확정한다.**

---

## 2. 실측으로 폐기된 설계 전제

| # | rev.1~5 전제 | 실측 결과 |
|---|-------------|----------|
| 1 | 상품 상세페이지 최하단에서 사업자정보를 직접 스크래핑 | **불가** — 상세페이지(`/vp/products/*`)는 앱 레벨에서 항상 **403**. 우회 불가 |
| 2 | 이메일 수집 불가 (Coupang 미공개) | **거짓** — `getStoreReview` API가 `repEmail` 반환. **100% 확보** |
| 3 | 한국 주거용 프록시 필수 (10~30만원/월) | **테스트 환경에서 불필요** — Camoufox headed + geoip 만으로 Akamai 통과 |
| 4 | `/np/omp?listSize=120&page={N}` URL 페이지네이션 | **거짓** — 무한 스크롤 + `getPromotion` 커서 페이지네이션 |
| 5 | 리스팅 HTML에서 `span.prod-sale-vendor-name` 셀렉터로 판매자명 추출 | **불필요** — 리스팅은 JS 렌더링 div이며, 판매자 매핑은 `individualInfo/products` API로 수행 |
| 6 | 공정위(FTC) DB 연동 (rev.1~3) → 제거 (rev.4) | 제거 판단은 옳았음. 단, 대체 경로는 상세페이지가 아니라 **스토어 API** |

---

## 3. 실제 동작하는 파이프라인 (3-API)

```
┌────────────────────────────────────────────────────────────────────┐
│  Phase 1  Camoufox 웜업                                             │
│    headed(headless=False) + xvfb-run + geoip=True + locale=ko-KR    │
│    + humanize=True → coupang.com 홈에서 자연스러운 마우스/스크롤     │
│    → Akamai _abck 검증 통과  ("웜업은 대기가 아니라 상호작용")        │
├────────────────────────────────────────────────────────────────────┤
│  Phase 2  /np/omp 로드 + getPromotion 요청 템플릿 캡처               │
│    page.on("request") 로 페이지 자신이 보내는 getPromotion POST      │
│    본문을 캡처 (디바이스 핑거프린트 문자열 포함)                     │
├────────────────────────────────────────────────────────────────────┤
│  Phase 3  getPromotion 리플레이 → vendorItemId 수집                  │
│    POST https://www.coupang.com/np/omp/api/getPromotion             │
│    캡처한 템플릿을 재사용하고 continuationToken/nextPageKey 만 갱신   │
│    → promotionData[] 에서 vendorItemId/itemId/title 추출             │
│    ⚠ 성공 판정: str(data["ret"]) == "0"  (정수 0이 아니라 문자열)    │
├────────────────────────────────────────────────────────────────────┤
│  Phase 4  vendorItemId → vendorId 매핑                               │
│    POST https://shop.coupang.com/api/v2/store/individualInfo/products│
│    body: {vendorItemIds:[...], isVIBased:true, storeId, vendorId}   │
│    → products[].storeInfoArea.{vendorId, storeId, displayName}      │
├────────────────────────────────────────────────────────────────────┤
│  Phase 5  vendorId → 사업자정보                                      │
│    GET https://shop.coupang.com/api/v1/store/getStoreReview          │
│        ?vendorId={vid}&urlName={vid}                                │
│    → name, repPersonName, businessNumber, repPhoneNum, repEmail,    │
│      repAddr1/2, eCommerceReportNumber, qualitySellerBadgeDto       │
├────────────────────────────────────────────────────────────────────┤
│  Phase 6  저장 — JSON(utf-8) + CSV(utf-8-sig)                        │
└────────────────────────────────────────────────────────────────────┘
```

### 3.1 getStoreReview 응답 → 레코드 필드 매핑 (실측 확정)

| API 응답 키 | 레코드 필드 | 필수항목 대응 |
|------------|------------|--------------|
| `name` | `company_name` (상호명) | ✅ 사업자명 |
| `repPersonName` | `ceo_name` (대표자명) | ✅ 대표자명 |
| `businessNumber` | `business_number` (사업자등록번호) | ✅ 사업자번호 |
| `repPhoneNum` | `phone` (전화번호) | ✅ 전화번호 |
| `repEmail` | `email` (이메일) | ✅ 이메일 |
| `repAddr1` + `repAddr2` | `address` (주소) | (보너스) |
| `eCommerceReportNumber` | `ecommerce_report_number` | (보너스, 대체로 null) |
| `qualitySellerBadgeDto` | `power_seller` (bool) + `power_seller_title` | 파워셀러 여부 |
| `ratingCount` / `thumbUpRatio` | `rating_count` / `thumb_up_ratio` | (보너스) |
| `storeInfoArea.displayName` (Phase 4) | `store_name` (스토어명) | ✅ 스토어 |
| `productId`+`itemId`+`vendorItemId` (Phase 4) | `url` (대표 상품 URL) | ✅ url |

---

## 4. 테스트 결과

```
수집일: 2026-07-26
환경: WSL2 Linux, xvfb-run, Camoufox headed, geoip=True, 프록시 없음
대상: https://www.coupang.com/np/omp '전체' 탭
```

| 지표 | 값 |
|------|-----|
| 스크롤로 수집한 상품 (vendorItemId) | 178개 |
| 매핑된 고유 판매자 (vendorId) | 156명 |
| 사업자정보 확보 성공 | 156명 중 마켓플레이스 판매자 전원 |
| 차단 (403/429) | 0건 |

### 4.1 필드 커버리지 (156건 실측)

| 필드 | 확보율 |
|------|--------|
| url (대표 상품 URL) | 156/156 (100%) |
| company_name (상호명) | 156/156 (100%) |
| ceo_name (대표자명) | 156/156 (100%) |
| business_number (사업자번호) | 156/156 (100%) |
| phone (전화번호) | 156/156 (100%) |
| **email (이메일)** | **156/156 (100%)** |
| address (주소) | 156/156 (100%) |
| store_name (스토어명) | 142/156 (91%) |
| power_seller (파워셀러 여부) | 156/156 (100%, 이 중 72명이 파워셀러) |

### 4.2 샘플 레코드 (개인정보 치환 — 익명 예시)

> 실제 사업자 정보는 커밋하지 않는다(`PAGE_STRUCTURE.md` §7.1 정책). 아래는
> 필드 구조만 보여주는 익명 예시로, 실측값을 치환한 것이다.

```json
{
  "vendor_id": "A00000000",
  "store_name": "예시스토어",
  "company_name": "주식회사 예시",
  "ceo_name": "홍길동",
  "business_number": "000-00-00000",
  "phone": "00-000-0000",
  "email": "example@example.co.kr",
  "address": "서울특별시 예시구 예시로 000 (예시동) 000호",
  "ecommerce_report_number": null,
  "power_seller": true,
  "power_seller_title": "파워판매자",
  "rating_count": 0,
  "thumb_up_ratio": 0
}
```

---

## 5. 필수 수집 항목 검증 (COLLECTION_SPEC.md 기준)

`COLLECTION_SPEC.md` 가 정의한 필수 수집 항목 7개 대비 실측 결과:

| # | 필수 항목 | 수집 여부 | 실측 확보율 | 비고 |
|---|----------|----------|------------|------|
| 1 | url (상품 페이지 URL) | ✅ | 100% | 판매자당 대표 상품 1건 URL — Phase 4의 `productId`/`itemId`/`vendorItemId` 로 조합 (`https://www.coupang.com/vp/products/{productId}?itemId={itemId}&vendorItemId={vendorItemId}`) |
| 2 | 사업자명 (상호명) | ✅ | 100% | `name` |
| 3 | 이메일 주소 | ✅ | 100% | `repEmail` — **기존 설계(불가)와 달리 확보 성공** |
| 4 | 대표자명 | ✅ | 100% | `repPersonName` — Gmarket보다 우수 (Gmarket은 미수집) |
| 5 | 스토어 | ✅ | 91% | `storeInfoArea.displayName` |
| 6 | 전화번호 | ✅ | 100% | `repPhoneNum` (마스킹 없음) |
| 7 | 사업자번호 | ✅ | 100% | `businessNumber` |

**결론: 필수 7항목 전부(7/7) 수집 성공.** 사업자명·이메일·대표자명·스토어·
전화·사업자번호는 `getStoreReview` 로, `url` 은 Phase 4 매핑 시 확보한 상품
ID 조합으로 채운다. `url` 은 판매자당 대표 상품 1건이며(한 판매자가 여러
상품을 운영해도 대표 1건 URL만 저장), 기술적 실패 없이 전 레코드에 채워진다.

> **Gmarket 대비.** Gmarket(`mg.gmarket.co.kr`)은 대표자명을 못 얻지만(7개 중
> 6개), Coupang은 **대표자명과 이메일을 모두** 얻어 필수 7항목을 전부
> 충족한다. 상품 URL은 Gmarket이 goodscode 로, Coupang이 대표 상품
> `productId`/`itemId`/`vendorItemId` 조합으로 만든다.

---

## 6. 알려진 제약

| 제약 | 내용 | 대응 |
|------|------|------|
| **BrandSeller 정보 없음** | 브랜드 공식 스토어(예: 레고코리아 `A00067881`)는 `getStoreReview`가 사업자정보를 `null` 반환 | 개별 마켓플레이스 판매자는 전원 정상. 브랜드 스토어는 `(brand seller - no info)` 로 로그만 남기고 스킵 |
| 상세페이지 403 | `/vp/products/*` 는 앱 레벨 차단으로 항상 403 | 사용하지 않음 — 스토어 API로 대체 |
| 무한 스크롤 중복 | 같은 vendorItemId가 반복 노출될 수 있음 | `dict` 키로 중복 제거 + 신규 0건이면 종료 |
| `ret` 타입 | `getPromotion` 성공 시 `ret`가 정수 `0`이 아니라 **문자열 `"0"`** | `str(data.get("ret")) == "0"` 로 판정 |

---

## 7. 실행 방법

```bash
# worktree: sft_gmarket_crawl_mgmode-coupang / 브랜치: coupang-crawler
cd coupang_crawl
xvfb-run -a python coupang_omp_crawler.py \
    --max-scroll-pages 10 \
    --batch-size 10 \
    --warmup-time 20
```

| 인자 | 기본값 | 설명 |
|------|--------|------|
| `--max-scroll-pages` | 10 | getPromotion 페이지 수 (페이지당 ~50건) |
| `--batch-size` | 10 | individualInfo 배치당 vendorItemId 수 |
| `--warmup-time` | 20 | Akamai 통과용 자연 상호작용 시간(초) |
| `--delay-min` / `--delay-max` | 1.0 / 2.5 | 판매자정보 요청 간 랜덤 딜레이(초) |
| `--output` | `coupang_omp_sellers_{ts}` | 출력 파일명 접두사 |

출력: `coupang_crawl/output/{prefix}.json` (utf-8) + `.csv` (utf-8-sig)

---

## 8. 문서 체계 정리 (rev.6)

| 문서 | 지위 |
|------|------|
| **`CRAWL_RESULTS.md` (본 문서)** | **현재 기준 — 구현 실측 결과** |
| `coupang_omp_crawler.py` | **동작하는 구현체** |
| `README.md` | 연구 요약 — rev.6 보정 note 추가 |
| `COLLECTION_STRATEGY.md` | rev.4/5 설계 — 실측과 다른 부분 본 문서로 대체 |
| `DATA_FIELDS_MAPPING.md` | rev.4 필드 매핑 — 실측 매핑은 §3.1 참조 |
| `PAGE_STRUCTURE.md` | rev.4 페이지 구조 — 상세페이지 403 등으로 대부분 폐기 |
| `PLATFORM_ANALYSIS.md` / `BYPASS_TECHNICAL_GUIDE.md` / `SCRAPLING_TECH_ANALYSIS.md` / `IMPLEMENTATION_*.md` | 구현 전 연구 — 참고용 |
