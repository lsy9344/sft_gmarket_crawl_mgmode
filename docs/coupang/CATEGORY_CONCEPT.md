# 컨셉 전환: 키워드 검색 → 카테고리 선택 수집 (2026-08-24)

> 상태: **구현 완료** · 워크트리 `coupang-search`
> 사용자 결정: "키워드를 '검색'하여 상품을 수집하는 컨셉을 변경한다. 쿠팡
> '카테고리' 버튼에 나오는 카테고리 중 하나를 '선택'하면 그 카테고리 수집을
> 시작한다. GUI 에는 현재 존재하는 모든 카테고리가 나와야 한다."
> 예시: `https://www.coupang.com/np/categories/221934` (출산/유아동)

## 1. 왜 이 전환이 자연스러운가

- 검색 결과 페이지네이션은 비로그인 세션에 제공되지 않음이 확정됨
  (rev.19·24·25 — URL/RSC/무JS/모바일/신뢰축적 전부 기각, 로그인 세션만 가능).
- 카테고리 리스팅(PLP)은 **비로그인에서도 `?page=1..17` 이 SSR 됨** —
  rev.11~24 에서 반복 실측된 검증 경로. 이미 뷰티 트리 14개 카테고리에서
  고유 판매자 1,580명 수집 실적.
- 즉, "페이지를 넘길 수 있는 경로"가 카테고리였으므로 수집 컨셉을 여기에 맞춘다.

## 2. 카테고리 목록 데이터 소스 (실측 2026-08-24, poc21)

- 엔드포인트: `GET /n-api/web-adapter/category-list` (JSONP) — 쿠팡 홈페이지
  **'카테고리' 버튼 메가메뉴의 데이터 소스**. 브라우저 세션 안에서 1회 요청.
- 응답: `data.gnb.{shoppingComponent, themeComponent, travelComponent, ...}` —
  각 노드 `{id, name, linkUri(/np/categories/{id}), visibleChildren[]}`, 최대 3뎁스.
- 실측 규모: **쇼핑 15 톱 / 테마 14 톱 / 여행 — 총 3,169개 카테고리 노드**.
  사용자 예시 221934 = '출산/유아동' 존재 확인.
- 앱 동작: [카테고리 목록 새로고침] 클릭 시 전용 1회 세션으로 로드 →
  `output/coupang_category_tree.json` 캐시(7일) → 이후 실행은 캐시 우선.
  초기 캐시는 실측 페이로드로 선납입 완료(3,167노드 — 그룹 간 중복 제거).

## 3. 수집 파이프라인

```
카테고리 선택 (트리에서 아무 노드나)
└─ 카테고리 수집 = /np/categories/{id}?page=1..N 순회 (기본 N=17)
   ├─ 페이지당 ~60개 카드 (로켓 제외 기본)
   ├─ 빈 페이지 2회 연속 = 상한 도달 → 종료 (빈 페이지는 차단 아님)
   ├─ 페이지 간 15~20초 랜덤 딜레이, 차단 감지 시 즉시 중단
   └─ 상품 → vendor 매핑(individualInfo) → 사업자정보(getStoreReview) → 저장
```

- 엔진: `SearchCrawler` 의 카테고리 전용 모드 (`SearchRunConfig.category_only` —
  keyword 없으면 층1 SRP/층2 밴드 스킵, 층3 PLP 만 실행). 기존 키워드 모드는
  하위 호환으로 유지(엔진/CLI).
- 출력: `coupang_category_{카테고리명}_{ts}.json/.csv` (CoupangRecord 스키마 동일).

## 4. 변경 파일

| 파일 | 내용 |
|------|------|
| `app/core/coupang/categories.py` | 신규 — 노드 계약·파서·캐시·1회 로드 세션 |
| `app/core/coupang/search_crawler.py` | keyword 선택화, 카테고리 전용 모드 |
| `app/ui/category_panel.py` | 신규 — 카테고리 트리 탭 (검색 탭 대체) |
| `app/ui/main_window.py` | 탭 교체 + 카테고리 새로고침 워커 연결 |
| `app/workers/category_worker.py` | 신규 — 트리 로드 QThread |
| `coupang_crawl/coupang_search_crawler.py` | CLI: `--category-id` 기본 경로 |
| `tests/test_coupang_categories.py` | 신규 15건 (파서·캐시·페처) |
| `tests/test_coupang_search.py` | 카테고리 전용 파이프라인·설정 검증 추가 |

전체 테스트 243건 통과. 실제 PyQt6 offscreen 스모크 통과
(트리 렌더·선택·설정 생성).

## 5. 운용 규율 (기존과 동일 — EXTERNAL_RESEARCH)

- 세션 간격 ≥ 30분, 일일 ≤ 5세션, 페이지 간 15~20초, 차단 시 즉시 중단+쿨다운.
- 카테고리 트리 로드는 캐시 우선으로 네트워크 요청 최소화 (차단 예방).
- 차단 이력: 8/22 무리한 다수 세션+무센서 요청으로 2회 차단 → 12시간 내외
  쿨다운 후 정상 복구 선례. 카테고리 로드는 1세션 1요청 수준이라 부담 최소.
