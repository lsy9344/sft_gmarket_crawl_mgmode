# Coupang OMP Crawler - Research Summary

## Project Context

현 프로젝트(`sft_gmarket_crawl_mgmode`)는 Gmarket 판매자 사업자정보 수집 도구.
`coupang_crawl/` 폴더는 Coupang OMP 사이트로 수집 대상 확장을 위한 연구/설계 문서.

## Document Index

| File | Content | rev |
|------|---------|-----|
| **`CRAWL_RESULTS.md`** | **★ 구현 실측 결과 보고서 — 수집 대상(/np/omp '전체' 탭), 동작하는 3-API 파이프라인, 필수항목 검증. 현재 기준(source of truth)** | **6** |
| `PLATFORM_ANALYSIS.md` | Coupang Akamai 봇감지 체계 분석, Gmarket 대비 (구현 전 연구) | 1 |
| `PAGE_STRUCTURE.md` | OMP URL 구조, 셀렉터(**검증 상태 표기**), **사업자정보 유일 출처(§3.2)**, 파워셀러 발견 체크리스트, **우회 메커니즘 검증(§7.4)** | **5** |
| `COLLECTION_STRATEGY.md` | **4단계 파이프라인** (공정위 연동 제거), 채택/기각 경로, **세션 안티탐지 전략** | **5** |
| `BYPASS_TECHNICAL_GUIDE.md` | 검증된 안티봇 우회 기술 (코드 포함), **§11~§15: 웜업 상호작용·세션 랜덤화·프록시 canary·적응적 백오프·HTTP/2 검증** | **5** |
| `DATA_FIELDS_MAPPING.md` | 필드 매핑, **동일 사업자번호 제거**, **파워셀러**, 상세페이지 직접 스크래핑 | **4** |
| `IMPLEMENTATION_ARCHITECTURE.md` | 앱 통합 구조, 모듈 설계, 의사코드, **rev.5 신규 모듈(randomizer/backoff/health)** | **5** |
| `SCRAPLING_TECH_ANALYSIS.md` | Scrapling/Camoufox 분석 + **API 정정표(§0)** + **rev.5 리스크 보강** | **5** |
| **`IMPLEMENTATION_PLAN.md`** | **구현 계획서 — 아키텍처 결정(AD), 단계별 작업/검증 기준, §9.3~§9.7 우회 제어** | **5** |

> **rev.2 (2026-07-25)**: 수집 항목 특이사항 2건(**동일 사업자번호 제거**,
> **파워셀러 여부**) 반영 + 설치본 `scrapling 0.4.11` API 실측 대조 결과.
>
> **rev.4 (2026-07-25) — 아키텍처 정정.** rev.2/3는 사업자정보를
> **공정위(FTC) DB 매칭**으로 확보하도록 설계했으나, 이는 잘못된 전제였다.
> **필수 수집 항목은 Coupang 자체 상품 상세페이지에 전부 존재한다**
> (`PAGE_STRUCTURE.md` §3.2) — Gmarket이 `mg.gmarket.co.kr` 를 직접
> 읽듯, Coupang도 자신의 상세페이지를 직접 읽으면 된다. **공정위 연동을
> 전면 제거**하고 모든 문서를 상세페이지 직접 스크래핑 기준으로 다시 썼다.
> `SCRAPLING_TECH_ANALYSIS.md` §0 정정표를 먼저 읽을 것.
>
> **rev.5 (2026-07-25) — 안티봇 우회 보강.** rev.4 문서 검토 결과
> 7가지 약점을 식별하고 보강했다:
> ① **웜업 상호작용 주입** — 대기만이 아닌 마우스/키보드/클릭 이벤트 생성
> ② **세션 간 메타패턴 랜덤화** — 배치·정렬·타이밍·decoy 방문
> ③ **프록시 canary 검증** — flagged IP 사전 차단 + sticky session
> ④ **적응적 백오프** — 차단 누적 시 점진적 완화 (쿨다운×N, 배치-N)
> ⑤ **HTTP/2 프레임 + 헤더 순서 검증** — 스파이크 P0-4 추가
> ⑥ **핑거프린트 교차 일관성 검증** — WebGL/TZ/Screen 불일치 방지
> ⑦ **파서 건강 모니터링** — XHR 성공률 추적, 셀렉터 변경 조기 감지
> 신규 모듈: `randomizer.py`, `backoff.py`, `health.py`.
> 처리량 재산정: 150초→165초/건 (웜업 상호작용 포함), 240명 ≈ 11시간.
>
> **rev.6 (2026-07-26) — 구현 완료, 실측으로 설계 대체.** 수집에 성공했다.
> **수집 대상은 `https://www.coupang.com/np/omp` 의 '전체' 탭 하나**(무한
> 스크롤)로 확정하며, 카테고리 페이지·상품 상세페이지는 사용하지 않는다.
> 사업자정보는 상세페이지 스크래핑(rev.4/5 설계)이 아니라 **Coupang 스토어
> API 3개**(`getPromotion` → `individualInfo/products` → `getStoreReview`)로
> 확보한다 — 상세페이지는 항상 403이라 사용 불가. **이메일은 "수집 불가"라던
> rev.4/5 판단과 달리 `getStoreReview` 가 반환해 100% 확보된다.** 테스트
> 환경에서 프록시 없이 Camoufox headed + geoip 만으로 Akamai 통과.
> **필수 7항목 중 6개 100% 수집**(url 제외 — 판매자 단위 수집).
> 상세 실측 결과·필드 매핑·검증은 **`CRAWL_RESULTS.md`** 를 참조할 것.
> 본 문서를 포함한 rev.1~5 문서는 구현 전 연구로, 실측과 다르면
> `CRAWL_RESULTS.md` 가 우선한다.

## Key Findings

### 1. Platform Protection

- **Gmarket**: Cloudflare (목록만) + 무보호 `mg.gmarket.co.kr` 엔드포인트
- **Coupang**: Akamai Bot Manager (전 사이트, 5단계), 무보호 엔드포인트 없음

### 2. Feasibility Verdict

| Question | Answer |
|----------|--------|
| 동일 수집항목 수집 가능한가? | **부분 가능 (6/7 fields)** — `/np/omp` '전체' 탭 + 스토어 API 3개로 확보. 대표자명·이메일 모두 Gmarket보다 우수. 미수집 1개는 `url`(판매자 단위 수집) — `CRAWL_RESULTS.md` §5 |
| Gmarket 방식 그대로 적용? | **원리는 같음, 구현만 재설계** — 둘 다 대상 사이트가 공개한 판매자정보를 직접 읽는다. Coupang은 상세페이지(403) 대신 **스토어 API** 를 읽고 Akamai 우회가 필요할 뿐 |
| 대량 수집 가능한가? | **가능** — 테스트에서 프록시 없이 156명 수집 성공 (차단 0건) |
| 이메일 수집? | **✅ 가능 (rev.6 실측)** — `getStoreReview` 가 `repEmail` 반환, 100% 확보 (rev.4/5 "불가" 판단 폐기) |
| 전화번호 수집? | **✅ 가능 (마스킹 없음)** — `repPhoneNum` 100% 확보 |
| **파워셀러 수집?** | **✅ 가능** — `getStoreReview` 의 `qualitySellerBadgeDto` (156명 중 72명 파워셀러) |
| **동일 사업자번호 제거?** | **가능** — 단, 출력 단위가 사업자 단위로 바뀜 (구조 변경) |
| 외부 정부 DB(공정위) 필요? | **불필요** — 필수 항목이 전부 Coupang 스토어 API에 있음 |

### 3. Core Strategy

```
Gmarket: [StealthySession → 목록] + [requests → mg.* 무보호 엔드포인트]
Coupang: [StealthySession + 프록시 → 목록] → [판매자 축약] → [상세페이지 직접 스크래핑]
         → [사업자번호 중복 제거]
```

두 플랫폼 모두 원리는 같다 — **대상 사이트 자신이 공개한 판매자 정보
페이지를 직접 읽는다.** Coupang은 Akamai 보호와 동적 렌더링 때문에
`StealthySession` + XHR 캡처가 필요할 뿐, 외부 DB를 조회하지 않는다는
점은 Gmarket과 동일하다.

**고비용 구간에 진입하기 전에 대상을 줄인다**

```
수천 상품  ──D2 판매자 축약──▶  수백 판매자  ──상세페이지 직접 스크래핑──▶  사업자정보 확보
  (리스팅)                     (전원 방문 — 대체 경로 없음)                       │
                                                                    사업자 N행  ◀──D3 병합──┘
```

- 상품 단위로 상세를 열면 같은 판매자 정보를 수십 번 재수집한다 —
  D2(판매자 단위 축약)만이 유일한 비용 절감 수단이다
- 4단계 파이프라인 중 **프록시가 필요한 것은 2단계(리스팅 스캔, 상세
  스크래핑)뿐**이지만, 나머지 단계는 그 산출물을 가공만 하므로 **사실상
  전체가 프록시에 의존**한다

### 4. Cost Comparison

| Item | Gmarket | Coupang |
|------|---------|---------|
| Proxy | 0원 | 10~30만원/월 |
| 외부 API/DB | 0원 (없음) | **0원 (없음, rev.4)** |
| Daily capacity | ~2,000건 | ~500건 (판매자 단위) |
| Setup complexity | Low | High |

### 5. 착수 전 결정 필요

**프록시 예산 집행이 프로젝트 실행의 전제조건이다.** rev.2는 "공정위
단독으로도 부분 가치가 있다"고 판단했으나, **rev.4에서는 성립하지
않는다** — 공정위 연동 자체가 제거됐고, 필수 항목은 Coupang 자체
상세페이지에서만 얻을 수 있어 프록시 없이는 아무 것도 수집할 수 없다.
전체 결정 목록은 `IMPLEMENTATION_PLAN.md` §16 (Q1~Q6).

## Research Date

2026-07-25

## Sources

- 쿠팡 크롤링 2026 완벽 가이드 (blog.hashscraper.com)
- 2025년 쿠팡 DB 크롤링 현실 (cosmowifi.tistory.com)
- AI와 함께한 크롤링 여정, 쿠팡을 뚫기까지 (cosmowifi.tistory.com)
- 봇 탐지 우회 기법 (cosmowifi.tistory.com)
- How to Bypass Anti-Bot Protection 2026 (scrapfly.io)
- How to Scrape Coupang Product Listings (falconscrape.com)
- Coupang Data Scraping Enterprise Guide (kndusc.com)
- 쿠팡 Open API (developers.coupang.com)
- 쿠팡 파트너스 API 가이드 (coupang-partners.tistory.com)
- 웹 크롤링 실전 - 쿠팡 (iamgus.tistory.com)
- 크롤링을 통한 데이터 수집 feat. 쿠팡 (velog.io)
- Coupang Seller Competition Monitoring (apify.com)
- How to Bypass Akamai's Bot Detection (scraperapi.com)
