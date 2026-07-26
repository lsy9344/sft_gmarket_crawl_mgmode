# Coupang OMP - Anti-Bot Bypass Technical Guide

## Verified Bypass Techniques (2025-2026 Success Cases)

### 1. Browser Engine Strategy

**Core Principle**: Old engine + New identity

```python
# Chromium 108 (old, stable, less fingerprint leakage)
# User-Agent spoofed as Chrome 125 (current-looking)
CHROMIUM_VERSION = "108"
SPOOFED_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
```

### 2. Selenium/WebDriver Stealth Configuration

> **⚠ 참고용 — 본 프로젝트에서 채택하지 않음.** 실제 구현은 Scrapling
> `StealthySession`(Camoufox C++ 레벨 스푸핑)을 사용한다
> (`SCRAPLING_TECH_ANALYSIS.md` §4). 아래 Selenium 코드는 JS 삽입 기반이라
> Akamai JS 무결성 검사에서 탐지 가능하며, 비교/이해 목적으로만 남긴다.

```python
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

options = Options()
options.binary_location = "/path/to/chromium-108/chrome.exe"

# Remove automation fingerprints
options.add_argument("--disable-blink-features=AutomationControlled")
options.add_experimental_option("excludeSwitches", ["enable-automation"])
options.add_experimental_option("useAutomationExtension", False)
options.add_argument(f"--user-agent={SPOOFED_UA}")
options.add_argument("--remote-debugging-port=9222")
options.add_argument("--disable-infobars")
options.add_argument("--disable-dev-shm-usage")
options.add_argument("--no-sandbox")

driver = webdriver.Chrome(options=options)

# Post-launch JS injection
driver.execute_script("""
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['ko-KR', 'ko', 'en-US', 'en']});
    window.chrome = { runtime: {} };
""")
```

### 3. Behavioral Simulation (Coupang-Specific Requirement)

```python
import random
import time

def human_scroll(driver):
    """Keyboard-based scroll (JS scrollTo() is detected)"""
    for _ in range(random.randint(3, 7)):
        driver.find_element("tag name", "body").send_keys(Keys.PAGE_DOWN)
        time.sleep(random.uniform(0.5, 2.0))

def human_mouse_move(driver, element):
    """Bezier curve mouse movement (straight-line is detected)"""
    from selenium.webdriver.common.action_chains import ActionChains
    actions = ActionChains(driver)
    # Multi-step movement with random offsets
    for step in range(random.randint(5, 15)):
        x_offset = random.randint(-3, 3)
        y_offset = random.randint(-3, 3)
        actions.move_by_offset(x_offset, y_offset)
        actions.pause(random.uniform(0.02, 0.08))
    actions.move_to_element(element)
    actions.perform()

def human_delay(base=1.0, jitter=2.0):
    """Random delay between actions"""
    time.sleep(base + random.uniform(0, jitter))
```

### 4. Navigation Pattern (Referral Chain)

```python
# Coupang detects direct URL access patterns
# Mimic organic user: search engine → coupang → product

def organic_navigation(driver, target_url):
    # Step 1: Start from search portal
    driver.get("https://www.google.com")
    human_delay(2, 3)
    
    # Step 2: Search for product category
    search_box = driver.find_element("name", "q")
    search_box.send_keys("쿠팡 상품")  # Simulate typing
    search_box.send_keys(Keys.RETURN)
    human_delay(3, 5)
    
    # Step 3: Navigate to Coupang
    driver.get("https://www.coupang.com")
    human_delay(5, 8)  # Wait for _abck cookie generation
    
    # Step 4: Navigate to target
    driver.get(target_url)
    human_delay(3, 5)
```

### 5. Session Management

```python
SESSION_CONFIG = {
    "max_items_per_session": 5,       # Gmarket: 8, Coupang: 3~5
    "session_cooldown": (600, 900),   # Gmarket: (300, 480), Coupang: 600~900s
    "item_delay": (5, 15),            # Gmarket: (0.5, 0.8), Coupang: 5~15s
    "warmup_wait": (8, 15),           # Gmarket: 3~5s, Coupang: 8~15s (70KB sensor)
    "max_daily_items": 1000,          # Hard cap for 0% block rate
    "abort_after_blocks": 2,          # Gmarket: 3, Coupang: 2 (more conservative)
}
```

> **rev.2 구현 주의 2가지**
>
> 1. **모든 대기는 취소 반응형이어야 한다.** `session_cooldown` 600~900초를
>    `time.sleep()` 으로 구현하면 [취소]가 최대 15분 먹통이 된다.
>    `Control.sleep()` 으로 분할 대기하고 남은 시간을 UI에 표시한다
>    (`app/core/prescan.py:145` 의 기존 패턴을 일반화).
> 2. **`max_daily_items` 는 디스크에 영속화해야 의미가 있다.** 메모리
>    카운터만 쓰면 앱 재시작으로 리셋되어 하드캡이 무력화된다
>    (`coupang_budget.json` — `IMPLEMENTATION_PLAN.md` AD-7).
>
> 또한 이 상수들은 **상품이 아니라 판매자 단위**로 소비된다. 같은 판매자의
> 상품 30건에 대해 상세를 30번 여는 것이 아니라 1번만 연다
> (`COLLECTION_STRATEGY.md` D2) — 같은 일일 예산으로 훨씬 많은 사업자를 확보한다.

### 6. Page Parsing Strategy

```python
# Download full page → strip scripts → parse locally
# Avoids triggering additional element-request tracking

def safe_parse(driver):
    """Download DOM, strip scripts, parse locally"""
    from bs4 import BeautifulSoup
    
    html = driver.page_source
    soup = BeautifulSoup(html, "html.parser")
    
    # Remove all script tags (prevents secondary tracking)
    for script in soup.find_all("script"):
        script.decompose()
    
    return soup
```

### 7. HTTP/2 Blocking Mitigation

Coupang intermittently blocks HTTP/2 automated traffic:
- Use Chromium 108 (older HTTP/2 implementation, less fingerprintable)
- Avoid `requests` library entirely for Coupang (Python TLS signature detected)
- If using HTTP client: `curl_cffi` with Chrome impersonation

```python
# Alternative: curl_cffi for TLS fingerprint matching
from curl_cffi import requests as cffi_requests

response = cffi_requests.get(
    url,
    impersonate="chrome124",  # Matches Chrome TLS fingerprint
    headers={"Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"},
    proxies={"https": residential_proxy_url},   # curl_cffi 는 proxies(복수) 가 맞다
)
```

> **주의 — 파라미터명 혼동.** 위 `proxies=`(복수)는 **curl_cffi/requests**
> 의 API다. Scrapling `StealthySession` 은 **`proxy=`(단수)** 를 쓴다
> (`SCRAPLING_TECH_ANALYSIS.md` §0 정정표). 두 라이브러리를 오가며 코드를
> 옮길 때 가장 흔히 나오는 실수다.
>
> **본 프로젝트에서 이 경로는 쓰지 않는다.** curl_cffi 는 TLS 지문(L1)만
> 모방할 뿐 `_abck` 센서 스크립트를 실행하지 못해 L2를 통과할 수 없다.
> Coupang은 전 사이트가 Akamai로 보호되고(`PLATFORM_ANALYSIS.md`), 필수
> 수집 항목이 전부 그 보호된 상세페이지 안에 있으므로
> (`DATA_FIELDS_MAPPING.md` rev.4) 봇감지 없는 우회 대상 엔드포인트 자체가
> 존재하지 않는다 — 모든 요청은 `StealthySession`(L3)을 거쳐야 한다.

### 8. Akamai `_abck` Cookie Handling

```
Mechanism:
1. First visit → server sets initial _abck (invalid)
2. Browser executes ~70KB sensor script
3. Sensor collects: mouse moves, key presses, touch events,
   canvas fingerprint, WebGL, AudioContext, timezone, etc.
4. Sensor generates telemetry → POST to /akam/... endpoint
5. Server validates → issues valid _abck cookie
6. Subsequent requests with valid _abck are allowed

Critical: _abck CANNOT be copied from another session.
Must be generated in-browser with genuine interaction.
```

### 9. Proxy Configuration

```python
PROXY_CONFIG = {
    "type": "residential",           # Datacenter = instant block
    "country": "KR",                 # Korea only
    "city": "Seoul",                 # Preferred (largest pool)
    "rotation": "per_session",       # New IP per session (not per request)
    "providers": [
        "Bright Data (luminati)",
        "Oxylabs",
        "Smartproxy",
        "IPRoyal",
    ],
    "estimated_cost": "10~30만원/월",  # For 500-1000 items/day
}
```

### 10. Detection & Recovery

```python
COUPANG_BLOCK_INDICATORS = {
    "status_codes": [403, 418, 429],
    "keywords": [
        "자동화된 테스트 소프트웨어",
        "접근이 제한",
        "보안 절차",
        "확인 절차",
        "잠시 후 다시 시도",
    ],
    "empty_200": True,  # 200 OK but empty/minimal body
}

def is_blocked(response) -> bool:
    if response.status_code in [403, 418, 429]:
        return True
    if response.status_code == 200 and len(response.text) < 1000:
        return True  # Soft block
    return any(kw in response.text for kw in COUPANG_BLOCK_INDICATORS["keywords"])

def on_block_detected(stats, control, budget):
    """Immediate session termination + extended cooldown.

    rev.2 정정 — time.sleep(1800) 을 쓰면 안 된다:
      ① [취소] 버튼이 최대 30분 먹통이 되고,
      ② 창을 닫을 때 좀비 스레드를 기다리며 UI가 얼어붙으며,
      ③ 앱이 재시작되면 복구 대기가 통째로 사라져 곧바로 재차단된다.
    → 분할 대기(control.sleep) + 차단 시각 영속화(budget.mark_block).
    """
    stats["bot_detected"] += 1
    budget.mark_block(); budget.save()          # 재시작 후에도 복구 대기 유지
    cooldown = random.uniform(900, 1800)        # 15~30 min
    control.sleep(cooldown, on_tick=lambda rem: ui.show_cooldown(rem))
```

## Performance Benchmarks (from reported success cases)

| Metric | Value |
|--------|-------|
| Daily collection | 500~1,000 items |
| Block rate | 0% (with full stealth) |
| Success rate | 99% |
| Session duration | 24h stable |
| Items per session | 3~5 |
| Session cooldown | 10~15 min |
| Total daily sessions | ~100~200 |

> **rev.5 주의.** 위 수치는 이상 조건(주거용 프록시 품질 균일, Akamai 정책
> 미변경) 기준이다. 실제 운영에서는 §11~§15의 보강 없이는 이 수치를 기대하기
> 어렵다. 특히 "0% block rate"는 **웜업 상호작용 + 세션 간 랜덤화 + 프록시
> canary** 가 전부 충족될 때의 상한선이다.

---

## 11. 웜업 상호작용 품질 (rev.5 신규) ★ 핵심 보강

### 문제

§5의 `warmup_wait: (8, 15)` 초는 **대기 시간**만 정의한다. Akamai 센서(70KB)는
대기 시간이 아니라 **실제 상호작용 이벤트**(마우스 이동, 키스트로크, 스크롤,
클릭)를 수집한다. 마우스 이동 0건 + 키 입력 0건인 세션은 "headless bot"
시그니처 그 자체다 — 15초를 기다려도 센서 텔레메트리가 비어 있으면 L2 검증이
불완전하게 통과되거나 L4에서 flag 된다.

### 원칙

> **웜업은 "대기"가 아니라 "상호작용"이다.** 센서가 의미 있는 텔레메트리를
> 수집할 수 있도록 실제 사용자 행동을 주입한다.

### 구현 (page_action 또는 웜업 전용 콜백)

```python
def warmup_interact(page, control):
    """_abck 센서가 의미 있는 텔레메트리를 수집하도록 유도.

    Camoufox의 C++ 마우스 궤적은 page.mouse.move() 호출 시 자동 적용된다.
    JS scrollTo() 는 탐지 가능하므로 keyboard 기반 스크롤을 쓴다.
    """
    import random

    # 1. 마우스를 viewport 중앙으로 이동 (C++ 베지에 궤적 자동 생성)
    page.mouse.move(random.randint(400, 800), random.randint(300, 500))

    # 2. 2~4회 소폭 스크롤 (keyboard — JS scrollTo 아님)
    for _ in range(random.randint(2, 4)):
        page.keyboard.press("PageDown")
        control.sleep(random.uniform(0.3, 1.2))

    # 3. 검색창 포커스 + 타이핑 시뮬레이션 (키스트로크 다이나믹스 생성)
    #    센서는 keydown/keyup 간격, 압력 패턴을 수집한다
    page.keyboard.type("쿠팡", delay=random.randint(80, 200))
    control.sleep(random.uniform(0.5, 1.5))
    page.keyboard.press("Escape")  # 검색 취소 — 실제 유저가 자주 하는 행동

    # 4. 1~2회 랜덤 클릭 (비상품 영역 — 푸터, 카테고리 메뉴 등)
    #    클릭 좌표 + 체류 시간이 센서에 기록됨
    page.mouse.click(random.randint(100, 300), random.randint(600, 800))
    control.sleep(random.uniform(1.0, 3.0))

    # 5. 상단으로 스크롤 복귀 (Home 키)
    page.keyboard.press("Home")
    control.sleep(random.uniform(0.5, 1.0))
```

### 적용 위치

```python
# Stage A / Stage C 공통 — 홈 웜업 직후 호출
session.fetch(cfg.HOME_URL, network_idle=True)
warmup_interact(session.current_page, control)   # ← 대기 전에 상호작용
control.sleep(random.uniform(3, 6))              # 센서 POST 완료 대기
```

### 검증 기준 (스파이크 P0-3)

- 웜업 후 `_abck` 쿠키가 "valid" 상태로 전환되는지 확인
  (initial `_abck` 는 `sz=1` 또는 빈 토큰, valid는 긴 base64 페이로드)
- 상호작용 없이 15초 대기만 한 경우와 비교해 후속 요청 차단율 차이 측정

---

## 12. 세션 간 상관관계 차단 (rev.5 신규)

### 문제

Akamai는 단일 세션 내 행동뿐 아니라 **세션 간 메타패턴**을 상관 분석한다:
- 항상 홈 → 정확히 N초 → 리스팅 (타이밍 정규성)
- 항상 같은 `listSize`/`sorter` 파라미터 조합
- 세션 시작 후 정확히 5건에서 종료 (기계적 절단)
- 매일 같은 시각에 수집 시작 (주기성)

이 패턴이 누적되면 개별 세션이 아무리 자연스러워도 **메타 시그니처**로 탐지된다.

### 대응 전략

```python
class SessionRandomizer:
    """세션 간 메타패턴 정규성을 깨뜨린다."""

    def batch_size(self) -> int:
        """배치 크기를 랜덤화 — 항상 5에서 끊지 않음."""
        return random.randint(3, 5)

    def warmup_duration(self) -> float:
        """로그노멀 분포 — 중앙값 10초, 꼬리가 있어 예측 불가."""
        import math
        return random.lognormvariate(math.log(10), 0.4)

    def maybe_decoy_visit(self, session, control) -> None:
        """30% 확률로 목적 없는 페이지 방문 삽입."""
        if random.random() < 0.3:
            decoy_urls = [
                "https://www.coupang.com/np/categories/186764",
                "https://www.coupang.com/np/campaigns/82",  # 베스트
                "https://www.coupang.com/np/search?q=추천",
            ]
            session.fetch(random.choice(decoy_urls), network_idle=True)
            control.sleep(random.uniform(3, 8))

    def sorter(self) -> str:
        """정렬 옵션을 세션마다 변경."""
        return random.choice(["bestAsc", "priceAsc", "priceDesc", "rateDesc"])

    def start_jitter_hours(self) -> float:
        """실행 시작 시각에 ±2시간 지터 (주기성 방지).
        스케줄러에서 사용 — 매일 같은 시간에 시작하지 않는다."""
        return random.uniform(-2.0, 2.0)
```

### 적용 규칙

| 항목 | 고정 값 (현재) | 랜덤화 (rev.5) |
|------|--------------|----------------|
| 배치 크기 | 항상 5 | 3~5 (세션마다) |
| 웜업 대기 | 균등(8, 15) | 로그노멀(중앙값 10초) |
| 정렬 옵션 | bestAsc 고정 | 4종 중 랜덤 |
| 세션 시작 전 | 홈 바로 fetch | 30% 확률로 무관 페이지 경유 |
| 일일 실행 시각 | 사용자 수동 | ±2시간 지터 권장 (스케줄러 사용 시) |
| 페이지 간 지연 | 균등(8, 15) | 균등 유지하되 세션마다 base偏移 (7~10, 10~18 등) |

---

## 13. 프록시 품질 검증 (Canary) + 세션 고정 (rev.5 신규)

### 문제

주거용 프록시 풀에 데이터센터 IP가 섞여 있거나, 이미 Akamai에 flagged된 IP가
할당될 수 있다. 한 번 flagged IP로 요청하면 그 세션 전체가 오염되고,
나아가 같은 공급자 풀의 다른 IP까지 평판이 하락할 수 있다.

### Canary 검증

```python
class ProxyProvider:
    """프록시 할당 전 경량 canary로 IP 품질을 검증한다."""

    def __init__(self, pool: list[str], control):
        self._pool = pool
        self._idx = 0
        self._blacklisted: set[str] = set()
        self._control = control

    def next(self) -> str:
        """검증된 프록시 1개를 반환. 최대 3회 시도."""
        for _ in range(3):
            proxy = self._pool[self._idx % len(self._pool)]
            self._idx += 1
            if proxy in self._blacklisted:
                continue
            if self._canary(proxy):
                return proxy
        raise ProxyExhaustedError("사용 가능한 프록시 없음 — 풀 품질 확인 필요")

    def _canary(self, proxy: str) -> bool:
        """경량 품질 검증 — StealthySession 불필요, curl_cffi로 L1만 확인.

        coupang.com 홈의 HTTP status + body 크기만 본다.
        403/429 이거나 body < 5KB면 해당 IP는 이미 flagged.
        """
        from curl_cffi import requests as cffi_requests
        try:
            r = cffi_requests.get(
                "https://www.coupang.com/",
                impersonate="firefox135",
                proxies={"https": proxy},
                timeout=10,
            )
            return r.status_code == 200 and len(r.text) > 5000
        except Exception:
            return False

    def blacklist(self, proxy: str) -> None:
        """차단 감지 시 해당 IP를 현재 실행에서 제외."""
        self._blacklisted.add(proxy)

    def is_configured(self) -> bool:
        return len(self._pool) > 0
```

### 세션 내 IP 고정 (Sticky Session)

프록시 공급자의 **sticky session** 기능을 활용해 같은 세션 내 요청이 같은
출구 IP로 나가야 한다. 세션 중간에 IP가 바뀌면 Akamai가 "세션 하이재킹"으로
판단해 즉시 차단한다.

```python
# Bright Data 예: session ID를 붙이면 같은 출구 IP 유지 (최대 30분)
proxy_url = f"http://user-zone-residential-session{session_id}:pass@brd.example:22225"

# Oxylabs 예: sessionid 파라미터
proxy_url = f"http://user:pass@kr.residential.oxylabs.io:7777"  # sticky by default

# Smartproxy 예: session ID
proxy_url = f"http://user-session{session_id}-sessid-{session_id}:pass@gate.smartproxy.com:10000"
```

> **공급자별 sticky session 문법은 스파이크 시 확인한다.** 세션당 5건 +
> 웜업/쿨다운 포함 ~15분이므로 30분 sticky면 충분하다.

---

## 14. 적응적 백오프 (Adaptive Backoff) (rev.5 신규)

### 문제

§10의 차단 복구는 **고정 쿨다운**(900~1800초) 후 같은 행동 패턴으로
재시도한다. Akamai는 IP 단위 + **행동 패턴 단위**로 차단하므로, 같은
패턴을 유지한 채 시간만 보내고 재시도하면 재차단 확률이 높다.

### 단계적 완화 전략

```python
class AdaptiveBackoff:
    """차단 이력에 기반한 점진적 전략 변경.

    차단이 누적될수록 행동 프로파일을 완화한다.
    """

    def __init__(self, budget, control):
        self._history: list[dict] = []
        self._budget = budget
        self._control = control
        self._level = 0

    def on_block(self, context: dict) -> None:
        """차단 발생 시 호출. context: {stage, page_no, proxy, timestamp}."""
        self._history.append(context)
        self._budget.mark_block()
        self._budget.save()

        if len(self._history) >= 2 and self._consecutive():
            self._level = min(self._level + 1, 4)
            self._escalate()

    def _consecutive(self) -> bool:
        """최근 2회 차단이 같은 실행 내에서 발생했는지."""
        if len(self._history) < 2:
            return False
        return True  # 같은 실행 내이므로 항상 True

    def _escalate(self) -> None:
        """레벨별 완화 조치."""
        if self._level == 1:
            # Level 1: 쿨다운 2배 + 배치 크기 1 감소
            self._cooldown_multiplier = 2.0
            self._batch_reduction = 1
        elif self._level == 2:
            # Level 2: 다음 실행을 2~4시간 후로 연기 (시간대 분산)
            self._cooldown_multiplier = 3.0
            self._batch_reduction = 2
        elif self._level == 3:
            # Level 3: 프록시 공급자 교체 (IP 풀 자체가 오염됐을 수 있음)
            self._cooldown_multiplier = 4.0
            self._batch_reduction = 2
            # → UI에 "프록시 공급자 변경 권장" 알림
        elif self._level >= 4:
            # Level 4: 24시간 전면 중단
            self._cooldown_multiplier = 0  # 의미 없음
            # → budget.suspend_until = now + 24h

    @property
    def cooldown_multiplier(self) -> float:
        return getattr(self, "_cooldown_multiplier", 1.0)

    @property
    def effective_batch_size(self) -> int:
        base = 5  # cfg.SESSION_BATCH_SIZE
        return max(2, base - getattr(self, "_batch_reduction", 0))
```

### 시간대 분산

```python
# 매일 같은 시간에 수집하면 주기적 패턴 감지에 걸린다.
# 스케줄러 사용 시 시작 시각에 지터를 추가한다.
import math

def next_run_delay_hours() -> float:
    """다음 실행까지의 지연 (시간). 로그노멀 + 지터."""
    base = random.lognormvariate(math.log(12), 0.3)  # 중앙값 12시간
    jitter = random.uniform(-2.0, 2.0)
    return max(4.0, base + jitter)  # 최소 4시간 간격
```

### 적용 위치

`IMPLEMENTATION_PLAN.md` §9.2 `BlockDetector` + `on_block_detected` 에 통합.
`behavior.py` 의 `batch_cooldown()` 이 `AdaptiveBackoff.cooldown_multiplier` 를
참조해 실제 대기 시간을 조정한다.

---

## 15. HTTP/2 프레임 시그니처 + 핑거프린트 일관성 검증 (rev.5 신규)

### 문제 1: HTTP/2 프레임 순서

`PLATFORM_ANALYSIS.md` 는 "HTTP/2 자동화 트래픽 비주기적 차단"을 보고한다.
Camoufox가 C++ 레벨에서 처리한다고 문서화되어 있지만, **실제 Firefox와
HTTP/2 SETTINGS 프레임 순서, WINDOW_UPDATE 타이밍, Priority 스킴이
일치하는지 실측 확인이 없다.**

### 문제 2: 핑거프린트 교차 검증

BrowserForge가 "실세계 통계 기반" 핑거프린트를 생성하지만:
- 한국 주거용 프록시 + WebGL Renderer "Apple M1 GPU" → **불일치 flag**
- 화면 2560x1440 + User-Agent 모바일 → 불일치
- `Accept-Language: ko-KR` + timezone `America/New_York` → 불일치

Akamai는 이 교차 일관성을 검사한다.

### 스파이크 검증 절차 (P0-3에 추가)

```
[HTTP/2 검증]
1. mitmproxy 또는 Wireshark로 Camoufox 트래픽 캡처
2. 실제 Firefox(수동 실행) 트래픽과 비교:
   - SETTINGS 프레임 파라미터 순서/값
   - HEADERS 프레임의 pseudo-header 순서 (:method, :path, :authority, :scheme)
   - Accept/Accept-Language/Accept-Encoding 헤더 순서
   - Sec-Fetch-* 헤더 존재 여부 + 값
3. 불일치 시: Camoufox 설정 또는 real_chrome=True 대안 검토

[핑거프린트 일관성 검증]
1. StealthySession으로 https://browserleaks.com/canvas 방문
2. 아래 값을 로깅:
   - Canvas hash
   - WebGL Renderer / Vendor
   - Screen resolution / Color depth
   - Timezone
   - Accept-Language
   - Platform (navigator.platform)
3. 확인: "한국 데스크톱 Firefox" 프로파일과 일치하는지
   - WebGL: Intel/NVIDIA/AMD (데스크톱 GPU) — Apple M1이면 안 됨
   - Timezone: Asia/Seoul
   - Platform: Win32 또는 Linux x86_64
   - Screen: 1920x1080 또는 2560x1440 (한국 주류)
4. 3회 반복해 세션마다 값이 달라지는지(회전) + 일관성은 유지되는지 확인

[헤더 순서 검증]
1. https://httpbin.org/headers 또는 로컬 mitmproxy로 요청 헤더 캡처
2. Firefox 기본 헤더 순서와 비교:
   Firefox: Host, User-Agent, Accept, Accept-Language, Accept-Encoding,
            Connection, Upgrade-Insecure-Requests, Sec-Fetch-*
3. 순서가 다르면 Akamai L1에서 비브라우저로 분류될 수 있음
```

### 실패 시 대안

| 문제 | 대안 |
|------|------|
| HTTP/2 프레임 불일치 | `real_chrome=True` (Chromium 스택) 전환 검토 |
| 핑거프린트 불일치 | BrowserForge 설정에 `os="windows"` 명시 (Scrapling 미지원 시 이슈 리포트) |
| 헤더 순서 불일치 | Camoufox upstream 이슈 — 수정 전까지 `DynamicSession` + 수동 stealth 고려 |
