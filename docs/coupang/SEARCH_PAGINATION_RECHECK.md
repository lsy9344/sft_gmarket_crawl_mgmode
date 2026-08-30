# 검색 페이지네이션 재검토 (rev.23 재검증 기록)

> 작성일: 2026-08-22 · 작성: 재검토 세션 (main 워크트리)
> 대상: "검색 결과 2페이지 이상 빈 페이지 → 수집 불가" 판단 재검증
> 근거 문서: `coupang-search` 브랜치 `SEARCH_POC_FINDINGS.md` rev.1~22,
> 본 세션 실측 POC 7종 (`coupang_crawl/*_poc.py`, 산출물 `output/`)

---

## 0. 한 줄 요약

**"2페이지 이상 빈 페이지"는 검색 결과 페이지(SRP, `/np/search`)에만
해당하는 서버 측 게이트이며, 우회 경로는 이미 히스토리에서 발견·실측된
카테고리 PLP(`/np/categories/{id}?page=N`)다. 비로그인 SSR로 page 17까지
동작함을 오늘(08-22) 재확인했다. "수집 불가" 판단은 SRP에만 성립한다.**

---

## 1. 재검토 배경

2026-08-13 POC(`output/search_poc_*`)에서 `/np/search?q={kw}&page=N` 이
page 1만 상품(82~86개)을 주고 page 2~5는 0개(128KB 빈 셸)를 반환해
"수집 불가"로 결론 났다. 본 재검토는 이 결론의 범위를 정확히 확정하고
페이지를 볼 수 있는 방법을 검증하는 것이 목적이다.

## 2. 본 세션 독립 재검증 (2026-08-22)

| 실험 | 결과 | 판독 |
|------|------|------|
| SRP page1 로드 | ✅ 62~86개 | 정상 |
| SRP 바닥 스크롤 (JS scrollTo, 창 끝 도달 확인) | 상품 증가 0, 추가 API 호출 0 | 무한 스크롤 미구현 변형 |
| SRP page2/3 직접 로드 (`channel=user`·`component=` 변형 포함) | "검색결과가 없습니다" 렌더 | 차단 아님 — 빈 결과 서빙 |
| 페이지 내 앵커 클릭(Next 클라이언트 내비게이션) | URL은 전환되나 상품 0 (말미에 403 동반) | 클라이언트 경로도 게이트 |
| in-page RSC flight fetch (`RSC:1` 헤더) | 레이아웃만 반환, 상품 없음 | RSC 경로도 게이트 |
| `listSize=72` 변형 | 무시됨 (62개 그대로) | 파라미터 무효 |
| SRP JS 청크 오프라인 분석 | 페이지네이션 바 컴포넌트 존재하나 서버가 `disableFixedPagination:true` 로 미렌더 결정; 결과 추가 로드 API 없음(순수 SSR) | 게이트는 서버 세션 판정 |

**결론 1 — 히스토리 rev.9·rev.19 재확인.** SRP 는 어떤 URL/RSC 파라미터
조합으로도 page≥2 를 서빙하지 않는다. `srp_result` 컴포넌트에 서버가
세션별로 `disableFixedPagination:true` 를 부여하며, 자동화 세션에는 번호
페이지네이션 자체가 렌더되지 않는다. (rev.19 기준 미시도 잔존 경로: 영속
프로필 세션 신뢰도·모바일 도메인·AB 옵션 판독 — poc16 이 준비됐으나
결과 미기록 상태.)

**주의 기록.** 본 세션에서 짧은 간격 다수 요청 중 SRP 하위 요청 403 을
1회 관찰했다(이후 복구). 히스토리의 요청 규율이 왜 존재하는지 재확인됨.

## 3. 페이지를 볼 수 있는 방법 (실측 확정)

### 3.1 카테고리 PLP — `/np/categories/{categoryId}?page=N` ★ 채택 경로

히스토리 rev.11(poc12)에서 발견된 비로그인 돌파 경로. **오늘 재실측으로
여전히 동작함을 확인**:

```
실측 (2026-08-22, category_probe_poc.py, 뷰티 176522)
  page=1  HTTP 200  상품 61개
  page=2  HTTP 200  상품 61개 — 신규 51개 (실제 2페이지 데이터)
  페이지 HTML 에 page=1..10,17 링크 + data-page="next" 렌더 확인
  내장 데이터: totalPageIndex=17 — 카테고리당 최대 17페이지(약 1,020개)
```

히스토리 실측과 일치 (rev.13: 상한 page 17 / rev.16: 뷰티 17페이지 완주
406상품·229판매자 / rev.17~22: 하위 카테고리 순회로 누적 고유 1,382명).

확장 구조 (rev.12~13 확정):

```
층1  SRP 검색    — 정렬 4종 × 60개 (page≥2 불가, 이것이 전부)
층2  가격대 필터 — 공식 밴드 N종 × 60개 (isPriceRange=true&minPrice&maxPrice)
층3  카테고리 PLP — /np/categories/{id}?page=1..17 순회  ★ 페이지네이션 본체
      + 하위 카테고리 트리 순회 (뷰티 트리: 3층, 리프 14개 확인)
```

### 3.2 SRP page≥2 잔존 가능성 (미확정 — 우선순위 낮음)

poc16(영속 프로필·AB 옵션·모바일) 결과 미기록. 시도하더라도 차단 리스크
대비 기대 효과가 낮으므로, 층3 PLP 순회로 필요한 수집량을 확보하는 것이
확정된 안전 경로다.

## 4. 구현 현황

- `coupang-search` 브랜치에 3층 엔진 구현 완료:
  `app/core/coupang/search_crawler.py` (`SearchCrawler`, CLI/UI 탭 포함),
  PLP 빈 페이지 종료·로켓 제외·차단 감지·백오프 내장, 테스트 231건 통과.
- 실 수집 실적 (rev.22 기준): 사업자정보 완성 고유 판매자 **1,382명**,
  차단 0건.

## 5. 운용 규율 (필수 준수 — EXTERNAL_RESEARCH 근거, 본 세션에서 유효성 재확인)

- 세션 간격 ≥ 30분, 일일 ≤ 5세션
- 페이지 간 15~20초 랜덤 딜레이
- 차단 감지(403/빈 셸 연속/차단 키워드) 시 즉시 중단 + 쿨다운
- 비로그인 유지, 캡챠 우회 금지

## 6. 본 세션 산출물

| 파일 | 내용 |
|------|------|
| `coupang_crawl/search_pages_poc.py` | SRP 스크롤 로드 관찰 |
| `coupang_crawl/search_diag_poc.py` | 스크롤 컨테이너/센티넬 진단 |
| `coupang_crawl/search_mech_poc.py` | SRP JS 청크 확보 + page2 변형 |
| `coupang_crawl/search_api_poc.py` | in-page API/RSC 프로브 |
| `coupang_crawl/search_nav_poc.py` | 클라이언트 내비게이션 에뮬레이션 |
| `coupang_crawl/plp_probe_poc.py` | /np/search/filters 라우트 탐색 (필터 조각만 반환 — 미채택) |
| `coupang_crawl/category_probe_poc.py` | **카테고리 PLP page1/2 최종 재확인** |
| `coupang_crawl/output/srp_chunks/` | SRP JS 청크 사본 (오프라인 분석용) |
| `coupang_crawl/output/cat_plp_p*.html` | PLP page1/2 실측 HTML 증거 |
