# Gmarket 카테고리 탭 — 실기동 검증 기록 (2026-09-08)

> 대상 기능: 신규 **"Gmarket 카테고리"** 탭 (국내 PC 전체 대/중/소 카테고리 선택 수집)
> 성격: 배포(Windows+한국 가정용 IP) 전, 개발 머신에서 수행한 실수집 시도·검증 기록
> 관련 문서: [`CATEGORY_RESEARCH.md`](CATEGORY_RESEARCH.md) (사이트 조사)
> 검증일: 2026-09-08

---

## 1. 요약 (한눈에)

| 검증 항목 | 결과 |
|---|---|
| 카테고리 트리 실크롤 (대/중/소 3단계, 한글명+코드) | ✅ 성공 — 대 60 / 중 723 / 소 4,765 (5,548 노드), seed 생성 |
| Phase 2 판매자정보 수집 (기존 `SellerCrawler` 재사용) | ✅ 성공 — 실제 goodscode 1건, 7필드 전부 확보 |
| Phase A 리스팅 수집 (`www.gmarket.co.kr/n/list` + patchright) | ❌ 차단 — Cloudflare 403 ×3회 (설계된 "차단/오류" 처리로 정상 종료) |
| 차단 감지 → 30초 백오프 → 재시도 → "차단/오류" 표기 로직 | ✅ 의도대로 동작 (로그로 확인) |
| 한국 환경(locale/tz) + Cloudflare 챌린지 자동해제 | ❌ 캡차는 해결됐으나 이후에도 403 — **IP 단계 차단** 판정 근거 |

---

## 2. 검증 환경

| 항목 | 값 |
|---|---|
| 머신 | 개발용 Linux (데이터센터/해외 네트워크 — 프로젝트 전제인 "한국 가정용 IP" **아님**) |
| 런타임 | venv(`/tmp/gmvenv`): scrapling 0.4.11 + patchright 1.61.2 + Chromium(rev 1228) + StealthySession 의존(playwright 1.61, curl_cffi, browserforge 등) |
| preflight | `check_gmarket_runtime()` → **통과** (앱과 동일 런타임 확인) |
| 대상 카테고리 | 중분류 `200000502` (여성의류 > 자켓/코트), 최대 10페이지 |

## 3. 시도별 상세

### 시도 A — 앱 코드 경로 그대로 (기존 Gmarket 탭과 동일 방식)

- 실행: `GmarketCategoryLister.collect()` (구현된 앱 경로 그대로, headless StealthySession)
- 기동 브라우저 직접 확인: **patchright Chromium `chromium-1228/chrome-linux64/chrome`**
  → "기존 탭의 방법"과 동일 백엔드임을 프로세스 레벨에서 확인
- 결과: warmup(307→403) 후 `/n/list?categoryCode=200000502` **3회 모두 HTTP 403**
  (각 시도 사이 30초 백오프) → `blocked=True, goodscode 0개, 140초`
- 핵심 로그:
  ```
  ERROR: Error waiting for selector a[href*="goodscode"] ... Timeout 20000ms exceeded.
  INFO: Fetched (403) <GET https://www.gmarket.co.kr/n/list?categoryCode=200000502>
  [여성의류 > 자켓/코트] 차단 감지, 30초 대기...   (×3)
  === 결과[A]: 코드 0개 blocked=True (140초) ===
  ```

### 시도 B — 한국 환경 + Cloudflare 챌린지 자동해제 (원인 판별용)

- 실행: scrapling 직접 fetch, `locale="ko-KR"`, `timezone_id="Asia/Seoul"`,
  `solve_cloudflare=True`, `network_idle=True` (3회)
- 결과: **캡차는 해결됨**(`INFO: Cloudflare captcha is solved`) → 그러나 직후
  셀렉터 대기 타임아웃 → **403 + 봇 확인 페이지(18KB, 봇 키워드 감지)** 반복, 236초
- 핵심 로그:
  ```
  INFO: Cloudflare captcha is solved
  ERROR: Error waiting for selector ... Timeout 60000ms exceeded.
  INFO: Fetched (403) <GET https://www.gmarket.co.kr/n/list?categoryCode=200000502>
  [직접] 시도3 status=403 len=18112 봇키워드=True
  ```

## 4. 판정

### 4.1 차단 원인 — 방법이 아니라 네트워크(IP)

- 같은 IP에서 `mg.gmarket.co.kr`(Phase 2)과 `category.gmarket.co.kr`(트리)은
  **정상 접근** → "Gmarket 전체가 차단"은 아님.
- `www.gmarket.co.kr`(Cloudflare)만 403. patchright 기본/한국 환경/챌린지
  자동해제 **어느 방법으로도 열리지 않음**.
- 캡차 해결 **후에도** 403이 반복되는 것은 지문·챌린지 단계가 아니라
  **IP/지역 평판 단계 거부**의 전형적 신호 → 이 네트워크에서는 기존
  "Gmarket 베스트" 탭도 동일하게 막힐 것으로 판단.
- "블랙리스트 등재 여부" 자체는 100% 단정 불가(데이터센터 IP 자동 차단/해외 IP
  정책일 가능성). 다만 "이 네트워크에서는 www 리스팅 접근 불가"는 실측 확정.

### 4.2 구현 로직 검증 (차단이 아니었다면 확인 못 했을 부분)

| 로직 | 검증 내용 |
|---|---|
| 차단 감지 | 403 상태 + 봇 키워드 HTML 감지 정상 |
| 백오프 | 403 시 30초 대기 후 재시도, 재시도 횟수(3회) 정상 |
| 종료 판정 | 재시도 소진 후 `ListingOutcome(blocked=True)` → "차단/오류" 표기 |
| 세션 정리 | 취소/종료 경로에서 with 블록 세션 종료, 잔류 프로세스 0 |
| Phase 2 연동 | 실제 goodscode `4412127826` → DS패션 official store(상호/이메일/전화/사업자번호/주소) **7필드 수집 성공** |

## 5. Camoufox(쿠팡 카테고리 탭 기술) 전환 가치 평가

**결론: 이 네트워크에서는 기대 가치가 낮아 전환을 권장하지 않는다.**

| 관점 | 평가 |
|---|---|
| 차단 원인 일치 여부 | Camoufox는 파이어폭스 기반 지문 + 지리정보 API 위장이 핵심인데, 시도 B(캡차 해결 후에도 403)로 볼 때 차단은 **지문이 아닌 IP 평판** 단계 → 엔진 교체로 해결 안 됨 |
| 네트워크 IP | Camoufox도 **동일한 출발 IP**를 사용 → Cloudflare 판정 입력값이 같음 |
| 비용 | camoufox 브라우저(~500MB) + GeoIP DB(~45MB) 설치 필요 |
| 유일한 이득 시나리오 | 차단이 IP가 아닌 브라우저 지문 기반이었을 경우 — 현재 증거(캡차 해결 후 403)와 상충 |

권장: Camoufox 실험은 **Windows + 한국 가정용 IP 환경에서도 www 가 403으로
막힐 때만** 2순위로 검토. 우선순위는 아래 "남은 검증" 1건이다.

## 6. 남은 검증 (배포 환경 — Windows + 한국 가정용 IP)

1. `/n/list?categoryCode={소분류코드}` 페이지 1 정상 렌더 + goodscode 확인
2. 페이지네이션 메커니즘 실측 (`&page=N` vs 더보기/무한스크롤) — 앱 기본값은 `&page=N` + 연속 빈 페이지 2회 종료
3. `categoryCode` vs `category` 파라미터 동작 확인 (레거시 링크는 `category=` 사용)
4. 대/중/소 어느 레벨 리스팅도 동작하는지, 페이지당 상품 수/최대 페이지 기록
   → 체크리스트 원문: [`CATEGORY_RESEARCH.md`](CATEGORY_RESEARCH.md) §4
   → 위 환경에서는 기존 Gmarket 탭이 이미 22/22 카테고리 실수집 이력(TECH_SPEC §12)이
   있으므로, 새 탭도 동일 런타임으로 검증하면 된다.

## 7. 재실행 방법 (개발 머신)

```bash
# 런타임(venv) — 1회
python3 -m venv /tmp/gmvenv
/tmp/gmvenv/bin/pip install requests==2.34.2 beautifulsoup4==4.15.0 \
    scrapling==0.4.11 patchright==1.61.2 playwright==1.61.0 \
    curl_cffi msgspec anyio protego browserforge apify-fingerprint-datapoints
/tmp/gmvenv/bin/patchright install chromium

# Phase A 실수집 시도 (코드/이름/페이지 수)
/tmp/gmvenv/bin/python -c "import sys; sys.path.insert(0,'.'); \
from app.core.base import Control; \
from app.core.gmarket_category_crawler import GmarketCategoryLister, CategoryTarget; \
l=GmarketCategoryLister(control=Control(), on_log=print, max_pages=10, page_delay_min=2, page_delay_max=3); \
print(l.collect([CategoryTarget('200000502','여성의류 > 자켓/코트')]))"
```
