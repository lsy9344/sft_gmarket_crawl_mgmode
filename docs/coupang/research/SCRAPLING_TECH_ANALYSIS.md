> **⚠ 아카이브됨 — 구현 전 연구 (rev.1~5).** 이 문서는 구현 *이전*의 연구/설계
> 문서로, 실측 결과 여러 전제가 폐기됐다. 현재 기준은 **`../CRAWL_RESULTS.md`** 가
> 우선한다. 역사적 참고용(안티봇 우회 연구 등)으로 보관한다.

# Scrapling 기술 조사 분석 — Coupang 수집 적용 가이드

> 조사일: 2026-07-24 | 대상: Scrapling >= 0.4.11 (github.com/D4Vinci/Scrapling)
> 목적: 쿠팡 Akamai 봇감지 우회를 위한 Scrapling 활용 기술 완전 분석
>
> **최종 개정: 2026-07-25 (rev.4)** — rev.2: 설치본(`scrapling 0.4.11`)
> API 실측 대조 결과 반영. **rev.4: 공정위(FTC) 연동 전면 제거** — 필수
> 수집 항목은 Coupang 자체 상품 상세페이지에 전부 존재하므로 외부 DB
> 매칭이 불필요하다(`DATA_FIELDS_MAPPING.md` rev.4). §6.1의 공정위 API
> 예제와 §11의 `ftc_matcher.py` 관련 모듈은 삭제했다. §0 정정표를
> **먼저** 읽을 것.

---

## 0. rev.2 API 정정표 ★ 코드 작성 전 필독

본 문서 초판의 코드 예시 상당수가 **실제 API와 달라 그대로는 동작하지
않는다.** 아래는 `.venv` 설치본을 introspect 해 확인한 결과다.

검증 근거:
`scrapling/engines/_browsers/_types.py` (`StealthSession` /
`StealthFetchParams` TypedDict),
`scrapling/engines/toolbelt/proxy_rotation.py`,
`scrapling/engines/toolbelt/custom.py`.

### 0.1 오류 정정

| 위치 | 초판 기재 | **실제 API** | 증상 |
|------|-----------|-------------|------|
| §3.4, §5.1, §5.2 | `session.fetch(url, proxies=PROXY)` | **`proxy=`** (단수) | `TypeError` / 프록시 무시 |
| §3.4 | 프록시 dict `{"http":..., "https":...}` | `str \| dict \| tuple` — dict는 **Playwright 스타일**(`server`/`username`/`password`) | 프록시 미적용 |
| §3.5 | "Scrapling이 자동으로 DoH 라우팅" | `dns_over_https: bool` **명시 지정 필요** | DNS 누출 |
| §3.6 | `session.fetch(url, block_ads=True)` | `block_ads` 는 **세션** 파라미터 | 광고 차단 미적용 |
| §5.4 | `add_session(sid=..., proxies=[...])` | `proxy_rotator=ProxyRotator([...])` | 회전 미동작 |
| §6.1 | `FetcherSession(...)` 에서 바로 `.get()` | `get/post/put/delete` 는 **컨텍스트 진입 후에만** 노출 | `AttributeError` |

```python
# ❌ 초판 (동작하지 않음)
with StealthySession(headless=True) as s:
    page = s.fetch(url, proxies={"http": P, "https": P}, block_ads=True)

# ✅ 실제
from scrapling.engines.toolbelt.proxy_rotation import ProxyRotator

with StealthySession(
    headless=True,
    proxy="http://user:pass@kr-residential:port",   # 단수, 세션 레벨
    proxy_rotator=ProxyRotator([P1, P2, P3]),       # 회전이 필요하면
    dns_over_https=True,                            # 명시 지정
    block_ads=True,                                 # 세션 레벨
    solve_cloudflare=True,
) as s:
    page = s.fetch(url, network_idle=True)          # 요청별 proxy= 오버라이드도 가능
```

```python
# ❌ 초판                          # ✅ 실제
FetcherSession(...).get(url)      with FetcherSession(impersonate="chrome") as s:
                                      resp = s.get(url, params=..., stealthy_headers=True)
```

### 0.2 초판에 없던 중요 파라미터 (전부 실측 확인)

| 파라미터 | 위치 | 왜 중요한가 |
|----------|------|------------|
| **`capture_xhr: str \| None`** | **세션** | **사업자정보 AJAX 응답을 직접 캡처.** 최하단 DOM 파싱의 폴백 수단 (§5.2a) |
| `proxy_rotator: ProxyRotator` | 세션 | 세션마다 IP 교체 (Coupang 필수 요건) |
| `page_action: Callable` | 세션/fetch | 스크롤·클릭 주입점 — **Akamai L4(행동) 우회의 실제 구현 자리** |
| `retries` / `retry_delay` | 세션 | 내장 재시도 |
| `real_chrome: bool` | 세션 | §12 "Firefox 소수 브라우저" 리스크의 대안 |
| `wait_selector` + `wait_selector_state` | 세션/fetch | 동적 렌더 완료 대기 |
| `blocked_domains: set[str]` | 세션/fetch | 불필요 도메인 차단 |
| `Response.captured_xhr: list[Response]` | 응답 | `capture_xhr` 결과 회수 |

### 0.3 rev.4 — 공정위 연동 전면 제거

이전 판(rev.2)은 성능 관련 정정으로 "공정위 Levenshtein 매칭은 2단계
알고리즘이 필요하다", "CSV는 CP949 인코딩이다" 등을 기술했다. **rev.4에서
공정위 연동 자체를 제거**했으므로 이 항목들은 전부 무의미해졌다.

필수 수집 항목(상호명·대표자명·사업자등록번호·주소·전화)은 Coupang 상품
**상세페이지 최하단 사업자정보 란**에 전부 존재한다
(`PAGE_STRUCTURE.md` §3.2). 통신판매업신고번호는 수집 대상이 아니다.
Gmarket이 `mg.gmarket.co.kr` 를 직접 읽듯, Coupang도 자신의 상세페이지
최하단을 직접 읽으면 된다 — 외부 정부 DB를 조회·매칭할 이유가 없다.
이에 따라 다음이 전부 제거된다:

- SQLite + FTS5 매칭 엔진, `thefuzz`/`python-Levenshtein` 의존성
- 공정위 CSV 다운로드/적재 절차
- §6.1의 `FetcherSession` 공정위 API 예제
- §11의 `ftc_matcher.py`/`ftc_db.py` 모듈

대신 **§3.7(XHR 캡처)이 사업자정보 확보의 유일한 경로**가 된다 — 대체
경로가 없으므로 §5.2의 상세 스크래핑 견고성(재시도, DOM 폴백)이 그만큼
더 중요해진다.

---

## 1. Scrapling 개요

Scrapling은 Python 기반 적응형 웹 수집 프레임워크로, 단순 HTTP 요청부터 대규모 크롤링까지
단일 인터페이스로 처리한다. 핵심 차별점:

| 특성 | 설명 |
|------|------|
| 적응형 파싱 | DOM 변경 시 요소 위치를 자동 재탐색 (auto_save/adaptive) |
| 3단계 Fetcher | HTTP → Dynamic(Playwright) → Stealthy(Camoufox) 에스컬레이션 |
| 내장 안티봇 우회 | Cloudflare Turnstile/Interstitial 자동 해결 |
| TLS 핑거프린트 | curl_cffi 기반 브라우저 TLS 서명 모방 (HTTP/3 지원) |
| Spider 프레임워크 | Scrapy 유사 비동기 스파이더 + 체크포인트 재개 |
| 성능 | JSON 직렬화 표준 대비 10x, 메모리 효율적 |

### 설치 (프로젝트 requirements.txt에 이미 포함됨)

```bash
pip install "scrapling[fetchers]>=0.4.11"
scrapling install --force   # 브라우저 바이너리 + 핑거프린트 DB 다운로드 (~300MB)
```

---

## 2. Fetcher 아키텍처 (3단계)

```
┌─────────────────────────────────────────────────────────────────┐
│                    Scrapling Fetcher 계층                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  Level 1: Fetcher / FetcherSession                               │
│  ├── curl_cffi 기반 HTTP 요청                                    │
│  ├── TLS 서명 모방 (impersonate='chrome'/'firefox135')           │
│  ├── HTTP/3 지원                                                 │
│  ├── stealthy_headers=True → 브라우저 헤더 순서 모방             │
│  └── 용도: 봇감지 없는 API/엔드포인트 고속 수집                   │
│                                                                   │
│  Level 2: DynamicFetcher / DynamicSession                        │
│  ├── Playwright Chromium 완전 브라우저 자동화                     │
│  ├── JS 렌더링, network_idle 대기                                │
│  ├── disable_resources (이미지/폰트 차단으로 속도↑)              │
│  └── 용도: JS 동적 로딩 페이지 (봇감지 약한 사이트)              │
│                                                                   │
│  Level 3: StealthyFetcher / StealthySession  ← 쿠팡 핵심        │
│  ├── Camoufox (패치된 Firefox) 엔진                              │
│  ├── C++ 레벨 핑거프린트 스푸핑 (JS 삽입 없음)                  │
│  ├── Cloudflare Turnstile 자동 해결 (solve_cloudflare=True)      │
│  ├── BrowserForge 기반 실세계 통계 일치 핑거프린트 생성           │
│  ├── 인간 마우스 이동 시뮬레이션 (C++ 구현)                      │
│  └── 용도: Akamai/Cloudflare 등 엔터프라이즈 봇감지 우회         │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. StealthySession 심층 분석 (쿠팡 핵심 기술)

### 3.1 내부 엔진: Camoufox

StealthySession/StealthyFetcher는 내부적으로 **Camoufox** (daijro/camoufox)를 사용한다.
Camoufox는 Firefox를 C++ 레벨에서 패치한 안티디텍트 브라우저:

| 우회 벡터 | Camoufox 처리 방식 |
|-----------|-------------------|
| navigator.webdriver | C++ 레벨 제거 → JS 탐지 불가 |
| Canvas/WebGL 핑거프린트 | C++ 레벨 렌더링 데이터 변조, JS에서 "native code"로 보임 |
| AudioContext | C++ 레벨 메트릭 변조 |
| Font 핑거프린팅 | OS별 시스템 폰트 번들 + 자간 랜덤 오프셋 |
| WebRTC IP 누출 | C++ 레벨 차단 |
| Geolocation | C++ 레벨 스푸핑 |
| Battery API | C++ 레벨 스푸핑 |
| Headless 탐지 | 포인터 타입 수정 + 가상 모니터 지원 |
| 자동화 스택 디버거 | Juggler 프로토콜 분리 문서 복제본 제공 |
| 마우스 이동 | C++ 재구현 인간 궤적 알고리즘 |

**핵심 장점**: JS 삽입(injection)이 아닌 C++ 구현 레벨 수정이므로,
사이트의 JS 무결성 검사에서 탐지되지 않는다.

### 3.2 BrowserForge 핑거프린트 생성

- 실세계 웹 트래픽 통계 분포에 기반한 핑거프린트 생성
- User-Agent, 화면 해상도, WebGL 렌더러, 플러그인 등이 내부 일관성 유지
- ML 기반 이상 탐지를 회피 (일관되지 않은 조합 방지)
- 세션마다 고유 프로필 자동 생성

### 3.3 StealthySession 주요 파라미터

```python
from scrapling.fetchers import StealthySession, StealthyFetcher

# 세션 방식 (여러 페이지 연속 수집 — 쿠팡에 적합)
with StealthySession(
    headless=True,              # 브라우저 창 숨김 (서버 환경)
    solve_cloudflare=True,      # Cloudflare 챌린지 자동 해결
) as session:
    page = session.fetch(
        url,
        google_search=False,    # 검색엔진 경유 비활성화
        network_idle=True,      # 모든 네트워크 요청 완료 대기
        adaptive=True,          # DOM 변경 자동 적응
    )

# 단발 요청 방식
page = StealthyFetcher.fetch(
    url,
    headless=True,
    solve_cloudflare=True,
    network_idle=True,
)
```

### 3.4 프록시 설정

> **rev.2 정정**: 파라미터명은 `proxies` 가 아니라 **`proxy`(단수)** 이며,
> dict 를 쓸 경우 requests 스타일(`{"http":..., "https":...}`)이 아니라
> **Playwright 스타일**(`{"server":..., "username":..., "password":...}`)이다.

```python
# 방법 1: 세션 레벨 프록시 (권장 — 세션 = IP 1개)
PROXY = "http://user:pass@kr-residential-proxy:port"      # str 형식
# 또는 Playwright 스타일 dict:
# PROXY = {"server": "http://kr-residential-proxy:port",
#          "username": "user", "password": "pass"}

with StealthySession(headless=True, proxy=PROXY) as session:
    page = session.fetch(url, network_idle=True)

# 방법 2: 요청별 오버라이드 (StealthFetchParams.proxy)
with StealthySession(headless=True) as session:
    page = session.fetch(url, proxy=PROXY)

# 방법 3: ProxyRotator — 세션마다 IP 교체 (Coupang 권장)
from scrapling.engines.toolbelt.proxy_rotation import ProxyRotator, cyclic_rotation

rotator = ProxyRotator(
    ["http://user:pass@proxy1:port", "http://user:pass@proxy2:port"],
    strategy=cyclic_rotation,          # 기본값, 커스텀 전략 주입 가능
)
with StealthySession(headless=True, proxy_rotator=rotator) as session:
    page = session.fetch(url, network_idle=True)
```

### 3.5 DNS 누출 방지

> **rev.2 정정**: 자동이 아니다. `dns_over_https` 를 **명시적으로 켜야** 한다.
> 끄고 프록시만 쓰면 DNS 질의가 로컬 리졸버로 새어나가 실제 위치가 드러난다.

```python
# DNS-over-HTTPS — 프록시 사용 시 DNS 누출 방지 (세션 파라미터)
with StealthySession(headless=True, proxy=PROXY, dns_over_https=True) as session:
    page = session.fetch(url, network_idle=True)
```

### 3.6 트래커/광고 차단

> **rev.2 정정**: `block_ads` 는 **세션 파라미터**다. `fetch()` 에 넘기면 무시된다.

```python
# 약 3,500개 알려진 트래커/광고 도메인 차단
# → 불필요한 네트워크 요청 감소 + 추적 방지 + 페이지 로드 가속
with StealthySession(headless=True, block_ads=True) as session:
    page = session.fetch(url, network_idle=True)
```

### 3.7 XHR 캡처 (rev.2 신규) ★ 사업자정보 수집의 핵심

초판에 누락된 기능. **세션** 파라미터 `capture_xhr` 에 URL 패턴을 주면,
페이지가 발생시킨 XHR/fetch 응답을 `Response.captured_xhr` 로 회수할 수 있다.

```python
with StealthySession(
    headless=True,
    proxy=PROXY,
    capture_xhr=r"...사업자정보 엔드포인트 패턴...",   # 스파이크에서 확정
    page_action=scroll_to_seller_section,            # XHR 을 유발
) as session:
    page = session.fetch(product_url, network_idle=True)
    for xhr in page.captured_xhr:        # list[Response]
        data = json.loads(xhr.body)      # 구조화된 사업자정보
```

**왜 중요한가.** 사업자정보는 상세페이지 최하단 사업자정보 란에 표시된다
(`PAGE_STRUCTURE.md` §4). 최하단 DOM을 직접 파싱하는 것이 1순위이나,
JS 렌더링 방식에 따라 XHR로 로드될 수도 있다.
XHR JSON은 구조가 안정적이고 필드가 이미 분리돼 있어 폴백으로 유효하다.
**본 프로젝트는 최하단 DOM 파싱을 1순위, XHR 캡처를 폴백으로 채택한다.**

근거: `_types.py` 의 `StealthSession.capture_xhr`,
`toolbelt/custom.py:81` 의 `Response.captured_xhr`.

---

## 4. 쿠팡 Akamai 우회 적용 분석

### 4.1 Akamai 5단계 감지 vs Scrapling 대응

| Akamai 감지 레이어 | Scrapling/Camoufox 대응 | 효과 |
|-------------------|------------------------|------|
| **L1: TLS/HTTP 핑거프린트** | Camoufox Firefox 실제 TLS 스택 사용 (모방 아님) | ✅ 완전 우회 |
| **L2: JS 챌린지 (_abck)** | 실제 브라우저에서 sensor 스크립트 실행, C++ 레벨 자동화 숨김 | ✅ 자동 처리 |
| **L3: 브라우저 핑거프린트** | C++ 레벨 Canvas/WebGL/Audio/Font 스푸핑, BrowserForge 일관성 | ✅ 완전 우회 |
| **L4: 행동 분석** | C++ 인간 마우스 궤적, network_idle 대기, 수동 지연 추가 필요 | ⚠️ 부분 (추가 코드 필요) |
| **L5: IP 평판** | 외부 한국 주거용 프록시 필수 (Scrapling 자체 IP 없음) | ⚠️ 프록시 의존 |

### 4.2 핵심 결론

> **Scrapling StealthySession은 Akamai L1~L3을 자동으로 처리하며,**
> **L4(행동)와 L5(IP)는 추가 구현이 필요하다.**

- L1~L3: Camoufox 엔진이 C++ 레벨에서 처리 → 별도 코드 불필요
- L4: 페이지 간 지연, 스크롤 시뮬레이션, 랜덤 대기를 코드에서 추가
- L5: 한국 주거용 프록시 (Bright Data, Oxylabs, Smartproxy) 연동 필수

### 4.3 기존 BYPASS_TECHNICAL_GUIDE.md 대비 장점

| 항목 | 기존 Selenium 3.9 + Chromium 108 | Scrapling StealthySession |
|------|----------------------------------|--------------------------|
| 자동화 탐지 | JS 삽입으로 webdriver 제거 (탐지 가능) | C++ 레벨 제거 (탐지 불가) |
| 핑거프린트 | 수동 설정, 일관성 보장 어려움 | BrowserForge 자동 일관성 |
| TLS 서명 | Chromium 108 실제 스택 | Firefox 실제 스택 (동일 효과) |
| _abck 쿠키 | 수동 5~8초 대기 + 상호작용 | 자동 처리 (브라우저 내 실행) |
| 유지보수 | 셀레니움 버전/드라이버 관리 | pip 업데이트 + scrapling install |
| 코드 복잡도 | ~200줄 (stealth 설정) | ~20줄 |
| 헤드리스 탐지 | 추가 패치 필요 | 내장 처리 |

---

## 5. 쿠팡 수집 구현 코드 (실전)

### 5.1 Phase 0: OMP 리스팅 스캔

```python
"""
Phase 0: OMP 리스팅에서 상품 ID + 판매자 닉네임 수집
StealthySession + 한국 주거용 프록시
"""
from scrapling.fetchers import StealthySession
import time
import random

> **rev.2 정정 요약**: `proxies=` → **세션 레벨 `proxy=`**,
> `time.sleep()` → **취소 반응형 `control.sleep()`**,
> 셀렉터는 **미검증**(`PAGE_STRUCTURE.md` §2) — 스파이크 확정 후 사용.
> 파워셀러 배지 추출도 여기서 함께 시도한다(비용 0).

```python
PROXY = "http://user:pass@kr-seoul-residential:port"   # 단수 · 세션 레벨

OMP_BASE = "https://www.coupang.com/np/omp?listSize=120&page={page}&sorter=bestAsc"

def scan_omp_listing(control, max_pages: int = 5) -> list[dict]:
    """OMP 리스팅 스캔 — 상품 ID, 판매자명, 파워셀러 배지 추출.

    control: 취소/일시정지 제어 (app.core.base.Control).
             쿨다운을 time.sleep 으로 하면 [취소]가 최대 15분 먹통이 된다.
    """
    products = []

    with StealthySession(
        headless=True,
        proxy=PROXY,              # ✅ 세션 레벨 단수 파라미터
        dns_over_https=True,      # ✅ 명시 지정 (자동 아님)
        block_ads=True,           # ✅ 세션 파라미터
        solve_cloudflare=True,
    ) as session:
        # 웜업: 홈페이지 방문 → _abck 센서(70KB) 실행 대기
        session.fetch("https://www.coupang.com/", network_idle=True)
        control.sleep(random.uniform(8, 15))     # ✅ 취소 반응형 분할 대기

        for page_num in range(1, max_pages + 1):
            control.checkpoint()                 # 건 경계 안전 중단
            page = session.fetch(
                OMP_BASE.format(page=page_num),
                network_idle=True,
                google_search=False,
            )

            # 차단 감지 (상태코드 → soft block → 키워드 순)
            if page.status in (403, 418, 429):
                print(f"[BLOCKED] page {page_num} — 세션 종료"); break
            if len(page.html_content) < 1000:
                print(f"[SOFT BLOCK] page {page_num} — 빈 응답"); break

            # ⚠ 아래 셀렉터는 전부 미검증 — 스파이크로 확정 후 사용
            for item in page.css("ul#productList > li[data-product-id]"):
                name_el   = item.css("div.name")
                seller_el = item.css("span.prod-sale-vendor-name")
                price_el  = item.css("strong.price-value")
                grade_el  = item.css("...파워셀러 배지 셀렉터 미확정...")

                products.append({
                    "product_id":   item.attrib.get("data-product-id", ""),
                    "title":        name_el[0].text if name_el else "",
                    "seller_name":  seller_el[0].text if seller_el else "",
                    "price":        price_el[0].text if price_el else "",
                    # rev.2: 배지 원문 보존 + 파생 불리언 (DATA_FIELDS_MAPPING §3.3)
                    "seller_grade": grade_el[0].text.strip() if grade_el else "",
                    "power_seller": bool(grade_el) and "파워셀러" in grade_el[0].text,
                })

            control.sleep(random.uniform(8, 15))  # 페이지 간 지연 (L4 우회)

    return products
```

> **다음 단계.** 이 결과를 그대로 상세 수집에 넘기지 않는다. 먼저
> `store_name` 을 정규화해 **판매자 단위로 축약**하고(D2), 축약된
> **모든** 판매자를 §5.2 상세 스크래핑으로 보낸다
> (`COLLECTION_STRATEGY.md` Stage C — rev.4: 공정위 연동이 제거되어
> "미매칭 판매자만"이라는 필터가 사라졌다. 전체 판매자가 대상이다).

### 5.2 판매자 상세 → 사업자정보 직접 스크래핑 (rev.4: 사업자정보 확보의 유일한 경로)

`Phase 1` 이라는 명칭은 rev.1의 잔재다 — 공정위 매칭이 없는 지금은 이
단계가 "폴백"이 아니라 **유일한 사업자정보 확보 경로**다.

사업자정보는 상세페이지 최하단 사업자정보 란에 표시된다.
JS 렌더링 여부에 따라 `network_idle` + 최하단 DOM 파싱 또는 `capture_xhr` 를 쓴다.

> **rev.2/4 정정 — 이 절은 초판에서 가장 많이 틀렸다.**
>
> 1. **대상이 잘못됐다.** `product_ids` 를 받으면 같은 판매자 상세를 수십 번
>    연다. 우리가 원하는 건 판매자 정보다 → **판매자당 대표 상품 1건**만
>    연다 (`COLLECTION_STRATEGY.md` Stage C).
> 2. **`.seller-company-name` 등은 출처 없는 추정 셀렉터다.**
>    `PAGE_STRUCTURE.md` §4 는 같은 정보를 "정적 HTML에 없음(AJAX)"이라고
>    기술해 서로 모순된다 → **XHR 캡처를 1순위**로 바꾼다.
> 3. `proxies=` → `proxy=`, `time.sleep()` → `control.sleep()`.
> 4. **rev.4: 공정위 폴백이 없다.** 여기서 실패하면 재시도 후 미확정으로
>    남긴다 — 대체 확보 경로가 존재하지 않는다.

```python
PRODUCT_URL = "https://www.coupang.com/vp/products/{pid}"

def enrich_sellers(control, targets: list["SellerTarget"], proxy_provider) -> None:
    """'판매자'의 사업자정보를 상세페이지에서 직접 수집 (상품이 아니라 판매자 단위).

    targets: Stage B(D2 축약)로 그룹화된 **모든** 판매자 + 그 대표 상품 1건.
    이 함수가 사업자정보를 얻는 유일한 경로다 — 외부 DB 폴백 없음.
    """
    for batch_no, batch in enumerate(chunked(targets, 5), start=1):
        control.checkpoint()

        if batch_no > 1:                                  # 배치 간 쿨다운
            control.sleep(random.uniform(600, 900))       # ✅ 취소 반응형

        with StealthySession(
            headless=True,
            proxy=proxy_provider.next(),                  # ✅ 세션마다 IP 교체
            capture_xhr=SELLER_XHR_PATTERN,               # ✅ 1순위 경로 (§3.7)
            page_action=scroll_to_seller_section,         # XHR 유발 (L4 우회)
            dns_over_https=True,
            block_ads=True,
            solve_cloudflare=True,
        ) as session:
            session.fetch("https://www.coupang.com/", network_idle=True)
            control.sleep(random.uniform(8, 15))          # _abck 웜업

            for target in batch:
                control.checkpoint()
                page = session.fetch(
                    PRODUCT_URL.format(pid=target.sample_product_id),
                    network_idle=True,
                )
                if page.status != 200 or len(page.html_content) < 1000:
                    continue

                # 1순위: XHR JSON  →  2순위: DOM 폴백 (셀렉터 스파이크 확정 후)
                profile = (parse_seller_from_xhr(page.captured_xhr)
                           or parse_seller_from_dom(page.html_content))
                if profile is None:
                    continue

                # 건 단위 durable 저장 — 크래시해도 재수집 최소화
                # rev.4: resolved/parse_method 만 기록 — "매칭 신뢰도" 개념 없음
                profile.resolved = bool(profile.business_number)
                sellers.upsert(target.seller_key, profile)
                sellers.save()

                control.sleep(random.uniform(5, 12))      # 상품 간 지연
```

**후속 처리.** 여기서 얻은 사업자번호는 곧바로 출력되지 않는다.
`normalize_brno()` + 체크섬 검증을 거친 뒤 **D3 동일 사업자번호 중복 제거**로
넘어간다 (`DATA_FIELDS_MAPPING.md` §3).

### 5.3 적응형 파싱 (셀렉터 변경 대응)

```python
"""
Scrapling 적응형 파싱: 쿠팡이 HTML 구조를 변경해도 자동 재탐색
"""
from scrapling.fetchers import StealthySession

with StealthySession(headless=True, proxy=PROXY) as session:   # ✅ 단수·세션 레벨
    page = session.fetch(url, network_idle=True)

    # 최초 실행: auto_save=True로 요소 특징 캐싱
    sellers = page.css(
        "span.prod-sale-vendor-name",
        auto_save=True,      # 요소의 텍스트/속성/위치 특징 저장
    )
    
    # 이후 실행: adaptive=True로 셀렉터 변경 시 자동 재탐색
    sellers = page.css(
        "span.prod-sale-vendor-name",
        adaptive=True,       # 저장된 특징 기반으로 요소 재탐색
    )
```

### 5.4 Spider 프레임워크 (대량 크롤링)

```python
"""
Scrapling Spider: 비동기 대규모 크롤링 + 체크포인트 재개
"""
from scrapling.spiders import Spider, Request

class CoupangOMPSpider(Spider):
    name = "coupang_omp"
    start_urls = ["https://www.coupang.com/np/omp?listSize=120&page=1"]
    concurrent_requests = 3          # 동시 요청 수 (낮게 유지)
    robots_txt_obey = False          # OMP는 robots.txt에서 비허용
    crawldir = "./crawl_data/coupang"  # 체크포인트 저장 (중단/재개)
    
    def setup_sessions(self):
        """세션 프로필 등록.

        rev.2 정정: 프록시 목록은 `proxies=[...]` 가 아니라
        `proxy_rotator=ProxyRotator([...])` 로 전달한다.
        """
        from scrapling.engines.toolbelt.proxy_rotation import ProxyRotator

        self.add_session(
            sid="stealth",
            session_type="stealthy",
            headless=True,
            solve_cloudflare=True,
            dns_over_https=True,
            proxy_rotator=ProxyRotator([
                "http://user:pass@kr-proxy-1:port",
                "http://user:pass@kr-proxy-2:port",
                "http://user:pass@kr-proxy-3:port",
            ]),
        )
    
    def parse(self, response):
        """리스팅 파싱 → 다음 페이지 + 상품 상세"""
        items = response.css("ul#productList > li[data-product-id]")
        for item in items:
            pid = item.attrib.get("data-product-id")
            yield Request(
                f"https://www.coupang.com/vp/products/{pid}",
                callback=self.parse_product,
                sid="stealth",
            )
        
        # 다음 페이지
        next_page = response.css("a.next-page::attr(href)").get()
        if next_page:
            yield Request(next_page, callback=self.parse, sid="stealth")
    
    def parse_product(self, response):
        """상품 상세 → 판매자 정보 추출"""
        yield {
            "product_id": response.url.split("/")[-1],
            "seller_name": response.css(".prod-sale-vendor-name::text").get(""),
            "title": response.css("h1.prod-buy-header__title::text").get(""),
        }
```

### 5.5 비동기 세션 (고성능)

```python
"""
AsyncStealthySession: 비동기 병렬 수집
"""
import asyncio
from scrapling.fetchers import AsyncStealthySession

async def collect_batch(urls: list[str], proxy: str) -> list:
    results = []
    async with AsyncStealthySession(headless=True, max_pages=5, proxy=proxy) as session:
        for url in urls:
            page = await session.fetch(url, network_idle=True)   # ✅ proxy 는 세션 레벨
            if page.status == 200:
                results.append(page)
            await asyncio.sleep(5)
    return results
```

> **rev.2 — 본 프로젝트는 이 방식을 채택하지 않는다.**
> 병렬 요청은 Akamai 행동 분석에 정확히 반대되는 신호다(사람은 페이지 5개를
> 동시에 열지 않는다). Coupang 문제의 해법은 동시성이 아니라 **지연과 세션
> 회전**이다. `max_pages` 를 올릴수록 차단 위험이 커진다.

---

## 6. Fetcher / FetcherSession (Level 1) 활용

> **rev.4 — 이 절의 원래 용도(공정위 API 조회)는 제거됐다.** 필수 수집
> 항목은 Coupang 자체 상세페이지에서 직접 확보하므로(§5.2) 봇감지 없는
> 외부 API를 별도로 호출할 필요가 없다. `FetcherSession`(Level 1)은
> 본 프로젝트의 Coupang 수집 경로에서는 **사용하지 않는다** — Akamai가
> 전 사이트를 보호하므로 TLS 모방만으로는 L2(`_abck`)를 통과할 수 없고,
> 결국 모든 Coupang 요청은 `StealthySession`(Level 3)을 거쳐야 한다.
> API 레퍼런스로서 참고용으로만 남겨둔다.

### 6.1 TLS 임퍼소네이트 옵션 (참고용)

```python
# 사용 가능한 impersonate 값:
# 'chrome', 'chrome110', 'chrome116', 'chrome120', 'chrome124'
# 'firefox', 'firefox135'
# 'safari', 'edge'
# → 쿠팡에는 실제 Firefox 사용하는 StealthySession 필수 (L1 으로는 L2 통과 불가)
```

---

## 7. DynamicSession (Level 2) 활용

### 7.1 JS 동적 로딩 콘텐츠 (봇감지 약한 페이지)

```python
"""
쿠팡 상품 상세 최하단 사업자정보 란 렌더링에 사용 가능
(StealthySession이 과한 경우 대안)
"""
from scrapling.fetchers import DynamicSession

with DynamicSession(
    headless=True,
    disable_resources=False,   # JS/CSS 로드 허용 (동적 콘텐츠 필요)
    network_idle=True,         # 모든 XHR 완료 대기
) as session:
    page = session.fetch(url, load_dom=True)
    # JS 렌더링 완료 후 DOM 접근
```

---

## 8. 쿠팡 특화 행동 시뮬레이션 (L4 우회)

Scrapling/Camoufox가 마우스 이동을 C++에서 처리하지만,
쿠팡의 Akamai는 **페이지 간 내비게이션 패턴**도 분석하므로 추가 코드 필요:

```python
"""
행동 시뮬레이션 유틸리티 — Akamai L4 우회
"""
import time
import random

> **rev.2 정정 — `time.sleep()` 을 쓰면 안 된다.**
>
> 배치 쿨다운은 600~900초, 차단 복구는 900~1800초다. `time.sleep(900)` 을
> 그대로 쓰면 **[취소] 버튼이 최대 30분간 먹통**이 되고, 창을 닫을 때
> `closeEvent` 가 좀비 스레드를 기다리며 UI가 얼어붙는다.
> 기존 코드베이스의 `Prescanner._wait_blocked`(`app/core/prescan.py:145`)가
> 이미 쓰는 분할 대기 패턴을 `Control.sleep()` 으로 일반화해 사용한다.
> 또한 남은 대기 시간을 UI에 표시해야 한다 — 15분간 멈춘 것처럼 보이면
> 사용자는 앱이 죽은 줄 안다.

```python
class BehaviorSimulator:
    """쿠팡 Akamai 행동 분석(L4) 우회를 위한 지연/패턴 관리.

    모든 대기는 Control 을 통해 분할 수행한다 — 취소/일시정지 즉시 반응 +
    남은 시간 UI 통지(on_tick).
    """

    def __init__(self, control, on_wait=None):
        self.control = control          # app.core.base.Control
        self.on_wait = on_wait          # (remaining_sec, reason) -> None

    def _wait(self, lo: float, hi: float, reason: str) -> None:
        secs = random.uniform(lo, hi)
        self.control.sleep(
            secs, on_tick=lambda rem: self.on_wait and self.on_wait(rem, reason)
        )

    def warmup_delay(self):  self._wait(8, 15, "_abck 센서 실행 대기")
    def page_delay(self):    self._wait(8, 15, "페이지 간 지연")
    def item_delay(self):    self._wait(5, 12, "상품 간 지연")
    def batch_cooldown(self):  self._wait(600, 900, "배치 쿨다운")
    def block_recovery(self):  self._wait(900, 1800, "차단 복구 대기")


# 예산/중단 판정은 별도 모듈로 분리한다 — 메모리 카운터로는 앱 재시작 시
# 하드캡이 리셋되어 무의미해지므로 디스크에 영속화한다(coupang_budget.json).
class DailyBudget:
    def listing_exhausted(self) -> bool: ...   # 일일 리스팅 요청 상한
    def detail_exhausted(self) -> bool: ...    # 일일 상세 요청 상한
    def mark_block(self) -> None: ...          # 차단 시각 기록 → 재시작 후에도 복구 대기 유지
```

> **rev.5 보강 — `warmup_delay()` 만으로는 부족하다.**
>
> Akamai 센서(70KB)는 **대기 시간이 아니라 실제 상호작용 이벤트**(마우스 이동,
> 키스트로크, 스크롤, 클릭)를 수집한다. `warmup_delay()` 가 8~15초를 기다려도
> 그 사이에 상호작용이 0건이면 센서 텔레메트리가 비어 "headless bot" 시그니처가
> 된다. **홈 fetch 직후 `warmup_interact(page, control)` 를 호출해 마우스 이동 +
> 키보드 스크롤 + 타이핑 + 클릭을 주입해야 한다.**
>
> 전체 구현: `BYPASS_TECHNICAL_GUIDE.md` §11.
> 적용 순서: `session.fetch(HOME_URL)` → `warmup_interact()` → `warmup_delay()`.
>
> 또한 `batch_cooldown()` 은 rev.5부터 `AdaptiveBackoff.cooldown_multiplier` 를
> 참조해 차단 이력에 따라 대기 시간을 동적으로 조정한다
> (`BYPASS_TECHNICAL_GUIDE.md` §14).

### 내비게이션 패턴 (자연스러운 유입 시뮬레이션)

```python
"""
직접 URL 접근 대신 자연스러운 유입 체인 시뮬레이션
Google → 쿠팡 홈 → 카테고리 → 상품 (참조 체인)
"""
def natural_navigation(session, control, target_url: str):
    """자연스러운 내비게이션 체인.

    rev.2: proxy 는 세션 생성 시 지정하므로 fetch 인자에서 제거.
           대기는 control.sleep 으로 — 취소 반응성 확보.
    """
    # Step 1: 쿠팡 홈페이지 (Referer 없음) — _abck 센서 실행
    session.fetch("https://www.coupang.com/", network_idle=True)
    control.sleep(random.uniform(3, 6))

    # Step 2: 카테고리 경유 (robots.txt 허용 경로)
    session.fetch("https://www.coupang.com/np/categories/186764", network_idle=True)
    control.sleep(random.uniform(3, 5))

    # Step 3: 실제 타겟 페이지 — 앞 두 단계가 Referer 체인을 만든다
    page = session.fetch(target_url, network_idle=True)
    return page
```

---

## 9. 차단 감지 및 복구

```python
"""
차단 감지 로직 — BYPASS_TECHNICAL_GUIDE.md 기준
"""
from dataclasses import dataclass

@dataclass
class BlockDetector:
    block_count: int = 0
    
    def check(self, page) -> bool:
        """True = 차단됨"""
        # HTTP 상태 코드
        if page.status in (403, 418, 429):
            self.block_count += 1
            return True
        
        # 소프트 블록 (빈 200 응답)
        if page.status == 200 and len(page.html_content) < 1000:
            self.block_count += 1
            return True
        
        # 한국어 차단 키워드
        block_keywords = ["접근이 제한", "봇", "자동", "인증이 필요"]
        content = page.html_content[:5000]
        if any(kw in content for kw in block_keywords):
            self.block_count += 1
            return True
        
        return False
    
    def should_abort(self) -> bool:
        """2회 차단 시 세션 즉시 종료"""
        return self.block_count >= 2
    
    def recovery_cooldown(self, control):
        """차단 후 복구 대기 (15~30분). rev.5: AD-6 취소 반응형 분할 대기."""
        cooldown = random.uniform(900, 1800)
        control.sleep(cooldown, on_tick=lambda rem: print(f"  [복구 대기] {rem:.0f}초"))
        self.block_count = 0
```

---

## 10. Scrapling 파싱 API 레퍼런스

### CSS / XPath 선택

```python
# CSS 선택자
items = page.css("ul#productList > li")
texts = page.css("div.name::text").getall()
first = page.css("strong.price-value::text").get()

# XPath
items = page.xpath("//ul[@id='productList']/li")

# BeautifulSoup 스타일
items = page.find_all("li", {"data-product-id": True})
```

### DOM 탐색

```python
element = page.css("span.prod-sale-vendor-name")[0]
parent = element.parent
sibling = element.next_sibling
similar = element.find_similar()  # 동일 구조 반복 요소 탐색
below = element.below_elements()
```

### 속성/텍스트 추출

```python
el = page.css("li[data-product-id]")[0]
pid = el.attrib["data-product-id"]
text = el.text
html = el.html_content

# 정규식 추출
price = page.css(".price").re_first(r'[\d,]+')
```

### 적응형 파싱

```python
# 최초: 요소 특징 저장
els = page.css(".seller-name", auto_save=True)

# 이후: 셀렉터 변경 시 자동 재탐색
els = page.css(".seller-name", adaptive=True)
# → 클래스명이 .vendor-name으로 바뀌어도 자동 매칭
```

---

## 11. 기존 프로젝트 통합 설계

### 11.1 모듈 매핑 (IMPLEMENTATION_ARCHITECTURE.md 기반)

> **rev.2 갱신**: 확정 모듈 구조는 `IMPLEMENTATION_PLAN.md` §8 을 따른다.
> 초판의 평면 배치(`app/core/coupang_*.py`)는 `app/core/coupang/` 패키지로
> 바꾼다 — 모듈이 13개까지 늘어나 `app/core/` 최상위가 Gmarket 모듈과
> 뒤섞이면 읽기 어렵다.

> **rev.4 갱신**: 공정위(FTC) 연동 전면 제거. `ftc_db.py`/`ftc_matcher.py`/
> `ftc_build_worker.py` 삭제. Stage 수가 5개(A~E)에서 4개(A~D)로
> 단순화됐다 — 리스팅/축약/상세스크래핑/집계출력.

```
app/core/coupang/
├── config.py       ← Scrapling 세션 설정, 프록시, 타이밍 상수
├── budget.py       ← 일일 예산 영속화 (앱 재시작에도 하드캡 유지)
├── block.py        ← 차단 감지 (403/418/429 + soft block + 키워드)
├── behavior.py     ← page_action 콜백 + 지연 프로파일 (L4 우회)
├── proxy.py        ← 프록시 공급자 (ProxyRotator 래퍼, L5)
├── parser.py       ← 리스팅 파서 + 판매자 상세 파서(XHR/DOM, 순수 함수)
├── listing.py      ← Stage A: OMP 리스팅 스캔
├── sellers.py      ← 판매자 프로필 캐시 (D2 축약의 저장소)
├── enrich.py       ← Stage C: 판매자 상세 직접 스크래핑 (사업자정보 유일 경로)
├── dedup.py        ← D3 사업자번호 정규화·검증·병합
└── pipeline.py     ← Stage 오케스트레이션 + Stage D (D3 중복 제거/출력)

app/workers/coupang_scan_worker.py    ← Stage A
app/workers/coupang_enrich_worker.py  ← Stage B+C
```

**AsyncStealthySession 은 쓰지 않는다.** 병렬 요청은 Akamai 행동 분석에
정확히 반대되는 신호다(사람은 5개 페이지를 동시에 열지 않는다). 동시성이
아니라 **지연과 세션 회전**이 이 문제의 해법이다.

### 11.2 세션 전략

| Stage | Fetcher 레벨 | 이유 |
|-------|-------------|------|
| A. OMP 리스팅 | StealthySession (L3) | Akamai 전면 적용, JS 렌더링 필요 |
| B. 판매자 축약(D2) | **없음 (로컬)** | 네트워크 요청 0 — 비용 0 |
| **C. 판매자 상세 스크래핑** | **StealthySession (L3) + 최하단 DOM 파싱** | 최하단 사업자정보 란 + Akamai — **사업자정보를 얻는 유일한 경로** |
| D. 집계/출력 | **없음 (로컬)** | D3 중복 제거 후 저장 |

**핵심 (rev.4)**: 4개 Stage 중 **프록시가 필요한 것은 A와 C 둘뿐**이지만,
**이 둘 없이는 파이프라인이 전혀 동작하지 않는다** — B와 D는 A/C의
산출물을 가공만 할 뿐 자체 데이터 소스가 없다. rev.2가 주장했던
"프록시 없이도 일부 Stage는 동작한다"는 더 이상 성립하지 않는다.

### 11.3 의존성

```
scrapling[fetchers]>=0.4.11    # 이미 포함됨 — 이 이상 추가 의존성 없음
# scrapling install --force    # 브라우저 + 핑거프린트 DB
```

> **rev.4**: `thefuzz`, `python-Levenshtein` 제거 — 퍼지 매칭 자체가 없다.

---

## 12. 한계점 및 리스크

| 한계 | 영향 | 완화책 |
|------|------|--------|
| StealthySession = Firefox 엔진 | 쿠팡이 Firefox 트래픽 적어 이상 탐지 가능 | BrowserForge가 실세계 비율 반영, Chrome impersonate 대안 |
| Akamai L4 행동 분석 | 자동 마우스만으로 부족할 수 있음 | BehaviorSimulator 추가 지연/패턴 |
| 세션당 수집량 제한 | 5건/배치, 1000건/일 | 다중 프록시 + 시간 분산 |
| _abck 쿠키 세션 귀속 | 쿠키 재사용 불가 | 매 세션 신규 생성 (자동) |
| 쿠팡 셀렉터 변경 | 수집 실패 | adaptive=True 자동 적응, XHR 우선 |
| **상세 스크래핑 실패 시 대체 경로 없음** | **커버리지 손실 직결 (rev.4)** | 재시도 + XHR/DOM 이중화 — 외부 DB 폴백 없음 |
| 프록시 비용 | 월 10~30만원 | 판매자 단위 축약(D2)으로 상세 요청 수 자체를 절감 |
| Turnstile hang | 임베디드 위젯 시 무한 대기 가능 | wait_selector 또는 DynamicSession 폴백 |
| **핑거프린트 교차 불일치 (rev.5)** | 한국 프록시 + 외산 GPU/해상도 → Akamai flag | 스파이크에서 browserleaks 검증 (`BYPASS_TECHNICAL_GUIDE.md` §15) |
| **HTTP/2 프레임 시그니처 (rev.5)** | Camoufox HTTP/2가 Firefox와 다를 수 있음 | mitmproxy 캡처 비교, 불일치 시 `real_chrome=True` (§15) |
| **세션 간 메타패턴 상관 (rev.5)** | 타이밍/파라미터 정규성으로 배치 탐지 | SessionRandomizer — 배치·정렬·타이밍 랜덤화 (§12) |
| **프록시 풀 오염 (rev.5)** | flagged IP 할당 시 세션 즉시 차단 | canary 검증 + blacklist (`BYPASS_TECHNICAL_GUIDE.md` §13) |

### Firefox vs Chrome 선택 고려

```
쿠팡 트래픽 분석:
- Chrome: ~70% (주류)
- Firefox: ~5% (소수)
- Safari: ~15%
- 기타: ~10%

리스크: Firefox 사용 시 소수 브라우저로 분류되어 추가 검증 가능
대안: DynamicSession(Playwright Chromium) + 수동 stealth 설정
      → 그러나 C++ 레벨 스푸핑 부재로 탐지 리스크 증가

결론: Camoufox(Firefox)의 C++ 스푸핑 > Chromium의 JS 기반 스푸핑
      BrowserForge가 Firefox 실세계 비율로 생성하므로 이상치 아님
```

---

## 13. 권장 수집 파이프라인 (rev.4 최종)

```
┌──────────────────────────────────────────────────────────────┐
│              Coupang 수집 파이프라인 (rev.4)                   │
├──────────────────────────────────────────────────────────────┤
│                                                                │
│  [Stage A] OMP 리스팅 스캔 (StealthySession L3 + 프록시)     │
│  ├── 홈 웜업(8~15초) → robots 허용 경로 우선 접근            │
│  ├── product_id + store_name + 파워셀러 배지 추출            │
│  └── 페이지 간 8~15초, 일일 예산 차감                        │
│                                                                │
│  [Stage B] 판매자 축약 (로컬 · 무료)                         │
│  ├── D2: store_name → seller_key 축약  ★고비용 진입 전       │
│  │       수천 상품 → 수백 판매자                             │
│  └── 기확보 사업자 즉시 스킵 (business_index 역인덱스)       │
│                                                                │
│  [Stage C] 판매자 상세 직접 스크래핑 (StealthySession L3 + 프록시) │ ★ 유일 경로
│  ├── 대상: Stage B 로 축약된 **모든** 판매자                 │
│  ├── capture_xhr 로 사업자정보 JSON 회수 (DOM 은 폴백)       │
│  ├── 배치 5건/세션, 600~900초 쿨다운 (취소 반응형 분할 대기) │
│  ├── 실패 시 재시도 → 그래도 실패면 미확정 (대체 경로 없음)  │
│  └── 차단 2회 시 즉시 중단 + 15~30분 복구 대기               │
│                                                                │
│  [Stage D] 사업자 집계 + 출력 (로컬)                         │
│  ├── D3: 동일 사업자번호 중복 제거 ★                         │
│  │       10자리 정규화 + 국세청 체크섬 → 대표 선택 → 병합    │
│  │       power_seller = OR, store_names/count 집계           │
│  └── coupang_business_{ts}   ← 사업자번호 유일               │
│      coupang_unresolved_{ts} ← 상세 실패 판매자 (분리 보관)  │
│                                                                │
└──────────────────────────────────────────────────────────────┘
```

**개정 이력 요약**

| 항목 | rev.1 | rev.2/3 | **rev.4 (최종)** |
|------|-------|---------|--------------------|
| 사업자정보 확보 | 상품 단위 상세 방문 | 공정위 매칭 우선, 미매칭만 상세 | **상세 스크래핑이 유일 경로, 전 판매자 대상** |
| 외부 DB | (미도입) | 공정위 SQLite+FTS5 | **없음** |
| 상세 수집 대상 | 상품 전체 | 미매칭 판매자만 | **축약된 전 판매자** |
| 사업자정보 취득 | 팝업 DOM 파싱 (오인) | XHR 캡처 1순위, DOM 폴백 | **최하단 DOM 파싱 1순위, XHR 폴백** (유일 경로) |
| 중복 제거 | product_id | + seller_key + business_number | 동일 |
| 출력 단위 | 상품 22필드 | 사업자 1행 + 상품 원본 별도 | 동일 |
| 대기 구현 | `time.sleep` | `Control.sleep` | 동일 |
| 프록시 의존도 | 부분 | 부분 (A+C+E는 무관) | **전체 — 프록시 없이는 무의미** |

---

## 14. 참고 자료

- Scrapling GitHub: https://github.com/D4Vinci/Scrapling
- Camoufox GitHub: https://github.com/daijro/camoufox
- Camoufox Stealth 문서: https://camoufox.com/stealth/
- Scrapling ReadTheDocs: https://scrapling.readthedocs.io/en/latest/
- Anti-detect Browser 비교: https://github.com/pim97/anti-detect-browser-tools-tech-comparison
- Akamai 우회 가이드 (Bright Data): https://brightdata.com/blog/web-data/bypass-akamai-bot-detection
- Akamai 우회 가이드 (Scrapfly): https://scrapfly.io/blog/posts/how-to-bypass-akamai-anti-scraping
- Scrapling 튜토리얼: https://medium.com/@datajournal/web-scraping-with-scrapling-2025-tutorial-ae6c7fb1dd81
- Scrapling 매뉴얼: https://doramagic.ai/en/projects/scrapling/manual/
- 쿠팡 수집 엔터프라이즈 가이드: https://kndusc.com/blogs/coupang-data-scraping-enterprise-guide/
