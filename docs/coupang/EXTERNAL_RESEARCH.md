# 외부 조사 — 쿠팡 대량 수집의 "장인" 기준점 (2026-08-14)

> 웹 서치 + GitHub + 상용 스크래핑 플랫폼(Apify) 조사 결과.
> 목적: 우리의 주기/수집량 설계에 외부 근거를 확보한다.

## 1. 상용 프로 구현 (Apify 쿠팡 전용 액터)

### huggable_quote/coupang-price-scraper (공개 구현 중 가장 정교)
- 키워드 일괄 입력 + 정렬 5종(ranking/low/high/sales/newest) 지원
- **RSC 페이로드 추출**: "HTML 파싱보다 빠르고 신뢰성 높음" — 우리 rev.3 내장 JSON
  방식과 동일
- **`maxPages` 기본값 1, 예제 2~3** — 프로도 키워드당 1~3 페이지만 수집
- 내부 API 활용: quantity-info, other-seller-info
- 공식 노트: "Coupang uses Akamai Bot Manager. Residential proxies significantly
  reduce blocking risk" / "Built-in delays between page requests"
- 로그인 선택 지원(WOW 회원가 수집 목적)

### fatihtahta/coupang-scraper
- 검색/카테고리/상품 URL 수집, 기본 프록시 = Apify Residential
- `limit` 최대 50,000 (프록시 풀 전제의 이상치)
- 사용 규모: 총 사용자 166명, 월 활성 4명 → 쿠팡 검색 스크래핑은 상업적으로도
  극소수 니치 영역. "공개된 대량 수집 솔루션"은 존재하지 않음

## 2. 실운영 오픈소스 수치 (box1401/goodprice, Coupang TW 데일리 크롤러)

실제 매일 도는 프로덕션 코드 설정값:

| 항목 | 값 |
|------|-----|
| 페이지 간 딜레이 | 랜덤 3~8초 |
| 재시도 | 3회, 백오프 30/60/90초 |
| 타임아웃 | 45초 |
| UA | 세션당 랜덤 |
| 폴백 | Tor SOCKS5 |
| 운영 | 매일 1회 스케줄 (상시 아님) |

※ Coupang TW 대상. KR 본사는 방어가 더 강함 → 우리 수치는 이보다 보수적으로 유지.

## 3. 국내 개발자 실전 기록

- robots.txt: `/np/search`는 모든 봇에게 Disallow (상품상세·카테고리만
  Googlebot/NaverBot 허용)
- 2024년까지 requests+헤더로 가능했던 단순 크롤링은 현재 강화됨
  (iamgus.tistory.com/699 등)
- VM 헤드리스 Selenium → Access Denied. 봇 점수 체계(JS 지문·TLS/JA3·행동)
  분석 후 결론은 "API 직접 호출이 최선" (hed-g.me/?p=119) — 우리 rev.3 방향과 일치
- GitHub 공개 쿠팡 크롤러는 최고 12스타 수준 — 작동하는 구현은 비공개(상업)

## 4. 우리 설계와의 대조 및 확정 파라미터

| 항목 | 외부 기준 | 우리 설계 | 판정 |
|------|----------|-----------|------|
| 파싱 | RSC 내장 JSON | 동일 | ✅ |
| 확장 | 정렬 5종 순회 | 정렬 4종(랭킹순 제외) | ✅ |
| 페이지 간 딜레이 | 3~8초(프록시 풀 전제) | 15~20초(단일 IP) | ✅ 보수적 적절 |
| 백오프 | 30/60/90초 | 동일 도입 | ✅ |
| 프록시 | 주거용 필수(대량 시) | 내 IP 우선, 필요 시 sticky 3~5개 | ✅ |

### 엔진 확정 파라미터
- 페이지 간 딜레이: 15~20초 랜덤 (내 IP 기준)
- 실패 백오프: 30 → 60 → 90초, 3회 후 중단
- 세션 간격: ≥ 30분
- 일일 세션: ≤ 5
- 차단 감지: 즉시 중단 + 쿨다운 (밀어붙이기 금지)

## 출처

- https://apify.com/fatihtahta/coupang-scraper
- https://apify.com/huggable_quote/coupang-price-scraper
- https://github.com/Milastream/coupang-price-scraper (README)
- https://github.com/Milastream/coupang-scraper (README)
- https://github.com/box1401/goodprice (src/scraper.py)
- https://iamgus.tistory.com/699
- https://hed-g.me/?p=119
- https://www.coupang.com/robots.txt
