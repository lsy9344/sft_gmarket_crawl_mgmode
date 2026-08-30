# foodspring.co.kr (식봄) 수집 차단 분석 및 우회 방법

> 작성일: 2026-08-27
> 대상: https://www.foodspring.co.kr/special/wcpd (전국 택배 배송 기획전)

---

## 1. 봇차단/보안 스타일 요약

| 경로 | 보호 방식 | 공격 난이도 |
|------|-----------|------------|
| `/special/wcpd` (상품 목록) | 없음 (일반 HTTP 200) | 낮음 |
| `/goods/detail/{id}` (상품 상세) | 없음 (일반 HTTP 200, rate-limit도 없는 수준) | 낮음 |
| `/seller/{id}` (셀러 페이지) | **AWS WAF JS challenge** (CloudFront, `x-amzn-waf-action: challenge`, HTTP 202 + 빈 body) | 중간 |
| `api.foodspring.co.kr/v2/graphql` | 게스트 세션 쿠키(`FS_TOKEN`) 요구. 쿠키 없으면 `CART.NOT_FOUND_CART` 오류 | 낮음(세션 1회 확보) |

- 전면 Cloudflare가 아니라 **경로별로 선택적 차단** (셀러 페이지 경로만 WAF로 보호).
- 상품 목록/상세는 scripts/curl로도 그대로 열리는 수준.
- 페이지네이션은 URL 파라미터(`?page=2`)가 **동작하지 않음** (항상 같은 80개 반환).
  → Next.js + Apollo GraphQL 기반 cursor 페이지네이션을 사용함.

## 2. 구조 파악 (Next.js + GraphQL)

- 사이트는 Next.js SSR + Apollo Client 캐시(`__NEXT_DATA__`).
- 목록 조회 쿼리: `SimpleGoodsListPageNationQuery`
  - 엔드포인트: `POST https://api.foodspring.co.kr/v2/graphql`
  - `goodsList(first:80, after:{cursor}, input:{categoryId:null, delivery:"PARCEL", sort:"POPULAR_DESC", terms:""})`
  - cursor는 base64 `Y3VzdG9tLWN1cnNvcjA=` = `custom-cursor0` 형태, 80개씩 순차 증가.
  - 전체 상품 수: **10,000개** (cursor 0~9999, 이후 반복 → 종료 판정)
- 목록 응답에 `vendor{nid, name}` 포함 → 셀러 ID/이름 확보 가능.
- 상품 상세 HTML의 `__NEXT_DATA__` Apollo 캐시에 `vendor_{nid}` 객체가 있으며
  판매자 사업자정보(상호, 사업장소재지, 연락처, 사업자등록번호, 통신판매신고번호,
  고객센터전화) 전체 포함.
- 셀러 페이지(`/seller/{id}`)의 정보는 상품 상세 페이지 정보와 동일
  (대표자명·이메일은 공개 정보로 노출되지 않음).

## 3. 우회 방법 (구현에 반영됨)

1. **세션 확보**: Playwright Chromium(headless) 1회 실행으로 `/special/wcpd` 방문
   → `FS_TOKEN` 등 게스트 세션 쿠키 획득 (1년 유효).
2. **상품 전체 수집**: 획득한 쿠키로 GraphQL API를 cursor 순회 호출(125페이지).
   - `requests`/`urllib` 수준에서 가능 (브라우저 불필요), 0.6~1.2초 간격.
   - nid 기준 중복 제거.
3. **셀러 사업자정보**: 고유 셀러별 대표 상품 1개의 상세 페이지를 일반 HTTP로
   fetch → `__NEXT_DATA__`의 `vendor_{nid}` 객체 파싱.
   - 상세 페이지는 WAF 적용 대상이 아니므로 우회 불필요.
   - 3개 워커 병렬 + 랜덤 지연(1~2초) + 실패 시 재시도/대체 상품 지정.
4. **(예비) 셀러 페이지가 필요한 경우**: Scrapling StealthySession이
   AWS WAF challenge를 자동 통과함을 확인 (`https://www.foodspring.co.kr/seller/3575`
   → 200 + 235KB HTML). 이 프로젝트의 기존 Gmarket 크롤러가 쓰는 방식과 동일 자산.

## 4. 수집 항목 매핑 (쿠팡 수집 항목 기준)

| 쿠팡 크롤러 필드 | foodspring 대응 | 출처 |
|------------------|------------------|------|
| vendor_id | 셀러ID (`vendor.nid`) | GraphQL 목록 |
| store_name / company_name | 셀러명(스토어) | 목록 `vendor.name` / 상세 `vendor.name` |
| business_number | 사업자등록번호 | 상세 `businessRegistrationNumber` |
| phone | 연락처 | 상세 `contact` |
| address | 사업장 소재지 | 상세 `businessAddress` |
| ecommerce_report_number | 통신판매 신고번호 | 상세 `mailOrderRegistrationNumber` |
| (없음) | 고객센터 전화 | 상세 `customerServiceNumber` |
| email | **공개 정보 없음** | - |
| ceo_name | **공개 정보 없음** | - |
| power_seller / rating | **노출 없음**(셀러평점은 그래프QL 미노출) | - |

## 5. 실행 방법

```bash
# 전체 실행 (상품 10,000개 + 셀러 정보 + 엑셀)
python foodspring_crawl/foodspring_seller_crawler.py --max-pages 200 --workers 3

# 상품만 수집
python foodspring_crawl/foodspring_seller_crawler.py --max-pages 200 --skip-sellers

# 갱신 시 (상품 목록만 새로 수집하고 셀러 정보는 state 파일로 재사용)
python foodspring_crawl/foodspring_seller_crawler.py --max-pages 200 --skip-session
```

- 중간 상태: `foodspring_crawl/state/` (products.json, sellers.json, cookie.txt)
- 결과물: `foodspring_crawl/output/foodspring_wcpd_*.xlsx`
  - 시트1 `상품목록` (10,000행): 상품URL·상품명·판매가·할인율·이미지·셀러ID·셀러명·사업자정보
  - 시트2 `셀러정보`: 셀러ID·셀러명·사업자등록번호·연락처·소재지·통신판매신고번호·고객센터전화·판매상품수