# Coupang × Bright Data 적용 검토 — Akamai 실측 (2026-09-08)

> 질문: "Gmarket에서 통한 Bright Data 방식을 Coupang 카테고리 탭(patchright/
> Camoufox 방식)에 적용하면 차단 없이 수집량을 늘릴 수 있는가? 쿠팡 차단은 Akamai."
> 대상: 메인 앱(Camoufox 엔진) + `patchright-canary` 워크트리(Windows 예약잡 운영 중).
> 결론 요약: **Unlocker 이식은 불가(실증 실패). 하이브리드(Camoufox/patchright +
> Bright Data 한국 ISP IP)는 성공.** 아래 실측 근거.

## 1. 기본 상태 — 우리 IP는 Akamai에 하드 차단

```
GET https://www.coupang.com/np/categories/194276  (사무실 IP, curl)
→ 403, 397B
```

## 2. Web Unlocker 이식 시도 — 실패 (실증)

`gm_unlocker` 존, `country:"kr"`, 3개 URL:

| 대상 | 결과 (응답 헤더) |
|---|---|
| /np/categories/194276 | 200 래퍼 + **본문 0B**, `x-brd-error: captcha or protection page found` (`reject_block`, 대상 502) |
| /np/search?q=… | 동일 `reject_block` |
| coupang.com 홈 | `domcontentloaded_event_timeout` |

- Unlocker가 Akamai 보호 페이지를 감지하고 스스로 포기한 케이스 — Cloudflare 때와
  달리 **쿠팡의 Akamai 구성은 현재 Unlocker 스택이 못 뚫는다**.
- 본문이 비어 200으로 오므로, 앱에서 쓴다면 반드시 `x-brd-error` 헤더 확인 필요.

## 3. 하이브리드 — 성공 (실증)

**Camoufox(Firefox 위장, geoip=True) + Bright Data 한국 ISP 고정 IP
(31.40.194.114, `gm_isp_kr3` 존 프록시)**:

| 시도 | 결과 |
|---|---|
| 카테고리 194276 | 636KB, **productId 63개**, Akamai 흔적 없음 |
| ?page=2 (같은 세션) | 636KB, 63개, **1페이지와 교집합 0** — 페이지네이션 정상 |
| 재방문 검증 | 첫 방문부터 631KB / 59개 — 정상 |

- **간헐 이슈**: 내비게이션의 약 1/3 확률로 2.7~3.4KB "부트스트랩 셸"이 옴
  (차단 아님, JS 부트 페이지). → 대응 규칙: **본문이 비정상적으로 작으면
  대기 후 같은 URL 재내비게이션** (1~2회면 회복, 메인 앱의 웜업 패턴과 동일).
- Akamai 관점 해석: 쿠팡 Akamai는 "깨끗한 IP + 진짜 브라우저 지문" 조합을 통과시킴.
  IP 교체만(Unlocker)으로는 안 되고 **지문까지 진짜여야** 한다 — Gmarket(Cloudflare,
  IP 지배)과 정반대의 결론.

## 4. 대상별 적용 판정

| 대상 | 판정 |
|---|---|
| 카테고리 목록(Phase A goodscode) | ✅ 하이브리드로 가능 — Camoufox/patchright 유지 + ISP 프록시만 추가 |
| shop.coupang.com 판매자 API (canary의 in-page fetch) | ⚠️ 브라우저 세션 컨텍스트 필요(credentials include) — **Unlocker 대체 불가**, 브라우저 유지 필수 |
| canary 워크트리 | 프록시 추가만으로 적용 가능 (patchright `proxy` 옵션). 예약잡은 **건드리지 말 것** — 다음 예약 사이클부터 적용 검토 |
| Camoufox 적용 | 메인 앱은 이미 Camoufox. canary(patchright)에도 가능하나 Firefox 지문 다변화 목적 — A/B 후보 |

## 5. 운영 유의점

1. **회선 IP 보호가 시급**: canary 예약잡이 사무실/집 회선 IP를 그대로 쓰고 있으면
   Gmarket 때와 같은 **Akamai 번아웃**이 예약된 것과 다름 없다. → ISP 프록시 연결을
   우선 검토 (IP는 이미 확보: `gm_isp_kr3`).
2. **비용 구조가 다름**: ISP는 용량제. 페이지당 ~0.6MB → 1,000페이지 ≈ 0.6GB.
   ISP 요금 단가(GB당) 확인 후 예산 산정. (Gmarket Unlocker는 요청제 $0.003/건.)
3. **계정 세션 리스크**: 로그인 세션 프로필을 새 IP와 함께 쓰면 쿠팡 계정 보안
   경보 가능성 — 비로그인으로 되는 목록 수집부터 프록시를 붙이고, 로그인
   파이프라인(/np/omp)은 신중히 별도 검토.
4. blockguard/쿨다운 규율은 유지 — 프록시를 써도 셸·차단 감지·기록 로직은 그대로.

## 6. 다음 단계 (미실행)

1. canary `patchright_browser()`에 프록시 주입 옵션 추가 + 예약잡 1사이클 관찰
2. 셸 재내비게이션 규칙을 목록 수집에 반영 (작은 페이지 감지 → 재시도)
3. ISP 존 대역폭 단가 확인 → 페이지 수 기반 예산 수립

## 7. Akamai 특성·우회 리서치 + 엔진 비교 판정 (2026-09-08)

### 7.1 Akamai Bot Manager는 무엇을 보는가 (리서치 요약)

1. **TLS/JA3/JA4 지문(첫 관문)**: 핸드셰이크만으로 클라이언트를 판별 —
   주장하는 UA와 TLS 지문이 어긋나면 즉시 감점. 실제 브라우저의 TLS는 진짜라 통과.
2. **센서 데이터(`_abck` 쿠키)**: 페이지 내 bmak 스크립트가 마우스/키 타이밍,
   브라우저 환경 일관성, JS 우회 흔적을 수집 → 서버가 0~100 봇 점수 산출.
   JavaScript 우회식(스텔스 플러그인류)은 이 일관성 검사에 걸리기 쉬움.
3. **IP 평판**: 데이터센터/프록시 ASN 감점 — 단, IP 단독으로 막지 않고
   위 신호들과 **종합 점수**로 판정.
4. 결론: 쿠팡을 뚫으려면 "진짜 브라우저 지문 + 깨끗한 IP + 사람 같은 행동"의
   **세 밑변이 모두 필요** — Gmarket(Cloudflare, IP 지배적)과 정반대.

### 7.2 엔진 비교 — Camoufox vs patchright (Bright Data ISP 프록시 결합)

| 기준 | Camoufox (Firefox 변형) | patchright (Chromium 변형) |
|---|---|---|
| 우회 방식 | C++ 레벨 패치 (JS 우회 흔적 없음) | Playwright 자동화 플래그/CDL 누출 패치 |
| Akamai 알려진 이슈 | 별도 감지 보고 없음, Akamai 대응 설계 언급 | **"Detected by Akamai" 공식 이슈 존재** (patchright #108) |
| 프록시 일관성 | `geoip=True` → IP에 맞춰 locale/타임존/지리 **자동 동기화** | 수동 구성 필요 (놓치면 감점 요소) |
| 쿠팡 실측 (오늘) | ✅ 통과 (63개 상품, 페이지네이션) | 미실측 (canary는 기존 IP로 가드 램프 내 동작 중) |
| 지문 풀 | Firefox 계열 — 자동화 도구 연관 낮음 | Chromium+Playwright — 표적화가 가장 많은 조합 |

### 7.3 판정 — **Camoufox + Bright Data ISP 프록시** 권장

1. 오늘의 실증이 정확히 이 조합(쿠팡 통과)이라 재현 리스크가 없다.
2. Akamai 센서가 사냥하는 "JS 우회 흔적"이 구조적으로 없다(C++ 패치).
3. `geoip=True`의 자동 일관성(Akamai가 교차 검증하는 신호)이 공짜로 확보된다.
4. patchright는 Akamai 감지 보고(#108)가 있고, 프록시 일관성 수동 구성 부담.
   단 canary는 patchright로 현역 운영 중이므로 **즉시 전환 금지** — 관찰 유지.

### 7.4 유의점

- canary의 영구 프로필에는 쿠팡 로그인 세션이 있음 — 새 IP + 기존 세션 조합은
  계정 보안 경보 가능성. 비로그인 목록 수집부터 프록시를 붙일 것.
- 어느 엔진이든 Akamai는 행동 점수를 매기므로, 프록시를 붙여도 램프(단계적
  상한)·딜레이·blockguard 규율은 그대로 유지해야 한다.
- Sources: Scrapfly(Akamai _abck/센서), crawlex(봇 점수), gtfo.dev(JA3/JA4),
  Camoufox 공식 stealth 문서, patchright GitHub 이슈 #108, ZenRows(Playwright×Akamai).

## 8. 실행 기록 — 과일 남은 카테고리 수집 완료 (2026-09-09 00:25)

> §3·§7의 하이브리드(Camoufox + `gm_isp_kr3` 프록시)를 실제 수집에 적용.
> canary 히스토리(`PATCHRIGHT_FULL_FRUIT_COLLECTION_PLAN.md`)·상태 파일
> (`fruit_collection_state.json`, Windows PatchrightFruit)로 남은 대상 확정:
> 194337(9페이지부터 이어서) + 194358 + 194368. 194337의 1~8페이지는 canary가
> 이미 수집(`fruit_products.csv`).

| 남은 카테고리 | 탐색 | 수집 |
|---|---|---|
| 망고/체리/아보카도/기타 (194337) | 9→19p (11p) | **346** |
| 냉동과일/간편과일 (194358) | 1→19p | **851** |
| 과일선물세트 (194368) | 1→12p | **597** |
| **합계 (고유)** | | **1,794** |

- 차단(Access Denied/Reference) 0건. 셸 간헐 발생 → 재내비게이션 규칙으로 흡수
  (과일선물세트 p11~12는 셸 지속 → 실제 목록 끝으로 판정, 471B 반복).
- 냉동과일은 19페이지까지 유효(신규 60개/페이지) — 예전 실측의 "SSR 17페이지
  상한"은 더 이상 유효하지 않음. 종료 판정은 "신규 0개 연속 2회"로 충분했다.
- 저장: `output/coupang_fruit_remaining_20260909_001332/`
  (`fruit_products_remaining.json/.csv` 통합 + 카테고리별 파일, summary.json)
- **Windows 예약잡(PatchrightFruitPipeline-80Min, ~09-10 12:47) 처리 필요**:
  남은 범위(194337 잔여·194358·194368)를 본 실행이 모두 수집했으므로, canary의
  남은 예약은 중복 수집이 된다 → 사용자 승인 후 예약 취소 권장. 판매자 연결
  단계(7,899건 완료)에 신규 1,794건 연장은 canary 파이프라인의 후속 작업으로 남는다.

## 9. 후속 완료 — 신규 1,794건 판매자 연결·사업자정보 (2026-09-09 01:31)

canary 파이프라인(patchright_full_sellers.py)을 Camoufox로 복제해 실행
(`scratchpad/coupang_fruit_sellers.py`). 배치 10건 매핑 → 신규 판매자
getStoreReview → fruit_sellers.csv / fruit_product_seller.csv 저장.

| 항목 | 값 |
|---|---|
| 상품-판매자 연결 | **1,794 / 1,794 (100%)**, 실패 매핑 0 |
| 고유 판매자 | **514명 — 전원 saved** (no_public_info 0, failed 0) |
| 필드 채움률 | company/ceo/email/phone 513·business_number 513·address 514 (99.8~100%) |
| 소요 | 43.7분 (사무실 회선 IP, 요청 간 3초) |

### 신규 발견 — getStoreReview는 IP 평판 게이트

- 매핑 API(`individualInfo`): 프록시 IP·사무실 IP 모두 200.
- **판매자정보 API(`getStoreReview`): 프록시 ISP IP에서 403 ("Access Denied") —
  행동 시뮬레이션·IP 리프레시(103.198.254.224로 교체) 후에도 동일.**
- 사무실 회선 IP + 새 Camoufox 세션: 200 정상.
- → Akamai가 엔드포인트별로 다른 규칙을 둔다. **canary가 사무실 IP에서 2차를
  돌리던 이유가 이것** — 1차(목록)는 프록시 OK, 2차(판매자 API)는 사무실 IP 필수.
- 운영 결론: Coupang 2단계는 IP를 섞어 쓴다 — 1차(목록)는 Bright Data ISP IP
  (회선 IP 보호), 2차(판매자 API)는 회선 IP (프록시 불가). 두 단계를 하나의
  IP로 묶으려면 전용 컨택 지문·성숙 쿠키 프로필이 필요 (미검증).

### 비용 (CLI `budget zones` 실측)

| 구역 | 비용 | 대역폭 |
|---|---|---|
| gm_unlocker | $3.79 | 17.7 GB |
| gm_isp_kr3 | **$2.32** | 268.3 MB |
| 합계 | **$6.11** | 잔액 $5.84 |

- 이번 과일 전체 작업(목록 20p + 판매자 1,794건) 증분 ≈ **$2.20**.
- 주의: 브라우저 방식은 페이지에 이미지·리소스까지 실려 **페이지당 ~12MB** —
  Unlocker(요청제 $0.003)보다 단가가 수십 배 높다. 쿠팡은 Unlocker 불가라
  어쩔 수 없으나, 대량 확대 시 `route` 차단(이미지/폰트) 등 대역폭 절감 필요.
- Windows 예약잡(PatchrightFruitPipeline-80Min)은 이미 Disabled 상태로 확인
  (충돌 없음). 재개 여부는 사용자 결정.

## 10. 앱 반영 완료 — 실측 규칙의 엔진 구현 (2026-09-09)

> §3·§8·§9 의 scratchpad 실측(coupang_fruit_remaining / coupang_fruit_sellers)
> 규칙을 메인 앱의 Coupang 수집 엔진에 반영했다. 설정 입력은
> [BRIGHTDATA_ACCOUNT_SETTINGS.md](../BRIGHTDATA_ACCOUNT_SETTINGS.md) 의
> "설정" 탭(output/brightdata_settings.json / 환경변수)을 사용한다.

| 실측 규칙 | 앱 구현 |
|---|---|
| 1차 목록 = Camoufox + ISP 프록시 (§3·§7.3) | 설정 탭에서 ISP 프록시 활성화 → `SearchRunConfig.proxy` 주입 → `Camoufox(geoip=True, proxy=...)` |
| 작은 페이지(셸) → 재내비게이션 (§3) | `_load_listing_page`: 상품 0건 + 본문 10KB 미만 → 대기 후 같은 URL 재내비게이션 최대 2회 (`PLP_SHELL_HTML_BYTES`, `SHELL_RENAVIGATE_MAX`) |
| 셸 지속 = 목록 끝 판정 (§8, 471B 반복) | 재내비게이션 소수 후에도 셸이면 빈 페이지 반환 — 기존 빈 페이지 연속 2회 종료 판정과 연동 |
| 2차 판매자정보 = 회선 IP 필수 (§9) | 2차 직전 `_switch_to_direct_session()` — 프록시 세션을 닫고 `Camoufox(proxy=None, geoip=True)` 새 세션으로 홈 웜업 후 `getStoreReview` 진행 (scratchpad 2-pass 방식) |
| 키워드 차단 즉시 중단 유지 | `is_blocked` 의 소프트 블록(크기) 규칙 대신 `keyword_block_reason`(키워드 전용) 로 하드 스톱 판정 — 작은 응답은 셸 규칙이 흡수 |

- 세션 교체를 지원하도록 `CoupangCrawler.run()` 이 마지막 세션을 추적해
  finally 정리한다(잔류 프로세스 방지 유지). 프록시 미설정 시 동작은
  기존과 완전히 동일하다(단일 세션 — 회귀 없음).
- 검증: 전체 자동 테스트 432 passed (셸 4건 + 세션 분리 3건 + 프록시 인자
  1건 신규), ruff/mypy 베이스라인 정합.
- 라이브 확인은 미실행 — 배포 환경에서 실제 프록시 키로 1차→2차 전환 1사이클
  관찰이 남아 있다(비용은 입력 계정 키에서 차감).
