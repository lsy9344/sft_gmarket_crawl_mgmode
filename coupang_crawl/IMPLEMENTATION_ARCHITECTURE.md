# Coupang OMP - Implementation Architecture

> 최종 개정: 2026-07-25 (rev.5)
> **rev.5 — 사업자정보 위치 정정.** 필수 수집 항목(상호명·대표자명·
> 사업자등록번호·주소·전화)은 Coupang 상품 **상세페이지 최하단
> 사업자정보 란**에 전부 존재한다. 통신판매업신고번호는 수집 대상이
> 아니다. Gmarket이 `mg.gmarket.co.kr` 를 직접 읽듯, Coupang도
> 자신의 상세페이지 최하단을 직접 읽는다 — 외부 정부 DB 조회·매칭
> 계층은 필요 없으며 rev.4에서 완전히 제거했다.
>
> **확정 실행 계획은 `IMPLEMENTATION_PLAN.md` 를 따른다.** 본 문서는 그
> 구조적 근거를 담는다.

---

## 0. rev.4 에서 바뀐 전제

| 이전 전제 | 실제 | 대응 |
|-----------|------|------|
| 사업자정보는 공정위 DB 매칭으로 확보 | **Coupang 자체 상세페이지에 이미 존재** — 매칭 불필요 | 상세페이지 직접 스크래핑을 유일한 경로로 채택 |
| 상세 수집은 공정위 미매칭 판매자만 대상 | 모든 판매자가 상세페이지를 거쳐야 함 | Stage C(상세 스크래핑)가 D2 축약 이후 **전체** 판매자를 대상으로 |
| SQLite+FTS5 퍼지 매칭 엔진 필요 | 매칭 자체가 없으므로 불필요 | `ftc_db.py`/`ftc_matcher.py` 모듈 **삭제** |
| `thefuzz`/`python-Levenshtein` 의존성 필요 | 불필요 | requirements 에서 제거 |
| `storage.py` 를 공용으로 재사용 | CSV writer가 Gmarket 11필드 고정(`storage.py:487`) → Coupang 필드 소실 | `PlatformProfile` 주입 (변경 없음, §2) |
| 상품 단위로 상세 수집 | 같은 판매자 상세를 수십 번 반복 | 판매자 단위 축약 (변경 없음, §3) |
| `time.sleep(cooldown)` | [취소]가 최대 30분 먹통 + 좀비 스레드 | `Control.sleep()` (변경 없음, §5) |
| 상세 실패 시 공정위로 폴백 | **대체 경로 없음** — 재시도 후 미확정 분리 | §4, §5.3 |

---

## 1. 모듈 구조 (rev.5)

```
app/
├── core/
│   ├── schema.py                [NEW] PlatformProfile / GMARKET_·COUPANG_PROFILE
│   ├── base.py                  [MOD] Control.sleep() 추가 (취소 반응형 대기)
│   ├── storage.py               [MOD] profile 주입 (§2)
│   ├── crawler.py               [MOD] goodscode 하드코딩 3곳 → profile 참조
│   └── coupang/                 [NEW] 패키지
│       ├── config.py            설정 상수 + CoupangSettings 로더
│       ├── budget.py            일일 예산 영속화
│       ├── block.py             BlockDetector / BlockedError
│       ├── behavior.py          page_action 콜백 + 지연 프로파일 (L4) + warmup_interact (rev.5)
│       ├── randomizer.py        [NEW rev.5] SessionRandomizer — 세션 간 메타패턴 랜덤화
│       ├── backoff.py           [NEW rev.5] AdaptiveBackoff — 차단 이력 기반 점진적 완화
│       ├── health.py            [NEW rev.5] ParserHealthMonitor — XHR/DOM 성공률 추적
│       ├── proxy.py             프록시 공급자 (ProxyRotator 래퍼, L5) + canary 검증 (rev.5)
│       ├── parser.py            리스팅 파서 + 판매자 상세 파서(XHR/DOM)
│       ├── listing.py           Stage A 엔진
│       ├── sellers.py           판매자 프로필 캐시 (D2)
│       ├── dedup.py             D3 사업자번호 정규화·검증·병합
│       ├── enrich.py            Stage C 엔진 — 판매자 상세 직접 스크래핑
│       └── pipeline.py          Stage 오케스트레이션 + Stage D 집계/출력
├── models/
│   └── coupang_records.py       [NEW] CoupangProduct / SellerProfile / BusinessRecord
├── workers/
│   ├── coupang_scan_worker.py   [NEW] Stage A
│   └── coupang_enrich_worker.py [NEW] Stage B+C
└── ui/
    ├── main_window.py           [MOD] QTabWidget (Gmarket / Coupang)
    └── coupang/                 [NEW] panel / settings / stage_bar / seller_table
```

> **rev.4 삭제**: `ftc_db.py`, `ftc_matcher.py`, `ftc_build_worker.py`.
> 공정위 연동이 없으므로 이 세 모듈은 존재할 이유가 없다.

**설계 제약 (기존 코드베이스 관례 계승)**

- 엔진(`core/`)은 **Qt 비의존** — `Control` + 콜백만 사용
- `scrapling` 은 **함수 내부 지연 import** (`prescan.py:66` 패턴)
- 워커는 예외를 경계에서 전부 포착해 시그널로 중계 (`crawl_worker.py:75`)
- `app/core/crawler.py` 는 Coupang 모듈을 **import 하지 않는다** (장애 격리)

---

## 2. Storage 파라미터화 (선행 조건, 변경 없음)

`Storage` 는 6차 리뷰를 거친 내구성 로직(fail-closed 승격, 원자적 쓰기,
손상 격리, manifest 검증)을 담고 있다. **재작성하지 않고 스키마만 주입한다.**

```python
# app/core/schema.py
@dataclass(frozen=True)
class PlatformProfile:
    key: str                          # "gmarket" | "coupang"
    file_prefix: str                  # 출력/체크포인트 접두사
    id_field: str                     # 체크포인트 검증 + 중복 제거 키
    record_fields: tuple[str, ...]    # CSV 헤더 순서
    collected_ids_filename: str
    state_filename: str
    promoted_manifest_filename: str
    lock_filename: str
    all_label: str = "ALL"
```

`Storage(output_dir, profile=GMARKET_PROFILE)` — 기본값이 현재 상수와
동일하므로 **Gmarket 경로는 무변화**다. 치환 대상은
`IMPLEMENTATION_PLAN.md` §7.2 표에 파일·행 단위로 명세돼 있다.

---

## 3. 데이터 모델 (rev.4)

```python
# app/models/coupang_records.py

@dataclass
class CoupangProduct:
    """Stage A 산출 — 리스팅 원본 (상품 단위)."""
    product_id: str
    item_id: str = ""
    vendor_item_id: str = ""
    url: str = ""
    title: str = ""
    store_name: str = ""
    price: int = 0
    rating: float = 0.0
    review_count: int = 0
    rocket_delivery: bool = False
    power_seller: bool = False          # 리스팅 "파워셀러" 배지 (확보 시)
    seller_grade: str = ""              # 배지 원문 보존
    source: str = ""
    scanned_at: str = ""


@dataclass
class SellerProfile:
    """Stage B/C 산출 — 판매자 1명 (D2 축약 단위).

    rev.4: 사업자정보는 오직 상세페이지 직접 스크래핑에서만 채워진다.
    match_method/match_score 같은 매칭 신뢰도 필드는 없다 — 확보했거나
    (그라운드 트루스) 못 했거나 둘 중 하나다.
    """
    seller_key: str                     # 정규화 store_name (조인 키)
    store_name: str = ""
    company_name: str = ""
    ceo_name: str = ""
    phone: str = ""
    business_number: str = ""           # 정규화 10자리 (체크섬 통과분만)
    address: str = ""
    power_seller: bool = False
    seller_grade: str = ""
    resolved: bool = False              # 상세페이지에서 사업자정보를 확보했는지
    parse_method: str = "none"          # dom | xhr | none — 운영 모니터링용
    resolved_at: str = ""

    @property
    def is_resolved(self) -> bool:
        return bool(self.business_number)


@dataclass
class BusinessRecord:
    """Stage D 최종 출력 1행 — 사업자 단위 (D3 이후)."""
    business_number: str                # ← 유일성 키
    company_name: str = ""
    ceo_name: str = ""
    email: str = ""                     # 항상 "" (Coupang 미공개)
    phone: str = ""
    address: str = ""
    power_seller: bool = False          # 병합 시 OR
    seller_grade: str = ""              # 관측 등급 ";" 결합
    store_names: str = ""               # 다중 스토어 ";" 결합
    store_count: int = 0
    product_count: int = 0
    sample_product_id: str = ""
    sample_url: str = ""
    source: str = ""
    collected_at: str = ""

    def to_dict(self) -> dict: ...
    def is_valid(self) -> bool:
        return bool(self.business_number)
```

필드 순서/의미는 `DATA_FIELDS_MAPPING.md` §6 이 단일 진실 공급원이다.

> **rev.4 제거**: `domain`, `business_status`, `handled_products`
> (공정위 전용 필드, 소스 소멸로 제거), `match_method`, `match_score`,
> `match_candidates`, `ftc_enriched` (매칭 알고리즘 자체가 없음).

---

## 4. 중복 제거 모듈 (변경 없음, 입력 소스만 단순화)

```python
# app/core/coupang/dedup.py

def normalize_brno(s: str) -> str:
    """숫자 10자리 정규 키. 실패 시 빈 문자열."""

def is_valid_brno(brno: str) -> bool:
    """국세청 체크섬 검증 — 실패 시 중복 제거 키로 쓰지 않는다.

    출처가 상세페이지 직접 스크래핑뿐이더라도 이 검증은 필수다 —
    DOM 오인식으로 잘못된 문자열을 뽑을 수 있기 때문이다.
    """

def normalize_seller_key(store_name: str) -> str:
    """(주)/주식회사/공백/문장부호 제거 + NFKC + casefold."""

def dedupe_by_business_number(
    profiles: list[SellerProfile],
    products_by_seller: dict[str, list[CoupangProduct]],
) -> tuple[list[BusinessRecord], list[SellerProfile]]:
    """D3 실행.

    반환: (사업자번호 확정 + 병합된 레코드, 번호 미확정 판매자)
    - 대표 선택: 필드 충족도 → 수집순 (rev.4: 소스가 단일하므로
      rev.2의 method 우선순위는 제거)
    - 병합: store_names/store_count/product_count, power_seller = OR
    """


class BusinessIndex:
    """실행 간 영속 중복 제거 — coupang_business_index.json.

    seller_key → business_number 역인덱스를 함께 유지해, 고비용 구간
    진입 *이전에* '이미 확보한 사업자'를 걸러낸다.
    """
    def known(self, business_number: str) -> bool: ...
    def seller_known(self, seller_key: str) -> bool: ...
    def add(self, record: BusinessRecord) -> None: ...
    def save(self) -> None: ...        # 원자적 쓰기 (tmp → replace)
```

---

## 5. 파이프라인 의사코드 (rev.5)

### 5.1 Stage A — 리스팅 스캔

```python
def scan_listing(self, plan: ListingPlan) -> ListingResult:
    from scrapling.fetchers import StealthySession        # 지연 import

    if not self.proxy.is_configured() and cfg.PROXY_REQUIRED:
        raise ProxyNotConfiguredError()                   # fail-closed

    detector = BlockDetector(limit=cfg.ABORT_AFTER_BLOCKS)
    backoff = AdaptiveBackoff(self.budget, self.control)  # rev.5
    rand = SessionRandomizer()                            # rev.5
    products: list[CoupangProduct] = []

    with StealthySession(
        headless=True,
        proxy=self.proxy.next(),        # 단수 · 세션 레벨 (canary 검증됨)
        dns_over_https=True,
        block_ads=True,
        solve_cloudflare=True,
        timeout=cfg.FETCH_TIMEOUT_MS,
    ) as session:
        session.fetch(cfg.HOME_URL, network_idle=True)    # _abck 웜업
        warmup_interact(session.current_page, self.control)  # rev.5: 센서 텔레메트리 주입
        self.behavior.warmup_delay()

        rand.maybe_decoy_visit(session, self.control)     # rev.5: 30% 확률 무관 페이지 경유

        for page_no in plan.pages:
            self.control.checkpoint()
            if self.budget.listing_exhausted():
                self.on_log("[예산] 일일 리스팅 한도 도달 — 중단"); break

            r = session.fetch(plan.url(page_no, sorter=rand.sorter()),
                              network_idle=True, google_search=False)
            self.budget.count_listing(); self.budget.save()   # 실패도 차감

            if detector.check(r):
                backoff.on_block({"stage": "A", "page_no": page_no})  # rev.5
                self.proxy.blacklist(session.proxy)                   # rev.5
                if detector.should_abort():
                    raise BlockedError(detector.reason)
                self.behavior.block_recovery(backoff.cooldown_multiplier)
                continue

            products.extend(parse_listing(r, plan.source_label))
            self.on_page(page_no, len(products))
            self.behavior.page_delay()

    return ListingResult(products, detector.stats)
```

### 5.2 Stage B — 판매자 축약 (네트워크 0)

```python
def reduce_to_sellers(self, products: list[CoupangProduct]) -> list[SellerTarget]:
    """D2: 상품 → 판매자 축약. 매칭 없음 — 단순 group-by + 인덱스 조회."""
    by_seller: dict[str, list[CoupangProduct]] = defaultdict(list)
    for p in products:
        key = normalize_seller_key(p.store_name)
        if key:
            by_seller[key].append(p)

    targets: list[SellerTarget] = []
    for seller_key, items in by_seller.items():
        self.control.checkpoint()

        # 이미 확보한 사업자면 즉시 스킵 — 프록시 비용 0
        if self.index.seller_known(seller_key):
            continue
        cached = self.sellers.get(seller_key)
        if cached and cached.is_resolved:
            continue

        targets.append(SellerTarget(
            seller_key=seller_key,
            store_name=items[0].store_name,
            sample_url=items[0].url,
            power_seller=any(i.power_seller for i in items),
            seller_grade=";".join(sorted({i.seller_grade for i in items if i.seller_grade})),
        ))
    return targets
```

### 5.3 Stage C — 판매자 상세 직접 스크래핑 (사업자정보 유일 경로)

```python
def enrich(self, targets: list[SellerTarget]) -> EnrichSummary:
    """rev.5: targets 는 Stage B 로 축약된 '전체' 판매자다.
    공정위 폴백이 없으므로, 여기서 실패한 판매자는 미확정으로 남는다.
    """
    detector = BlockDetector(limit=cfg.ABORT_AFTER_BLOCKS)
    backoff = AdaptiveBackoff(self.budget, self.control)  # rev.5
    rand = SessionRandomizer()                            # rev.5
    health = ParserHealthMonitor()                        # rev.5

    for batch_no, batch in enumerate(
        chunked(targets, backoff.effective_batch_size), 1  # rev.5: 동적 배치
    ):
        self.control.checkpoint()
        if self.budget.detail_exhausted():
            self.on_log("[예산] 일일 상세 한도 — 다음 실행에서 계속"); break
        if batch_no > 1:
            self.behavior.batch_cooldown(backoff.cooldown_multiplier)  # rev.5

        with StealthySession(
            headless=True,
            proxy=self.proxy.next(),                # 세션마다 IP 교체 (canary 검증됨)
            capture_xhr=cfg.SELLER_XHR_PATTERN,     # 1순위 경로
            page_action=behavior.scroll_to_seller_section,
            dns_over_https=True, block_ads=True, solve_cloudflare=True,
        ) as session:
            session.fetch(cfg.HOME_URL, network_idle=True)
            warmup_interact(session.current_page, self.control)  # rev.5
            self.behavior.warmup_delay()
            rand.maybe_decoy_visit(session, self.control)        # rev.5

            for target in batch:
                self.control.checkpoint()
                r = session.fetch(target.sample_url, network_idle=True)
                self.budget.count_detail(); self.budget.save()

                if detector.check(r):
                    backoff.on_block({"stage": "C", "seller": target.seller_key})  # rev.5
                    self.proxy.blacklist(session.proxy)                            # rev.5
                    if detector.should_abort():
                        raise BlockedError(detector.reason)
                    break                            # 세션 폐기 → 다음 배치

                profile, method = self._parse_seller(r)  # xhr 우선, dom 폴백
                health.record(method, profile is not None)  # rev.5
                if profile is None:
                    continue                          # 재시도는 상위 루프에서

                brno = normalize_brno(profile.business_number)
                profile.business_number = brno if is_valid_brno(brno) else ""
                profile.resolved = bool(profile.business_number)
                profile.parse_method = method

                # 결과를 먼저 durable 저장 (기존 내구성 프로토콜 계승)
                self.sellers.upsert(target.seller_key, profile)
                self.sellers.save()
                self.behavior.item_delay()

    if health.is_degraded():                          # rev.5
        self.on_log("[경고] XHR 성공률 저하 — 셀렉터/XHR 변경 가능성. 스파이크 재실행 권장")

    return EnrichSummary(...)

def _parse_seller(self, response) -> tuple[SellerProfile | None, str]:
    if (p := parse_seller_from_xhr(response.captured_xhr)):
        return p, "xhr"
    if (p := parse_seller_from_dom(response.html_content)):
        return p, "dom"
    return None, "none"
```

### 5.4 Stage D — D3 중복 제거 + 출력

```python
def export(self, label: str) -> ExportResult:
    profiles = self.sellers.all()
    by_seller = self.listing.group_by_seller()

    businesses, unresolved = dedupe_by_business_number(profiles, by_seller)

    # 실행 간 중복 제거: 이미 출력한 사업자는 제외
    fresh = [b for b in businesses if not self.index.known(b.business_number)]

    # 결과를 먼저 저장한 뒤에만 인덱스를 커밋 (기존 내구성 순서)
    paths = self.storage.save_results(
        [b.to_dict() for b in fresh], label=label
    )
    for b in fresh:
        self.index.add(b)
    self.index.save()

    unresolved_paths = self.storage.save_results(
        [p.to_dict() for p in unresolved], label=f"{label}_unresolved"
    )
    return ExportResult(paths, unresolved_paths, len(fresh), len(unresolved))
```

---

## 6. 설정 (`app/core/coupang/config.py`, rev.4)

```python
# URLs
COUPANG_BASE       = "https://www.coupang.com"
HOME_URL           = COUPANG_BASE
OMP_LISTING_URL    = f"{COUPANG_BASE}/np/omp"
CATEGORY_URL       = f"{COUPANG_BASE}/np/categories/{{category_id}}"   # robots 허용
SEARCH_URL         = f"{COUPANG_BASE}/np/search"                       # robots 허용
PRODUCT_DETAIL_URL = f"{COUPANG_BASE}/vp/products/{{product_id}}"

LIST_SIZE    = 120
SORT_OPTIONS = ["bestAsc", "priceAsc", "priceDesc", "rateDesc"]

# 타이밍 (Akamai 대응 — 전부 취소 반응형 Control.sleep 으로 대기)
WARMUP_WAIT        = (8.0, 15.0)      # _abck 센서(70KB) 실행
PAGE_DELAY         = (8.0, 15.0)
ITEM_DELAY         = (5.0, 15.0)
SESSION_BATCH_SIZE = 5
SESSION_COOLDOWN   = (600.0, 900.0)
BLOCK_COOLDOWN     = (900.0, 1800.0)
ABORT_AFTER_BLOCKS = 2
FETCH_TIMEOUT_MS   = 30000
DETAIL_RETRY_LIMIT = 2                # 상세 파싱 실패 시 재시도 (대체 경로 없으므로 중요)

# 일일 예산 (디스크 영속 — 앱 재시작에도 유지)
MAX_DAILY_LISTING_FETCHES = 200
MAX_DAILY_DETAIL_FETCHES  = 500

# 차단 감지
BOT_KEYWORDS = ["자동화된 테스트 소프트웨어", "접근이 제한", "보안 절차",
                "확인 절차", "잠시 후 다시 시도"]
BLOCK_STATUS_CODES   = (403, 418, 429)
SOFT_BLOCK_MIN_BYTES = 1000

# 프록시 (L5) — 파이프라인 전체가 이에 의존한다 (rev.4)
PROXY_REQUIRED = True          # 미설정 시 Stage A/C 실행 거부
PROXY_COUNTRY  = "KR"
PROXY_TYPE     = "residential"

# robots.txt
RESPECT_ROBOTS = True          # True 면 /np/categories/, /np/search 우선

# 셀렉터 — ⚠ 전부 미검증. 스파이크 확정 전 사용 금지 (PAGE_STRUCTURE.md §7)
LISTING_SELECTORS = {...}
SELLER_XHR_PATTERN = ""        # 스파이크에서 확정
```

> **rev.4 제거**: `FTC_API_BASE`, `FTC_BASIC_ENDPOINT`, `FTC_DETAIL_ENDPOINT`,
> `FTC_SERVICE_KEY`, `FTC_DAILY_LIMIT`, `MATCH_THRESHOLD`, `AMBIGUITY_DELTA`
> 전부 삭제. Gmarket 의 `config.BOT_KEYWORDS`(`app/core/config.py:74`)와
> 별도 목록임은 유지 — 키워드 집합이 다르고 soft-block 규칙이 추가된다.

---

## 7. 의존성

```
# 추가 의존성 없음 — scrapling[fetchers]>=0.4.11 / PyQt6 는 이미
# requirements.txt 에 포함되어 있고, 그 외 신규 라이브러리가 필요 없다.
```

> **rev.4 제거**: `thefuzz>=0.22.0`, `python-Levenshtein>=0.25`,
> SQLite FTS5 의존. 퍼지 매칭 자체가 사라졌으므로 불필요하다.
> `pyinstaller.spec` 의 `hiddenimports` 에도 추가할 항목이 없다.

---

## 8. 리스크 (rev.5 재평가)

| Risk | Level | Mitigation |
|------|-------|-----------|
| **상세 셀렉터/XHR 미발견** | **CRITICAL** | 스파이크(P0-3)를 선행 게이트로. **rev.4에서는 실패 시 대체 경로가 없다** — 유료 API 또는 프로젝트 재검토(§17-Q1) |
| Akamai 탐지 → IP 차단 | HIGH | 주거용 프록시 세션 회전, 차단 2회 시 중단 |
| 상세 파싱 실패율 | HIGH | XHR 우선 + DOM 폴백 + 재시도(`DETAIL_RETRY_LIMIT`). **폴백 경로가 없으므로 실패율이 곧 커버리지 손실** |
| 프록시 미확보 | **BLOCKING** | Stage A/C 모두 필수 — 파이프라인 전체가 성립하지 않음 |
| DOM 오인식으로 잘못된 사업자번호 추출 | 중 | 정규화 + 국세청 체크섬 검증 필수 |
| Storage 파라미터화가 내구성 로직 훼손 | 낮음 | 기계적 치환만 허용 + 기존 테스트 무수정 통과 |
| 파워셀러 배지 미발견 | 낮음 | 빈 값 + 경고 1회 — 파이프라인 중단 없음 (rev.4: 상세 방문이 전체 커버되므로 영향 더 작음) |
| 법적 (robots/ToS) | 중 | 허용 경로 우선, OMP 직접 스캔은 명시적 옵트인 |
| 셀렉터 변경 | 중 | XHR 우선 + `adaptive=True` + 픽스처 회귀 테스트 |
| `_abck` 세션 귀속 | 중 | 배치마다 신규 세션 (쿠키 재사용 불가) |
| **웜업 텔레메트리 공백 (rev.5)** | **HIGH** | `warmup_interact()` 주입 — 마우스/키보드/클릭 이벤트 생성. P0-4a 검증 |
| **HTTP/2·헤더 순서 불일치 (rev.5)** | **HIGH** | P0-4b mitmproxy 실측. 불일치 시 `real_chrome=True` |
| **세션 간 메타패턴 탐지 (rev.5)** | 중 | `SessionRandomizer` — 배치·정렬·타이밍·decoy 랜덤화 |
| **프록시 풀 오염 (rev.5)** | 중 | canary 검증 + blacklist + 공급자 다변화 |
| **파서 변경 미감지 (rev.5)** | 중 | `ParserHealthMonitor` — XHR 성공률 < 50% 시 자동 경고 |

> **rev.4 제거된 리스크**: "FTC 매칭률이 기대 이하", "오매칭이 출력에
> 혼입", "FTS5 미지원 SQLite 빌드" — 매칭 알고리즘 자체가 없으므로
> 해당 없음.
