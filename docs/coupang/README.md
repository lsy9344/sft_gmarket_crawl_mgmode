# Coupang OMP Crawler - 문서 인덱스

## 2026-09-28 직접 접속 중단 조사

- [12:58~13:01 Sandbox 재개 실패: Decodo 초기 홈 403](research/SANDBOX_PROXY_HOME_BLOCK_20260928.md): 완료 6개·목록 39개 정상 인식. 한국 프록시 IP 3개 모두 초기 Chromium 홈에서 차단됐으며, 집 회선 전환·판매자 조회 전 실패.
- [판매자 조회를 Decodo로 옮기는 제안 검토](research/PROXY_SELLER_PROPOSAL_REVIEW_20260928.md): 한 판매자 API의 프록시 200 원본 확인. 집 IP 점수 원인 단정·과거 Camoufox 전면 차단 주장 검토, 실제 앱 적용 전 확인 범위.
- [Sandbox 수집 수량과 Decodo 사용량](research/SANDBOX_COLLECTION_USAGE_20260928.md): 선택 대상 13개 중 6개 완료·1개 중단·6개 미시작. 판매자 중복 제거 319명. Decodo 최근 24시간 약 0.29GB, $4/GB 환산 약 $1.16(동일 계정 테스트 포함).
- [Sandbox Chromium 직접 접속 차단·재개와 바깥 테스트의 관련성](research/SANDBOX_DIRECT_BLOCK_20260928.md): 약 39분 뒤 재개해 6번째 카테고리 상품 73개·판매자 36명 완료. 이후 00:50에 7번째 목록 39개 수집 후 직접 홈 접속에서 재차 차단. 12시간 대기·동시 시험 원인 주장 검토 및 카테고리마다 새 직접 세션을 만드는 구조 확인.

## 2026-09-27 브라우저 변경 및 운영 판단

- [현재 운영 판단과 Patchright 워크트리 비교](research/BROWSER_ENGINE_DECISION_20260927.md): 현재 엔진 배치, 자동 전환 판단, 과거 실험과의 차이, 새 PC 사용 순서.
- [Windows Sandbox 수정·검증 보고서](CHROMIUM_CATEGORY_FIX_20260927.md): 브라우저 변경과 거짓 매핑 누락 오류 수정. 실제 Sandbox에서 누락 연결 28건 복구, 상품 97개·판매자 69명 정상 완료 후 세 번째 카테고리 진행 확인.
- [브라우저 공식 출처 조사](research/BROWSER_ENGINE_PRIMARY_SOURCES_20260927.md): Patchright·Chromium·Chrome·Camoufox의 관계와 출처.

위 문서는 9월 27일 현재 카테고리 수집에 관한 기록이다. 아래 rev.6 및 구현 전 연구의 설명은 당시 범위에 대한 역사적 기록으로 읽는다. 아래의 “research/ 폐기” 안내는 그 표에 열거한 구현 전 문서에만 적용한다.

## Project Context

현 프로젝트(`sft_gmarket_crawl_mgmode`)는 Gmarket 판매자 사업자정보 수집 도구
(PyQt6 Windows 앱). `docs/coupang/` 은 Coupang OMP 로 수집 대상을 확장하기 위한
문서 폴더다. **구현은 완료되어 수집에 성공했으며**(rev.6), 향후 지마켓/쿠팡
**2탭 UI 앱**으로 통합될 예정이다.

- **구현체(코드)**: `coupang_crawl/coupang_omp_crawler.py` (v3.0)
- **수집 결과 데이터**: `coupang_crawl/output/` (gitignore — 커밋 대상 아님)

## Document Index

### ★ 현재 문서 (rev.6 — 구현 실측 기준)

| File | Content | rev |
|------|---------|-----|
| **`LOGIN_ACCESS_DENIED_INCIDENT_20260831.md`** | **★ 2026-08-31 개발·배포 PC 로그인 단계 Akamai Access Denied 사고 기록 — 현재 HOLD, 재개 조건과 필수 보완사항** | **1** |
| **`WORK_ORDER.md`** | **Gmarket PyQt6 앱에 Coupang v3를 2탭으로 통합하기 위한 구현 작업지시서 — 문서 우선순위, 변경 파일, 스레드/종료/저장 계약, 테스트와 완료 기준** | **1.0** |
| **`CATEGORY_CONCEPT.md`** | **★ 컨셉 전환(2026-08-24): 키워드 검색 → 카테고리 선택 수집 — 카테고리 트리 데이터 소스·수집 파이프라인·변경 파일** | **1** |
| `SEARCH_POC_FINDINGS.md` | 검색/페이지네이션 실측 연대기 rev.1~25 — SRP page≥2 비로그인 기각 확정, 카테고리 PLP 돌파 경로 발견 | 25 |
| `FINAL_DATA_REPORT.md` | 뷰티 트리 14개 카테고리 수집 최종 리포트 — 고유 판매자 1,580명 | 1 |
| **`CRAWL_RESULTS.md`** | **★ 구현 실측 결과 보고서 — 수집 대상(/np/omp '전체' 탭), 동작하는 3-API 파이프라인, 필수항목 검증(7/7), '전체' 탭 피드 총량 조사(§4.3, 180개). 현재 기준(source of truth)** | **6** |
| `COLLECTION_STRATEGY.md` | 수집 전략 — '전체' 탭 단일 대상, 스토어 API 3개 파이프라인, 프록시 불필요 | **6** |
| `PAGE_STRUCTURE.md` | OMP URL/API 구조 — getPromotion 커서 페이지네이션, 상세페이지 403 | **6** |
| `DATA_FIELDS_MAPPING.md` | 필드 매핑 — 스토어 API 필드 → 레코드, 필수 7항목 100% 확보 | **6** |

### research/ — 구현 전 연구 (rev.1~5, 실측으로 폐기)

> 아래 문서는 구현 *이전*의 연구/설계 문서로, **실측 결과 여러 전제가 틀렸음이
> 확인되어 폐기**됐다. 현재 기준과 다르면 **`CRAWL_RESULTS.md` 가 우선한다.**
> 역사적 참고용(안티봇 우회 연구 등)으로 보관한다.

| File | Content | rev |
|------|---------|-----|
| `research/PLATFORM_ANALYSIS.md` | Coupang Akamai 봇감지 체계 분석, Gmarket 대비 | 1 |
| `research/BYPASS_TECHNICAL_GUIDE.md` | 안티봇 우회 기술 연구 (웜업·세션 랜덤화·백오프 등) | 5 |
| `research/FREE_BYPASS_RESEARCH.md` | 무료 우회 경로 연구 | 5 |
| `research/IMPLEMENTATION_ARCHITECTURE.md` | 앱 통합 구조·모듈 설계 (구현 전 구상) | 5 |
| `research/SCRAPLING_TECH_ANALYSIS.md` | Scrapling/Camoufox 분석 (상세페이지 스크래핑 전제 — 폐기) | 5 |
| `research/IMPLEMENTATION_PLAN.md` | 구현 계획서 — 아키텍처 결정, 단계별 작업 (구현 전) | 5 |

> **rev.6 (2026-07-26) — 구현 완료, 실측으로 설계 대체.** 수집에 성공했다.
> **수집 대상은 `https://www.coupang.com/np/omp` 의 '전체' 탭 하나**(무한
> 스크롤)로 확정하며, 카테고리 페이지·상품 상세페이지는 사용하지 않는다.
> 사업자정보는 상세페이지 스크래핑(rev.4/5 설계)이 아니라 **Coupang 스토어
> API 3개**(`getPromotion` → `individualInfo/products` → `getStoreReview`)로
> 확보한다 — 상세페이지는 항상 403이라 사용 불가. **이메일은 "수집 불가"라던
> rev.4/5 판단과 달리 `getStoreReview` 가 반환해 100% 확보된다.** 테스트
> 환경에서 프록시 없이 Camoufox headed + geoip 만으로 Akamai 통과.
> **필수 7항목 전부(7/7) 수집** (url은 판매자당 대표 상품 URL로 확보).
> '전체' 탭 피드 총량은 **약 180개 고유 상품**(자연 종료)으로 실측됐다
> (`CRAWL_RESULTS.md` §4.3).

---

## Key Findings (rev.6 실측 기준)

### 1. Platform Protection

- **Gmarket**: Cloudflare (목록만) + 무보호 `mg.gmarket.co.kr` 엔드포인트
- **Coupang**: Akamai Bot Manager (전 사이트) — 그러나 테스트 환경에서
  **프록시 없이** Camoufox headed + geoip + humanize 로 통과 실측

### 2. Feasibility Verdict

| Question | Answer |
|----------|--------|
| 동일 수집항목 수집 가능한가? | **가능 (7/7 fields)** — `/np/omp` '전체' 탭 + 스토어 API 3개로 확보. 대표자명·이메일 모두 Gmarket보다 우수. `url` 은 판매자당 대표 상품 URL — `CRAWL_RESULTS.md` §5 |
| Gmarket 방식 그대로 적용? | **원리는 같음, 구현만 재설계** — 둘 다 대상 사이트가 공개한 판매자정보를 직접 읽는다. Coupang은 상세페이지(403) 대신 **스토어 API** 를 읽는다 |
| 대량 수집 가능한가? | **'전체' 탭 전량 = 약 180개 상품/156명 판매자** — 유한 피드라 전량 수집해도 소규모, 차단 0건 (`CRAWL_RESULTS.md` §4.3) |
| 이메일 수집? | **✅ 가능** — `getStoreReview` 가 `repEmail` 반환, 100% 확보 (rev.4/5 "불가" 판단 폐기) |
| 전화번호 수집? | **✅ 가능 (마스킹 없음)** — `repPhoneNum` 100% 확보 |
| 파워셀러 수집? | **✅ 가능** — `getStoreReview` 의 `qualitySellerBadgeDto` (156명 중 72명 파워셀러) |
| 외부 정부 DB(공정위) 필요? | **불필요** — 필수 항목이 전부 Coupang 스토어 API에 있음 |
| 프록시 필요? | **테스트 환경에서 불필요** — rev.4/5 의 "프록시 10~30만원/월 전제"는 폐기 |

### 3. Core Strategy (rev.6)

```
Coupang: [Camoufox 웜업 → /np/omp '전체' 탭] → getPromotion(vendorItemId)
         → individualInfo/products(vendorId 매핑) → getStoreReview(사업자정보)
```

대상 사이트 자신이 공개한 판매자 정보(스토어 API)를 직접 읽는다 — 외부
DB를 조회하지 않는다는 점에서 Gmarket과 원리가 같다.

## Research Date

2026-07-25 (연구) · 2026-07-26 (구현 완료·실측)

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
