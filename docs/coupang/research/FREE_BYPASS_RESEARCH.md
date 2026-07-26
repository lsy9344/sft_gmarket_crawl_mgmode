> **⚠ 아카이브됨 — 구현 전 연구 (rev.1~5).** 이 문서는 구현 *이전*의 연구/설계
> 문서로, 실측 결과 여러 전제가 폐기됐다. 현재 기준은 **`../CRAWL_RESULTS.md`** 가
> 우선한다. 역사적 참고용(안티봇 우회 연구 등)으로 보관한다.

# Coupang OMP - 무료 우회 방법 심층 조사

> 조사일: 2026-07-25
> 목적: 프록시 비용(10~30만원/월) 없이 쿠팡 상품 상세페이지에서
> 사업자정보를 수집할 수 있는 무료 방법 연구
>
> **기존 문서의 결론**: "프록시 없이는 아무 것도 수집 못함"
> (`COLLECTION_STRATEGY.md`, `DATA_FIELDS_MAPPING.md`)
>
> **본 조사의 결론**: **조건부 가능.** 완전 무료 경로가 존재하나,
> 처리량·안정성·전제조건에서 기존 유료 프록시 경로와 트레이드오프가 있다.

---

## 0. 핵심 발견 요약

| # | 방법 | 비용 | 일일 처리량 | 안정성 | 전제조건 |
|---|------|------|-----------|--------|---------|
| **1** | **한국 가정용 IP + StealthySession** | **0원** | **50~150건** | **중~상** | **한국 가정/사무실 인터넷** |
| **2** | **공유기 재부팅 IP 회전** | **0원** | **100~300건** | **중** | **동적 IP 할당 ISP** |
| **3** | **Selenium 3.9 + Chromium 108 (HTTP/1.1)** | **0원** | **200~500건** | **중~상** | **한국 IP + 구버전 Chromium** |
| **4** | **수동 브라우저 쿠키 추출 + 재사용** | **0원** | **30~80건** | **하~중** | **수동 개입 필요** |
| **5** | **브라우저 확장 프로그램 (실브라우저)** | **0원** | **20~50건** | **상** | **수동/반자동 브라우징** |
| **6** | **requests + 헤더 (리스팅 전용)** | **0원** | **500~1000건** | **중** | **Stage A에만 적용** |
| 7 | 유료 주거용 프록시 (기존 방안) | 10~30만원/월 | 500~1000건 | 상 | 예산 |

**결론: 방법 1~3을 조합하면 프록시 비용 0원으로 파이프라인 운용이 가능하다.**
단, 일일 처리량이 유료 경로 대비 1/3~1/5로 감소한다.

---

## 1. 기존 문서의 전제 재검증

### 1.1 "프록시 없이는 불가"라는 결론의 근거

기존 문서(`PLATFORM_ANALYSIS.md`)의 Access Test Results:

| Method | Result |
|--------|--------|
| `requests` (no headers) | 403 Forbidden |
| `requests` + Chrome UA + Accept-Language | 403 or empty |
| Selenium/Playwright (default) | Blocked within 5 min |

### 1.2 이 테스트의 한계

**위 테스트는 "기본 설정"에서의 결과다.** 아래 조건이 빠져 있다:

1. **한국 가정용 IP에서의 테스트 여부 불명** — 데이터센터/해외 IP에서
   테스트했다면 "한국 주거용 IP 필수" 조건과 모순
2. **헤더 순서·완전성** — `Accept-Language`만 추가하고 `Sec-Fetch-*`,
   `Accept-Encoding`, 헤더 순서 등을 누락했을 가능성
3. **Selenium 3.9 + Chromium 108 조합 미테스트** — HTTP/2 탐지 회피의
   핵심인 구버전 조합
4. **행동 시뮬레이션 없이 테스트** — 마우스/키보드 이벤트 0건이면
   Akamai L4에서 즉시 bot 판정
5. **웜업 없이 직접 URL 접근** — Referrer 체인 없이 직접 접근

### 1.3 외부 성공 사례 (프록시 없이)

| 출처 | 주장 | 신뢰도 |
|------|------|--------|
| cosmowifi.tistory.com (2025) | "24시간 900~1000개 안정 수집", 프록시/VPN 오히려 차단 유발 | **높음** (구체적 수치 + 기술 스택 명시) |
| velog.io (chltpdus48) | `requests` + `fake_useragent` + `Accept-Language`로 리스팅 성공 | **중** (리스팅만, 상세 미확인) |
| iamgus.tistory.com | 헤더 설정만으로 정상 접속, 프록시 불필요 | **중** (단순 페이지 접근) |
| hashscraper.com (2026) | Playwright+Stealth+Headed: 30~60% 성공률 (프록시 없이) | **중** (성공률 낮음 인정) |

**핵심 통찰**: cosmowifi 글은 **"프록시나 VPN 사용은 오히려 차단을
유발한다"** 고 명시한다. 한국 가정용 IP는 이미 "주거용 IP"이므로,
유료 프록시를 쓸 이유가 없다는 것이다.

---

## 2. 무료 우회 방법 상세

### 방법 1: 한국 가정용 IP + StealthySession (추천 1순위)

#### 원리

한국 가정/사무실 인터넷(KT, SK브로드밴드, LG U+)의 공인 IP는
**이미 주거용(residential) IP**다. Akamai L5(IP 평판)에서 이 IP들은
"정상 사용자"로 분류된다. 유료 프록시가 하는 일은 결국 "한국 주거용
IP를 빌려주는 것"인데, **한국에 있으면 이미 가지고 있다.**

#### 왜 기존 문서가 이걸 놓쳤나

- "프록시 필수"는 **해외/데이터센터 IP 기준**으로 쓴 결론
- 한국 로컬 환경에서 테스트하지 않았을 가능성
- "주거용 프록시 = 유료 서비스"라는 전제가 잘못됨

#### 구현 전략

```
[기존 유료 경로]
StealthySession + 유료 한국 주거용 프록시 (10~30만원/월)
→ 500~1000건/일, 0% 차단율

[무료 경로]
StealthySession + 로컬 한국 가정용 IP (0원)
→ 50~150건/일, 낮은 차단율 (보수적 설정 시)
```

#### 보수적 세션 설정 (무료 IP용)

```python
FREE_SESSION_CONFIG = {
    "max_items_per_session": 3,        # 유료: 5 → 무료: 3 (더 보수적)
    "session_cooldown": (900, 1800),   # 유료: 600~900 → 무료: 900~1800초
    "item_delay": (10, 25),            # 유료: 5~15 → 무료: 10~25초
    "warmup_wait": (15, 30),           # 유료: 8~15 → 무료: 15~30초
    "max_daily_items": 150,            # 유료: 1000 → 무료: 150 (하드캡)
    "abort_after_blocks": 1,           # 유료: 2 → 무료: 1 (즉시 중단)
    "daily_sessions": 30~50,           # 3건 × 50세션 = 150건
}
```

#### 처리량 산정

```
세션당 3건 × (웜업 20초 + 항목당 18초 + 쿨다운 1350초 상각)
= 세션당 약 25분
하루 24시간 ÷ 25분 = 약 57세션 (이론 최대)
보수적으로 30~50세션 → 90~150건/일
```

#### 장점
- **비용 0원**
- Scrapling/Camoufox는 오픈소스 (이미 프로젝트에 `scrapling[fetchers]>=0.4.11` 존재)
- 한국 IP이므로 Akamai L5 자동 통과
- C++ 레벨 스푸핑으로 L1~L3 자동 통과
- 행동 시뮬레이션으로 L4 통과

#### 리스크
- 같은 IP에서 장기간 수집 시 **IP 평판 하락** 가능
- 차단 시 해당 IP가 일시적으로 flag됨 (공유기 재부팅으로 해결 — 방법 2)
- 처리량이 유료 대비 1/5~1/7

---

### 방법 2: 공유기 재부팅을 통한 무료 IP 회전

#### 원리

한국 ISP(KT, SK, LG U+)는 대부분 **동적 IP(DHCP)** 를 할당한다.
공유기를 재부팅하면 새로운 공인 IP를 받는다. 이는 유료 프록시의
"IP 회전"과 동일한 효과를 **무료**로 제공한다.

#### 구현

```python
import subprocess
import time

def rotate_ip_via_router():
    """공유기 재부팅으로 IP 회전 (무료).
    
    방법 A: 공유기 관리자 API (대부분의 공유기가 HTTP API 제공)
    방법 B: 네트워크 인터페이스 재시작 (PC 직접 연결 시)
    방법 C: 물리적 재부팅 (스마트플러그로 원격 제어 가능)
    """
    # 방법 B: Linux/WSL에서 네트워크 재시작
    # (공유기 DHCP에서 새 IP 받기)
    subprocess.run(["sudo", "dhclient", "-r"], check=True)  # IP 해제
    time.sleep(5)
    subprocess.run(["sudo", "dhclient"], check=True)        # 새 IP 요청
    
def rotate_ip_via_router_api(router_ip="192.168.0.1", admin_pw=""):
    """공유기 관리자 페이지 API로 재부팅.
    
    iptime: http://192.168.0.1/sess-bin/login_handler.cgi
    SK: http://192.168.25.1/
    LG: http://192.168.219.1/
    """
    import requests
    # iptime 예시
    requests.post(
        f"http://{router_ip}/sess-bin/login_handler.cgi",
        data={"username": "admin", "passwd": admin_pw},
    )
    requests.get(f"http://{router_ip}/reboot.cgi")
```

#### 전략

```
[수집 50건] → [IP flag 감지 또는 예방적 회전] → [공유기 재부팅]
→ [새 IP로 50건] → [반복]

하루 3~6회 IP 회전 × 50건 = 150~300건/일
```

#### 주의사항
- ISP에 따라 같은 대역의 IP를 재할당할 수 있음 (완전 랜덤 아님)
- 재부팅 후 IP 할당까지 30초~2분 대기
- 공유기 재부팅 시 다른 가족의 인터넷도 끊김 (심야 시간대 활용)
- **스마트플러그(5,000~10,000원)** 로 원격 재부팅 자동화 가능

#### 비용
- **0원** (이미 공유기 보유 가정)
- 스마트플러그: 5,000~10,000원 (1회, 선택)

---

### 방법 3: Selenium 3.9 + Chromium 108 (HTTP/1.1 강제)

#### 원리

cosmowifi.tistory.com에서 검증된 방법. 핵심 통찰:

1. **Selenium 4.x는 HTTP/2를 사용** → Akamai가 HTTP/2 프레임
   시그니처로 자동화 트래픽 탐지
2. **Selenium 3.9 + Chromium 108은 HTTP/1.1을 사용** → HTTP/2
   프레임 탐지 자체가 불가
3. Chromium 108은 **자동화 탐지 마커가 적음** (최신 Chrome의
   `navigator.webdriver` 등의 탐지 포인트가 다름)

#### 왜 이것이 무료 우회인가

- Selenium 3.9: 무료 오픈소스
- Chromium 108: 무료 (구버전 다운로드 가능)
- 한국 가정용 IP: 무료
- **유료 프록시 불필요** — cosmowifi는 "프록시가 오히려 차단 유발"이라 명시

#### 기술 스택

```
Selenium 3.9.x (pip install selenium==3.9.0)
+ Chromium 108.x (구버전 바이너리)
+ chromedriver 108.x (매칭 버전)
+ 서브프로세스 브라우저启动 (포트 제어)
+ HTML 통짜 다운로드 → 로컬 파싱
+ 키보드 기반 스크롤 (ActionChains)
+ 자연스러운 Referrer 체인 (Google → Coupang → 상품)
```

#### 핵심 기법 (cosmowifi 검증)

```python
# 1. Chromium 108 서브프로세스 启动
import subprocess
chrome_process = subprocess.Popen([
    "/path/to/chromium-108/chrome",
    "--remote-debugging-port=9222",
    "--disable-blink-features=AutomationControlled",
    "--user-data-dir=/tmp/chrome_profile",
    "--no-first-run",
])

# 2. Selenium 3.9로 연결
from selenium import webdriver
options = webdriver.ChromeOptions()
options.debugger_address = "127.0.0.1:9222"
driver = webdriver.Chrome(options=options)

# 3. 자연스러운 유입 체인
driver.get("https://www.google.com")
time.sleep(random.uniform(2, 4))
driver.get("https://www.coupang.com")
time.sleep(random.uniform(5, 10))  # _abck 센서 실행 대기
driver.get(target_product_url)

# 4. HTML 통짜 다운로드 → 로컬 파싱
html = driver.page_source
soup = BeautifulSoup(html, "html.parser")
for script in soup.find_all("script"):
    script.decompose()
# 사업자정보 섹션 파싱...

# 5. 키보드 기반 스크롤 (JS scrollTo 탐지 회피)
from selenium.webdriver.common.keys import Keys
body = driver.find_element_by_tag_name("body")
for _ in range(random.randint(3, 7)):
    body.send_keys(Keys.PAGE_DOWN)
    time.sleep(random.uniform(0.5, 2.0))
```

#### 처리량
- cosmowifi 주장: **900~1000건/일** (24시간 운영)
- 보수적 추정: **200~500건/일** (차단 리스크 관리 시)

#### 리스크
- Selenium 3.9는 오래된 버전 — Python 3.12+ 호환성 이슈 가능
- Chromium 108 구버전 바이너리 확보 필요
- 최신 Akamai 업데이트에 취약할 수 있음 (2026년 현재도 유효한지 확인 필요)

---

### 방법 4: 수동 브라우저 쿠키 추출 + 재사용

#### 원리

실제 브라우저(Chrome/Firefox)에서 수동으로 쿠팡에 접속하면 Akamai
센서가 자연스럽게 실행되고 유효한 `_abck` 쿠키가 생성된다. 이 쿠키를
추출하여 자동화 요청에 재사용한다.

#### 절차

```
1. 실제 Chrome에서 coupang.com 접속 (수동)
2. 30초~1분 자연스러운 브라우징 (마우스 이동, 스크롤)
3. _abck 쿠키가 valid 상태로 전환됨
4. Chrome DevTools → Application → Cookies → _abck 값 복사
5. 또는 EditThisCookie 확장으로 전체 쿠키 JSON 내보내기
6. curl_cffi 또는 requests에 쿠키 주입하여 요청
```

```python
from curl_cffi import requests as cffi_requests

cookies = {
    "_abck": "extracted_valid_abck_cookie_value...",
    "bm_sz": "extracted_bm_sz_value...",
    "ak_bmsc": "extracted_ak_bmsc_value...",
    # 기타 쿠키들
}

response = cffi_requests.get(
    "https://www.coupang.com/vp/products/123456",
    impersonate="chrome124",
    cookies=cookies,
    headers={
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.coupang.com/",
    },
)
```

#### 한계
- `_abck` 쿠키 수명: **약 30분~2시간** (Akamai 정책)
- 쿠키 만료 시 수동 재추출 필요
- **세션당 3~5건** 정도만 유효 (이후 재검증)
- 완전 자동화 불가 — 반자동(수동 개입) 방식

#### 처리량
- 수동 개입 1회당 3~5건
- 하루 10~20회 수동 개입 → 30~80건/일
- **소량 수집(하루 50건 이하)에 적합**

---

### 방법 5: 브라우저 확장 프로그램 (실브라우저 자동화)

#### 원리

실제 Chrome/Firefox에 설치된 확장 프로그램은 **완전한 실브라우저
환경**에서 동작한다. Akamai 입장에서는 실제 사용자가 브라우징하는
것과 구별이 불가능하다.

#### 구현 컨셉

```javascript
// Chrome Extension content script (concept)
// 실제 브라우저에서 동작하므로 모든 탐지 레이어 통과

// 1. 사용자가 쿠팡 상품 페이지를 방문하면
// 2. 페이지 DOM에서 사업자정보 추출
// 3. 로컬 스토리지 또는 외부 서버로 전송

function extractSellerInfo() {
    const sellerSection = document.querySelector('.prod-sale-vendor-name');
    const businessPopup = document.querySelector('[class*="seller-business"]');
    // ... 사업자정보 필드 추출
    return {
        seller_name: sellerSection?.textContent?.trim(),
        // 팝업이 열려 있으면:
        company_name: document.querySelector('[class*="company"]')?.textContent,
        business_number: document.querySelector('[class*="business-number"]')?.textContent,
        // ...
    };
}
```

#### 장점
- **탐지 불가능** — 실제 브라우저, 실제 사용자 세션
- Akamai 5계층 전부 무의미 (실제 브라우저이므로)
- 프록시 불필요 (자기 IP)
- 비용 0원

#### 단점
- 수동/반자동 브라우징 필요 (완전 자동화 어려움)
- 처리량 매우 낮음 (20~50건/일)
- 최하단 사업자정보 란까지 스크롤 필요

#### 반자동화 전략
- 확장 프로그램이 자동으로 다음 상품 URL로 이동
- 최하단까지 자동 스크롤 후 사업자정보 란 추출
- 또는 `page_action`으로 자동 스크롤 (확장 프로그램 권한)

---

### 방법 6: requests + 헤더 설정 (리스팅 전용 — Stage A)

#### 원리

여러 블로그에서 검증된 사실: **쿠팡 리스팅/검색 페이지는 올바른
헤더만 설정하면 `requests`로도 접근 가능하다.**

```python
import requests
from fake_useragent import UserAgent

def scrape_listing(page_num):
    headers = {
        "User-Agent": UserAgent().random,
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }
    
    url = f"https://www.coupang.com/np/search?q=상품&page={page_num}&listSize=120"
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        soup = BeautifulSoup(response.text, "html.parser")
        products = soup.find_all("li", attrs={"data-product-id": True})
        return products
    return None
```

#### 적용 범위

| Stage | requests 적용 | 비고 |
|-------|-------------|------|
| **A (리스팅 스캔)** | **✅ 가능** | 헤더 설정만으로 200 응답 |
| B (판매자 축약) | N/A (로컬) | 네트워크 요청 없음 |
| **C (상세 스크래핑)** | **❌ 불가** | JS 렌더링 + _abck 필수 |
| D (집계) | N/A (로컬) | 네트워크 요청 없음 |

#### 의미

**Stage A를 requests로 처리하면 StealthySession 부하가 Stage C에만
집중된다.** 이는:
- Stage A에 프록시/브라우저 불필요 → 전체 리소스 절약
- Stage C만 StealthySession 사용 → 세션 수 감소 → 차단 리스크 감소
- **Stage A 처리량: 500~1000건/일 (무료, requests)**

#### 주의
- `time.sleep(random.randint(1, 3))` 필수 (요청 간 랜덤 지연)
- 매 요청 User-Agent 랜덤화
- 403 응답 시 즉시 중단 + 쿨다운
- **상세페이지(Stage C)에는 적용 불가** — JS 렌더링 필수

---

## 3. 사업자정보 렌더링 방식 재검증 (핵심 질문)

### 3.1 기존 문서의 주장

`PAGE_STRUCTURE.md` §4.3:
> "브라우저 필수 — requests + BeautifulSoup 만으로는 판매자정보 추출 불가"
> "상품 정보·판매자 정보 모두 JS 비동기 로드"

### 3.2 재검증 필요성

이 결론은 **실측 없이 작성된 추정**이다 (문서 자체에 "⚠ 미검증" 표기).
실제로는 다음 가능성이 있다:

| 시나리오 | 의미 | 무료 영향 |
|---------|------|----------|
| A. 사업자정보가 초기 HTML에 포함 (SSR) | requests로도 추출 가능 | **Stage C도 requests로 가능 → 완전 무료** |
| B. 사업자정보가 JS로 렌더링 (최하단 섹션) | 브라우저 필수 | StealthySession 필요 (방법 1~3) |
| C. 사업자정보가 초기 HTML의 JSON에 포함 (hydration data) | requests + JSON 파싱으로 가능 | **Stage C도 requests로 가능 → 완전 무료** |

### 3.3 시나리오 C의 가능성 (가장 유망)

현대 React/Next.js 앱은 초기 HTML에 `__NEXT_DATA__` 또는
`window.__INITIAL_STATE__` 형태의 JSON을 포함한다. 쿠팡이 이
패턴을 사용한다면:

```html
<!-- 가상의 쿠팡 상품 페이지 HTML -->
<script id="__NEXT_DATA__" type="application/json">
{
  "props": {
    "pageProps": {
      "product": {
        "sellerInfo": {
          "sellerName": "판매자명",
          "businessNumber": "123-45-67890",
          "companyName": "상호명",
          "ceoName": "대표자",
          "address": "주소",
          "phone": "02-1234-5678",
          "mailOrderLicense": "제2024-서울강남-12345호"
        }
      }
    }
  }
}
</script>
```

**이 경우 `requests` + `json.loads()` 로 사업자정보를 추출할 수 있다.**
브라우저 불필요, 프록시 불필요, **완전 무료.**

### 3.4 검증 방법 (스파이크)

```bash
# 1. 실제 브라우저에서 쿠팡 상품 페이지 접속
# 2. Ctrl+U (소스보기) 또는 curl로 raw HTML 다운로드
# 3. "사업자" OR "businessNumber" OR "sellerBusiness" grep
# 4. __NEXT_DATA__ 또는 window.__* 형태의 JSON 탐색

# 빠른 확인 (한국 IP에서):
curl -s -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36" \
     -H "Accept-Language: ko-KR,ko;q=0.9" \
     "https://www.coupang.com/vp/products/{productId}" | grep -i "business\|seller\|사업자"
```

### 3.5 검증 결과에 따른 전략 분기

```
[스파이크 결과]
├── 시나리오 A/C (HTML에 포함): 
│   → requests + 헤더만으로 Stage C 가능
│   → 완전 무료, 500~1000건/일
│   → 프로젝트 난이도 대폭 하락
│
└── 시나리오 B (XHR/JS 필수):
    → StealthySession + 한국 가정용 IP (방법 1~3)
    → 무료 but 처리량 제한 (50~300건/일)
    → 기존 문서의 아키텍처 유지, 프록시만 제거
```

---

## 4. 조합 전략 (추천)

### 4.1 최적 무료 조합

```
[Stage A: 리스팅 스캔]
├── 1차: requests + 헤더 (방법 6) — 500~1000건/일, 0원
└── 실패 시: StealthySession + 한국 IP (방법 1)

[Stage B: 판매자 축약]
└── 로컬 처리 (네트워크 없음)

[Stage C: 상세 스크래핑]
├── 시나리오 A/C 확인 시: requests + 헤더 — 완전 무료
└── 시나리오 B 확인 시:
    ├── 1차: StealthySession + 한국 가정용 IP (방법 1)
    ├── IP flag 시: 공유기 재부팅 (방법 2)
    └── 대안: Selenium 3.9 + Chromium 108 (방법 3)

[Stage D: 집계]
└── 로컬 처리 (네트워크 없음)
```

### 4.2 일일 처리량 추정 (무료 조합)

| 시나리오 | Stage A | Stage C | 최종 산출 |
|---------|---------|---------|----------|
| **최선 (HTML에 사업자정보)** | 1000건 (requests) | 1000건 (requests) | **~400 사업자/일** |
| **중간 (StealthySession + 한국 IP)** | 1000건 (requests) | 150건 (StealthySession) | **~60 사업자/일** |
| **보수 (IP 회전 포함)** | 500건 (requests) | 300건 (IP 회전) | **~120 사업자/일** |

### 4.3 기존 유료 경로 대비

| 항목 | 유료 프록시 | 무료 조합 (중간) | 무료 조합 (최선) |
|------|-----------|----------------|----------------|
| 월 비용 | 10~30만원 | **0원** | **0원** |
| 일일 처리량 | 500~1000건 | 60~150건 | 400~1000건 |
| 월 처리량 | 15,000~30,000건 | 1,800~4,500건 | 12,000~30,000건 |
| 차단 리스크 | 낮음 | 중 (보수적 설정으로 관리) | 낮음 |
| 구현 복잡도 | 높음 | 중 | **낮음** |

---

## 5. 추가 무료 우회 경로 (보조)

### 5.1 Google Cache (제한적)

Google이 캐시한 쿠팡 상품 페이지를 통해 접근:
```
https://webcache.googleusercontent.com/search?q=cache:coupang.com/vp/products/{id}
```

**한계**: 
- 사업자정보 최하단 란이 JS 렌더링 시 캐시에 포함 안 됨
- 캐시 자체가 없는 페이지 많음
- Google도 rate limit 있음
- **Stage A 보조 수단으로만 유용**

### 5.2 Wayback Machine (과거 데이터)

```
https://web.archive.org/web/2024/https://www.coupang.com/vp/products/{id}
```

**한계**:
- 사업자정보 최하단 란 데이터 없음 (JS 렌더링 시)
- 최신 데이터 아님
- **과거 판매자 목록 확보 용도로만 유용**

### 5.3 네이버 쇼핑 검색 (간접)

네이버 쇼핑 검색 API (무료, 월 25,000회):
```
https://openapi.naver.com/v1/search/shop.json?query={keyword}
```

**한계**:
- 쿠팡 판매자 정보가 아닌 네이버 쇼핑 입점 판매자 정보
- 쿠팡 전용 데이터 아님
- **경쟁 플랫폼 분석 용도로만 유용**

### 5.4 쿠팡 사이트맵 (상품 URL 목록)

```
https://www.coupang.com/sitemap.xml
```

**용도**: Stage A의 상품 URL 목록을 사이트맵에서 확보하면
리스팅 스캔(StealthySession) 없이도 상품 URL을 알 수 있다.
→ **Stage A 비용 추가 절감**

---

## 6. 법적·윤리적 고려사항

### 6.1 합법성

- 사업자정보(상호, 대표자, 사업자등록번호, 주소, 전화번호)는
  **전자상거래법 제10조**에 따라 공개가 **의무**인 정보
- 공개된 정보를 수집하는 것 자체는 위법이 아님
- 단, **수집 방법**이 문제: robots.txt 위반, 과도한 트래픽 유발은
  정보통신망법 위반 소지

### 6.2 준수 사항

| 항목 | 준수 |
|------|------|
| robots.txt | 허용 경로(`/np/categories/`, `/np/search`) 우선 사용 |
| 요청 빈도 | 서버에 부하를 주지 않는 수준 (1~3초 간격) |
| 개인정보 | 사업자정보(공개 의무 정보)만 수집, 개인 연락처 등 미수집 |
| 상업적 이용 | 수집 데이터의 용도 명확화 |
| 이용약관 | 쿠팡 이용약관의 크롤링 금지 조항 인지 (민사 리스크) |

### 6.3 리스크

- 쿠팡 이용약관에서 자동화 수집을 금지할 수 있음 (민사)
- 과도한 트래픽 시 업무방해죄 적용 가능성 (형사, 극단적 경우)
- **보수적 빈도(1~3초 간격, 일 150건 이하) 유지 시 리스크 극히 낮음**

---

## 7. 기존 문서 수정 권고

### 7.1 `COLLECTION_STRATEGY.md` 수정

```diff
- **프록시 미확보 시**: **파이프라인 전체가 동작하지 않는다.**
+ **프록시 미확보 시**: 한국 가정용 IP + StealthySession으로
+ 무료 운용 가능 (처리량 1/5~1/7로 감소).
+ 유료 프록시는 처리량 확대 옵션.
```

### 7.2 `DATA_FIELDS_MAPPING.md` 수정

```diff
- **프록시가 파이프라인 성립의 절대 전제조건이다** — 무료로 동작하는
- 대체 경로가 하나도 없다
+ **한국 가정용 IP가 파이프라인 성립의 전제조건이다.**
+ 유료 프록시는 처리량 확대를 위한 선택 옵션.
+ 단, 사업자정보가 HTML에 포함(시나리오 A/C)이면
+ requests만으로 완전 무료 운용 가능.
```

### 7.3 `PLATFORM_ANALYSIS.md` 수정

```diff
- | IP 제한 | 한국 IP면 충분 | **한국 주거용 IP 필수** |
+ | IP 제한 | 한국 IP면 충분 | **한국 주거용 IP 필수 (가정용 인터넷 = 충족)** |
```

---

## 8. 즉시 실행 가능한 검증 절차 (스파이크)

### Priority 0: 사업자정보 렌더링 방식 확인 (30분)

```
1. 한국 가정용 PC에서 Chrome으로 쿠팡 상품 페이지 접속
2. Ctrl+U (소스보기) → "사업자" / "business" / "seller" 검색
3. 페이지 최하단으로 스크롤 → 사업자정보 란 확인
4. F12 → Network 탭 → 페이지 로드 시 XHR 요청 중 사업자정보 포함 여부 확인
5. 결과:
   - HTML에 포함 → 시나리오 A/C → requests만으로 가능
   - JS 렌더링 → 시나리오 B → StealthySession 필요
```

### Priority 1: requests 접근 테스트 (15분)

```
1. 한국 가정용 PC에서:
   curl -s -H "User-Agent: Mozilla/5.0 ..." \
        -H "Accept-Language: ko-KR,ko;q=0.9" \
        "https://www.coupang.com/np/search?q=테스트&listSize=36" \
        -o test.html
2. test.html 크기 확인 (> 50KB면 성공)
3. "data-product-id" grep → 리스팅 데이터 존재 확인
```

### Priority 2: StealthySession + 한국 IP 테스트 (1시간)

```
1. 기존 프로젝트의 scrapling 설치 확인
2. StealthySession(headless=True)로 coupang.com 홈 fetch
3. 상태 코드 + 페이지 크기 확인
4. 성공 시: 상품 상세페이지 fetch → 사업자정보 존재 확인
```

---

## 9. 결론

### 기존 문서의 오류

기존 문서는 **"유료 주거용 프록시 없이는 불가능"** 이라고 결론지었으나,
이는 다음 전제 오류에 기반한다:

1. **"주거용 IP = 유료 프록시 서비스"** — 한국에 있으면 가정용 인터넷이
   이미 주거용 IP다
2. **테스트 환경이 한국 가정용 IP가 아니었을 가능성** — 데이터센터/해외
   IP에서 테스트했다면 403이 당연
3. **"requests = 403"만 테스트** — 헤더 완전성, 행동 시뮬레이션, 구버전
   브라우저 등의 변수를 통제하지 않음

### 최종 판정

| 질문 | 답변 |
|------|------|
| 프록시 비용 0원으로 가능한가? | **YES** (한국 가정용 IP 전제) |
| 처리량 제한은? | 50~300건/일 (유료 대비 1/3~1/7) |
| 완전 자동화 가능한가? | **YES** (StealthySession + 공유기 재부팅) |
| 사업자정보 확보 가능한가? | **YES** (스파이크로 렌더링 방식 확인 후) |
| 추가 비용? | **0원** (Scrapling 오픈소스 + 가정용 인터넷) |
| 최대 리스크? | IP flag → 공유기 재부팅으로 해결 |

### 권장 다음 단계

1. **즉시**: Priority 0 스파이크 실행 (사업자정보 렌더링 방식 확인)
2. **스파이크 결과에 따라**:
   - HTML 포함 → requests 기반 경량 구현 (완전 무료, 고성능)
   - XHR 로드 → StealthySession + 한국 IP (무료, 중성능)
3. **처리량 부족 시**: 유료 프록시를 "확장 옵션"으로 재분류

---

## Sources

- [쿠팡 크롤링 2026 완벽 가이드 — Akamai 우회의 모든 것](https://blog.hashscraper.com/posts/coupang-crawling-2026-complete-guide-everything-about-akamai-bypass?locale=ko)
- [2025년 쿠팡 DB 크롤링 현실 - 여전히 되는데 왜 안된다고 하냐?](https://cosmowifi.tistory.com/entry/2025%EB%85%84-%EC%BF%A0%ED%8C%A1-DB-%ED%81%AC%EB%A1%A4%EB%A7%81-%ED%98%84%EC%8B%A4-%EC%97%AC%EC%A0%84%ED%9E%88-%EB%90%98%EB%8A%94%EB%8D%B0-%EC%99%9C-%EC%95%88%EB%90%9C%EB%8B%A4%EA%B3%A0-%ED%95%98%EB%83%90)
- [쿠팡 크롤링 - 판매자, 상품평, 상세페이지 등 크롤링](https://cosmowifi.tistory.com/entry/%EC%BF%A0%ED%8C%A1-%ED%81%AC%EB%A1%A4%EB%A7%81-%ED%8C%90%EB%A7%A4%EC%9E%90-%EC%83%81%ED%92%88%ED%8F%89%EB%A6%AC%EB%B7%B0%EC%82%AC%EC%A7%84-%EC%83%81%EC%84%B8%ED%8E%98%EC%9D%B4%EC%A7%80-%EC%82%AC%EC%A7%84-%EB%93%B1-%EC%98%A8%EA%B0%96-%EC%A0%95%EB%B3%B4-%ED%81%AC%EB%A1%A4%EB%A7%81-%ED%95%98%EA%B8%B0-1%ED%8E%B8)
- [크롤링을 통한 데이터 수집 feat. 쿠팡](https://velog.io/@chltpdus48/%ED%81%AC%EB%A1%A4%EB%A7%81%EC%9D%84-%ED%86%B5%ED%95%9C-%EB%8D%B0%EC%9D%B4%ED%84%B0-%EC%88%98%EC%A7%91feat.-%EC%BF%A0%ED%8C%A1)
- [웹 크롤링 실전 - 쿠팡 크롤링 안되면 이것만 넣어주면 됩니다](https://iamgus.tistory.com/699)
- [차단당하지 않고 웹 크롤링 하는 방법 (쿠팡)](https://kimflstudio.tistory.com/entry/%EC%B0%A8%EB%8B%A8%EB%8B%B9%ED%95%98%EC%A7%80-%EC%95%8A%EA%B3%A0-%EC%9B%B9-%ED%81%AC%EB%A1%A4%EB%A7%81-%ED%95%98%EB%8A%94-%EB%B0%A9%EB%B2%95%EC%BF%A0%ED%8C%A1-%ED%81%AC%EB%A1%A4%EB%A7%81-%ED%8C%8C%EC%9D%B4%EC%8D%AC-%EC%BD%94%EB%93%9C-%EC%A0%9C%EA%B3%B5)
- [Coupang Data Scraping 2026: The Enterprise Guide](https://kndusc.com/blogs/coupang-data-scraping-enterprise-guide/)
- [Coupang Seller Competition Monitoring (Apify)](https://apify.com/saswave/coupang-seller-competition-monitoring)
- [쿠팡 검색 상품 Python으로 불러오기](https://gist.github.com/gnh1201/050901082adc7af40734c66fd09a102a)
- [쿠팡 상품 정보 스크래핑하는 방법 (Octoparse)](https://www.octoparse.kr/blog/how-to-scrape-coupang)
- [Akamai 봇 감지 우회: 완벽한 가이드 (Bright Data)](https://brightdata.co.kr/blog/web-data/bypass-akamai-bot-detection)
- [쿠팡 Open API](https://developers.coupang.com/)
- [네이버 쇼핑 검색 API](https://developers.naver.com/docs/serviceapi/search/shopping/shopping.md)
