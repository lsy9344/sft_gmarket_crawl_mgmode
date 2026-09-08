# Gmarket 전체 카테고리 조사 기록 (CATEGORY_RESEARCH)

> 실측일: 2026-09-08 · 대상: 국내 PC(www) 한글 카테고리 + 대/중/소 3단계
> 관련 구현: `app/core/gmarket_categories.py`(트리), `app/core/gmarket_category_crawler.py`(리스팅)

---

## 1. 결론 (요약)

| 항목 | 확정값 |
|---|---|
| 트리 데이터 소스 | `https://category.gmarket.co.kr/listview/L{code}.aspx` — **Cloudflare 없음, 브라우저 불필요** |
| 트리 구성 | 대분류 60 · 중분류 723 · 소분류 4,765 (합계 5,548 노드, 한글명+코드) |
| 리스팅 URL | `https://www.gmarket.co.kr/n/list?categoryCode={code}` — **Cloudflare Managed Challenge** → StealthySession 필요 |
| 코드 체계 | 대분류 `100000xxx` / 중분류 `200000xxx` / 소분류 `300000xxx` (9자리, 국내·글로벌 공용) |
| 시드 캐시 | `app/resources/gmarket_category_tree.json` (2026-09-08 실측 생성, 만료 없음) |
| 판매자정보(Phase 2) | 기존 `mg.gmarket.co.kr/SellerInfo/SellerInfo?goodscode={}` 그대로 재사용 |

---

## 2. 발견 과정 (핵심 실측)

### 2.1 국내 PC 카테고리 리스팅 — `/n/list`

- `www.gmarket.co.kr` 홈/`n/list` 모두 Cloudflare Managed Challenge 확인
  (일반 HTTP 클라이언트는 "간단한 확인 안내" 17KB 봇 확인 페이지 수신).
- 홈 SSR HTML 에서 확인된 현재 페이지 링크:
  `https://www.gmarket.co.kr/n/stardelivery/category?categoryCode=100000003` →
  **`categoryCode` 파라미터가 현행 표준**.
- 기존 best/superdeal 리스팅(`n/best`, `n/superdeal`)과 동일한 보호 수준이므로
  동일한 StealthySession 방식으로 fetch 하면 된다(기존 `Prescanner` 패턴).

### 2.2 전체 카테고리 트리 — 레거시 `category.gmarket.co.kr` (Cloudflare 없음!)

- `category.gmarket.co.kr/listview/L100000103.aspx` (HTTP 200, euc-kr) 서버
  렌더링 HTML 에 다음이 **전부 내장**되어 있다:
  1. **전체 대분류 네비**: `<a href=".../listview/LList.aspx?gdlc_cd={code}">한글명</a>`
  2. **해당 대분류의 중/소분류 트리**: 문서 순서대로
     `<a href="http://www.gmarket.co.kr/n/list?category=200002669">티셔츠</a>`(중분류)
     → 그 아래 `<a ... category=300026660>무지 티셔츠</a>`(소분류) …
- 대분류별로 **자기 페이지의 서브트리만** 담는다(교차 포함 없음 — 검증 완료).
- 레거시 페이지가 생성하는 리스팅 링크는 `category=` 파라미터를 쓴다 —
  `n/list` 가 `categoryCode`/`category` 를 모두 받는지는 배포 환경 실측으로 확정
  (현행 표준은 categoryCode, 실패 시 category 폴백 코드가 남아 있음).

### 2.3 코드 체계 — 국내/글로벌 공용 확인

- 글로벌 모바일 `mg.gmarket.co.kr/AllCategory` JSON(Cloudflare 없음)의 코드가
  국내 PC 대분류 코드와 **일치**: `100000003` = 여성의류(Women's Clothing),
  `100000103` = 브랜드 여성의류 등.
- 기존 best 의 `groupCode`(예: 100000003=뷰티)는 **카테고리 코드가 아님**
  (베스트 그룹 전용 번호) — 혼동 주의.

### 2.4 참고: 무차단 대안 (사용 안 함)

- `mg.gmarket.co.kr/Category/List?lcId={code}` + `POST /Search/SearchJson`
  (pageNo/pageSize/TotalGoodsCount) → Cloudflare 없이 goodscode 수집 가능.
- 카테고리명이 영어 + 글로벌(국제배송) 카탈로그일 수 있어, 사용자 결정
  (국내 PC 한글) 에 따라 주 경로로 채택하지 않음.

---

## 3. 시드 캐시 생성·갱신

- 파일: `app/resources/gmarket_category_tree.json` (구조:
  `{fetched_at, fetched_ts, node_count, roots:[{code,name,level,children}]}`)
- 앱 UI: **"카테고리 목록 새로고침"** → `GmarketCategoryTreeFetcher` 가
  대분류 약 60장을 순회(페이지당 ~0.4초 휴식, 30~60초) 후 `output/gmarket_category_tree.json`
  (7일 TTL) 저장. 새로고침 불가 시 번들 seed 로 폴백.
- 재생성(개발):
  ```python
  from app.core.gmarket_categories import GmarketCategoryTreeFetcher, GmarketCategoryTreeCache
  roots = GmarketCategoryTreeFetcher(on_log=print).fetch()
  GmarketCategoryTreeCache(path=..., seed_path=...).save(roots)
  ```

---

## 4. 배포 환경 실측 체크리스트 (Windows + patchright Chromium)

구현의 페이지네이션 기본값은 `&page={n}` 쿼리 + 연속 빈 페이지 2회 종료이다.
첫 실수집 전에 아래를 확인해 `app/core/config.py` 주석과 이 문서를 갱신한다.

1. [ ] `/n/list?categoryCode={소분류코드}` 페이지 1에 goodscode 링크가 렌더되는가
   (`wait_selector='a[href*="goodscode"]'` 기준).
2. [ ] 2페이지 이동 방식: `&page=2` 쿼리가 동작하는가, 아니면 "더보기" 버튼/무한
   스크롤인가. 다르면 `GmarketCategoryLister` 의 페이지 순회를 그 방식으로 교체.
3. [ ] `/n/list?category=` (레거시 링크)도 같은 결과를 주는가 → categoryCode 와
   동일하면 폴백 불필요.
4. [ ] 중분류·소분류·대분류 어느 레벨에서 리스팅해도 동작하는가.
5. [ ] 페이지당 상품 수(기본 60 내외?)와 최대 노출 페이지 수를 기록.

### 차단 시 관찰 포인트

- 리스팅 HTML 에 "기다리십시오/확인 안내/자동입력 방지" 키워드 → 차단 판정
  (기존 `BOT_KEYWORDS`) 후 `CLOUDFLARE_WAIT`(30초)×재시도, 그래도 실패 시
  해당 카테고리는 "차단/오류" 로 표시하고 계속 진행.

---

## 5. 수집 흐름 (구현 반영)

```
[Gmarket 카테고리 탭]
  카테고리 트리(대/중/소) 표시 ── 캐시/seed → 새로고침(listview 60장 GET)
  선택(+하위 포함) → GmarketCategoryCrawlWorker
    Phase A: StealthySession 1회 → /n/list?categoryCode= 1..N 페이지 → goodscode
    Phase B: build_crawl_plan + SellerCrawler(기존) → mg.gmarket.co.kr 판매자정보
             → JSON/CSV + collected_ids/체크포인트(기존 내구성 규율 그대로)
```
