# Gmarket 데이터 수집 접근 경로 리서치 (2026-09-08)

> 목적: `VERIFICATION_CATEGORY_TAB_20260908.md`의 판정(IP 단계 차단)을 전제로,
> **www 없이(또는 www 차단 시) 상품 목록을 얻을 수 있는 경로**를 실측·분석한다.
> 실측 환경: 개발 머신 — 공인 IP `118.36.49.213` (KR / KT / AS4766, 경기 안양 측정,
> hosting/proxy 플래그 없음). **기존 기록의 "데이터센터/해외 네트워크" 전제와 불일치 — §5 참조.**
> 관련 문서: [`CATEGORY_RESEARCH.md`](CATEGORY_RESEARCH.md) §2.4, [`VERIFICATION_CATEGORY_TAB_20260908.md`](VERIFICATION_CATEGORY_TAB_20260908.md)

---

## 1. 요약 (한눈에)

| 경로 | 상태 | 국내 카탈로그 | 자동화 난이도 | 판정 |
|---|---|---|---|---|
| `www.gmarket.co.kr/n/list` (주 경로, StealthySession) | ❌ 403 (이 네트워크) | ✅ 한글 PC | 브라우저 | **유지** — 배포 환경(한국 가정용 IP)에서만 검증 가능 |
| `mg.gmarket.co.kr/Category/List` + `POST /Search/SearchJson` | ✅ 200 (Cloudflare 없음) | ❌ **글로벌(국제배송) 한정** | 순수 HTTP+JSON, 매우 쉬움 | **폴백 전용** — 커버리지가 요구사항과 다름 |
| `m.gmarket.co.kr` (국내 모바일) | ❌ 403 (Cloudflare) | ✅ | 브라우저 | www와 동일 보호 — 우회 가치 없음 |
| `item.gmarket.co.kr` (상품 상세) | ❌ 403 (Cloudflare) | ✅ | 브라우저 | Phase B는 `mg`만 쓰므로 영향 없음 |
| 공식 Open API (`etapi.gmarket.com`) | — | ✅ | 판매자 자격 필요 | 부적합 — ESM+ 판매자 로그인·승인 필요 (판매자 상품 관리용) |
| 외부 스크래핑 서비스 (Apify 등) | — | ✅ | 유료·외부 의존 | 채택 안 함 — 자체 포함 설계와 상충 |
| Camoufox 전환 | — | — | — | 기존 판정 유지 — IP 차단이라 엔진 교체 무의미 |

**결론**: 국내 PC 한글 카탈로그를 얻는 유일한 경로는 기존 계획대로
`www` + StealthySession + 한국 가정용 IP(배포 환경)다. 이 네트워크에서 바꿀 수 있는
것은 없다. 다만 **"그래도 막힐 때의 폴백"** 으로 `mg` SearchJson 경로의 정확한 스펙을
확정했다(§2) — 폴백은 "글로벌 배송 상품 한정"임을 UI에 명시해야 한다.

## 2. 폴백 후보 상세 — `mg.gmarket.co.kr` 검색 API (신규 실측)

### 2.1 엔드포인트

```
GET  https://mg.gmarket.co.kr/Category/List?lcId={중분류코드}   # HTML 껍데기 (AJAX 파라미터 노출)
POST https://mg.gmarket.co.kr/Search/SearchJson                 # 상품 목록 JSON
```

- 요청 본문(form-urlencoded, 페이지 JS `lpsrp.js` 기준):
  `menuName=LP&keyword=&scKeyword=&lcId={code}&mcId=&scId=&sortType=GLOBAL_RANKPOINT&minPrice=0&maxPrice=0&pageNo={n}&pageSize=60&sellCustNo=&overseaTransYn=N`
- 응답(JSON): `TotalGoodsCount`, `PageNo`, `PageSize`, `LcIdName`(영어 카테고리명),
  `Item[]` — 각 항목에 `GoodsCode`, `GoodsName`(영어), `SalePrice`, `DiscountRate`,
  `Seller`, `LCode/MCode/SCode`, `OverseaTransYN` 등.
- 페이지네이션: `pageNo` 증가 방식. 실측(자켓/코트 200000502): 60+60+9=**129건** 소진 확인.
- Cloudflare·봇 감지 없음 — 기존 `SellerCrawler`와 동일한 plain requests 로 접근 가능.

### 2.2 글로벌 카탈로그 한정 — 수치 근거

| 항목 | 값 |
|---|---|
| 자켓/코트(중분류 200000502) 전체 상품수 | **TotalGoodsCount = 129** |
| 국내 PC 동일 카테고리 예상 규모 | 수천~수만 건 (www 차단으로 직접 대조 불가하나 자명한 차이) |
| 상품명/카테고리명 언어 | 영어 (글로벌 판매자가 영문으로 등록한 상품만 노출) |
| `shipnation=KR` / `charset=koKR` 쿠키 주입 시 | 변화 없음 (129건, 영어 유지) — 구조적 한정 |

- 카테고리 코드 체계는 국내/글로벌 공용(기존 확인 유지)이므로 **코드 호환은 되지만
  데이터 모집단이 다르다** — 이것이 `CATEGORY_RESEARCH.md` §2.4 "사용 안 함" 판정의
  정확한 사유이며, 본 실측으로 수치 확정됨.

### 2.3 폴백으로 채택한다면 (제안 — 미구현)

- Phase A 대체: 카테고리 순회 → `SearchJson` POST(순수 HTTP) → goodscode 수집.
  기존 `build_crawl_plan` + Phase B(`mg` SellerInfo) 파이프라인 그대로 재사용 가능.
- UI 라벨: "글로벌 배송 상품 한정(N건)" 표기 — 국내 데이터와 혼동 방지 필수.
- 트리거: 배포 환경에서 www가 403일 때만 수동/자동 폴백. 기본값 아님.

## 3. 웹 리서치 결과

- 공식 API: `etapi.gmarket.com` — 지마켓/옥션 **판매자** 대상 상품 API. ESM+ 로그인,
  마스터 ID 생성, 신청 승인 필요 → 구매자 관점 카테고리 수집에는 부적합.
- 외부 서비스: Apify 등에 Gmarket 스크래퍼(유료 액터) 존재. www 차단을 서비스 인프라로
  우회하는 방식이나, 계정/비용/외부 의존이 생기고 데이터 흐름이 제3자를 경유 → 채택 보류.
- Cloudflare 우회 기법(2026년 지문 위장 계열) — 본 사례는 캡차 해결 후에도 403인
  **IP 평판 차단**이므로 해당 없음(기존 Camoufox 판정과 동일 근거).

## 4. 액션 아이템 (업데이트)

1. **변경 없음**: 주 경로(www + StealthySession)와 남은 검증 목록
   (`VERIFICATION_CATEGORY_TAB_20260908.md` §6, `CATEGORY_RESEARCH.md` §4) 그대로 유지.
2. 폴백 스펙(§2)을 코드화할지는 배포 환경 실측 결과를 보고 결정 —
   배포 IP에서 www가 정상이면 불필요.
3. `item.gmarket.co.kr`가 이 네트워크에서 403임을 확인 — 향후 상세페이지 수집 기능을
   추가한다면 `mg` 계열(Cloudflare 없는) 경로를 먼저 조사할 것.

## 5. 신규 발견 — 현재 공인 IP가 한국 KT (기존 전제 수정)

| 항목 | 측정값 (2026-09-08) |
|---|---|
| 공인 IP | `118.36.49.213` — KR, KT(AS4766), 경기 안양, ip-api `hosting=false, proxy=false, mobile=false` |
| VPN/시스템 프록시 | 없음 (env 프록시 변수 없음, tun/wg 인터페이스 없음, docker 브리지만 존재) |
| www 재확인 | 이 IP에서 `/n/list` 403 (당일 재측정) |
| 기존 기록 | VERIFICATION 문서의 환경 표는 "데이터센터/해외 네트워크"로 기술 — **실측과 불일치** |

### 해석 (두 가지 가설)

1. **번아웃 가설**: 이 IP가 한국 소비자 대역(KT)으로 보이는데도 403이라면,
   과거 대량 수집(기존 탭 실수집 이력 등)으로 이 주소가 "눈에 띄는 주소"가
   됐을 가능성. 캡차 해결 후에도 403이 반복된 기존 관찰과 일치 — 주소 단위
   평판 저하(번아웃).
2. **회선 실체 가설**: KT 대역이라도 사무실/서버용 회선일 수 있음
   (내부 IP 172.31.x NAT 뒤). 소비자 대역으로 보여도 Cloudflare의 자체
   분류가 다를 수 있음.

### 프록시 구매 계획에 미치는 영향

- 열쇠는 "한국 IP" 자체가 아니라 **"깨끗한(사용 이력 없는) 가정용 IP"**.
  동일 한국 IP라도 번아웃되면 무용지물 — 구매 시 '국가'보다 '타입+신선함'이 기준.
- **무료 선행 실험**: 이 회선이 유동 IP(공유기 재부팅으로 주소가 바뀌는)라면
  재부팅 → 새 주소 → 재테스트로 "깨끗한 KT 가정용 IP" 실험이 비용 0으로 가능.
  새 주소에서 403이 사라지면 번아웃 가설 확정 + 프록시 없이도 운영 방안(주소 교체
  휴식) 확보. 그래도 403이면 회선 실체/비-IP 요인 가능성 ↑.
- **기술 준비 상태**: 코드 수정 최소 — scrapling `StealthySession`이
  `proxy`(str/dict) + `proxy_auth` 파라미터를 공식 지원(v0.4.11 실측 확인),
  `requests` 세션도 프록시 지원. 자격 증명만 있으면 즉시 테스트 가능.

## 6. Bright Data Web Unlocker 실측 — 1차 수집(Phase A) 통과 (2026-09-08)

> 사무실 IP가 KT 소비자 대역이어도 www 403 → 공유기 재부팅 불가(사무실 회선)에 따라
> Bright Data 무료 트라이얼의 **Web Unlocker**(대신 열어주기 API)로 Phase A를 실측.
> 계정: 무료 티어 + 결제 수단 등록 완료. Zone: `gm_unlocker`(type unblocker, trial).

### 요청 형식 (실측 확정)

```
POST https://api.brightdata.com/request
Authorization: Bearer <계정 API 토큰>          # 존 비밀번호는 401 — 계정 토큰으로 동작
Content-Type: application/json
{"zone":"gm_unlocker","url":"<대상 URL>","format":"raw","country":"kr"}
```

- 존 생성은 관리 API로 가능: `POST /api/zone` — 본문
  `{"zone":{"name":"gm_unlocker","type":"unblocker"},"plan":{"type":"unblocker"}}`
  (`plan`은 **객체**. 문자열을 넣으면 "plan missing/unknown zone.N" 오류)
- 결제 수단 미등록 시 존 생성 거부(`payment_method_required`).

### 실측 결과

| 검증 항목 | 결과 |
|---|---|
| Cloudflare 통과 | ✅ `country:"kr"` 출발 — 로봇 확인 페이지 없음, 한글 국내 페이지 수신 |
| 리스팅 URL | ✅ 단, **`/n/list?category={code}`(레거시)만** 목록 반환. `categoryCode=`는 홈(`/`) 리다이렉트(11KB, goodscode 0) |
| 페이지네이션 | ✅ **`&k=0&p={N}&keep-ssid=y`** — `&page=N`은 무시됨(3페이지 모두 동일 84개) |
| 페이지당 상품 | p1: 84 / p2: 60 / p3: 60, 페이지 간 교집합 ≈ 0(1개), 누적 203 |
| 총 상품수 | `totalCount: 466`(자켓/코트 중분류 200000502) — 글로벌(mg) 129 vs 국내 466 |
| goodscode 추출 | ✅ 기존 `extract_goodscodes()` 정규식 그대로 동작 |
| 응답 크기 | 페이지당 1.3~2.6MB(Next.js SSR + `__NEXT_DATA__` 포함) |

### 기존 문서 정정 사항

- `CATEGORY_RESEARCH.md` §1 "현행 표준은 categoryCode" → SSR 직접 수집 기준으로는
  **`category=`가 유효**, `categoryCode=`는 홈 리다이렉트. 폴백 순서가 아니라 주력.
- `CATEGORY_RESEARCH.md` §4 체크리스트 1~3번 중 2번(페이지네이션)과 3번(파라미터)은
  Unlocker 경로에서 답 확정: `&p=N` 방식 + `category=` 파라미터.
  → `GmarketCategoryLister`의 `category_list_url()` 수정 필요
    (`&page={n}` → `&k=0&p={n}&keep-ssid=y`, URL도 `category=` 사용).
  1번(SSR 렌더)·4번(레벨별 동작)은 배포 환경 브라우저 경로에서 병행 확인 권장.
- 비용: 성공 요청당 과금(약 $3/1,000건) — 카테고리 1개(최대 10페이지)당
  요청 ~10건 수준. 트라이얼 크레딧으로 초기 검증 가능.

### 등록 완료 (개발 환경)

- Command Code MCP: `cmd mcp add --env API_TOKEN=<token> brightdata -- npx -y @brightdata/mcp`
  (local scope) — 신규 세션부터 `/mcp`에서 brightdata 도구 사용 가능.
- 앱 코드에 Unlocker를 붙이는 것(Phase A 백엔드 교체)은 미구현 — 본 문서 §6 스펙으로
  구현 착수 가능.

## 7. 시도·조사 타임라인 (2026-09-08 전체 기록)

| # | 시도 | 결과 |
|---|---|---|
| 1 | 문서 분석 (`VERIFICATION_CATEGORY_TAB_20260908.md`) | www 403 = IP 평판 차단 판정, 배포 환경 검증 남음 상태 파악 |
| 2 | 코드·엔드포인트 조사 (explore + config 실측) | Cloudflare 없는 경로 2개 이미 사용 중: `category.gmarket.co.kr`(트리), `mg.gmarket.co.kr`(판매자정보) |
| 3 | 도메인별 접근성 실측 (curl) | www/m/item 403 · category/mg 200 · mobile은 302→m |
| 4 | `mg` 검색 API(`Search/SearchJson`) 실측 | Cloudflare 없이 JSON 60건/페이지, 페이지네이션 완비 — 그러나 **글로벌 카탈로그 한정(TotalGoodsCount 129)**, 영어 상품명. 쿠키(`shipnation=KR` 등)로도 불변 → 폴백 전용 확정 |
| 5 | 공식 API/외부 서비스 리서치 | `etapi.gmarket.com`은 판매자 전용. Apify 등 유료 서비스는 외부 의존으로 보류 |
| 6 | 공인 IP 확인 | **이 개발 머신이 한국 KT 대역(118.36.49.213, 안양)** — 기존 기록의 "데이터센터/해외" 전제는 실측과 불일치(§5). VPN/프록시 환경 없음 |
| 7 | 번아웃 가설 검증 시도 | 사무실 회선이라 공유기 재부팅(주소 갱신) 불가 → 무료 실험 불가로 종료 |
| 8 | 프록시 업체 조사 | CLI+MCP 보유 기준 3곳(Bright Data/Oxylabs/Decodo) — Bright Data 1순위(국내 풀 77k, ISP 고정형, 둘 다 보유) |
| 9 | 상품 선택 | Web Unlocker(SERP API는 검색 전용, Browser API는 2순위 — 직접 조작 필요 시) |
| 10 | 계정 세팅 | 토큰 1호는 보기 전용(존 생성 거부) → Admin 토큰 재발급 → 결제 수단 등록 → 존 생성 성공 |
| 11 | 존 생성 API 디버깅 | `plan`은 **객체**(`{"type":"unblocker"}`)여야 함. 문자열이면 "plan missing / unknown zone.N" 오류 |
| 12 | Unlocker 실측 (Phase A) | **성공** — §6 참조. Cloudflare 통과, 국내 한글 목록 수신 |
| 13 | 주소·페이지네이션 확정 | `category=` 파라미터만 유효( categoryCode는 홈 리다이렉트), 페이지 넘기기는 `&k=0&p={N}&keep-ssid=y` (`&page=N` 무시) |
| 14 | MCP 등록 | `cmd mcp add --env API_TOKEN=... brightdata -- npx -y @brightdata/mcp` (local scope) — 신규 세션부터 사용 |
| 15 | 앱 코드 반영 | `GmarketCategoryLister` 를 Unlocker 엔진으로 교체(브라우저 제거), 토큰은 환경변수/output/brightdata_token.txt. 테스트 379건 통과 |
| 16 | 변형 발견·대응 | **대분류(L-code) 페이지는 레거시 CategoryLarge 변형(광고 링크 7개뿐)으로 응답** → 엔진이 감지해 빈 페이지 처리, 전체 대상은 중분류(M) 723개로 설계 |
| 17 | 확대 수집 실측 | 중분류 4개 × 최대 8페이지 = 요청 32건, 차단 0, **고유 goodscode 1,948건**(501/485/481/481) — 페이지마다 신규 60개씩, 실측 소요 약 14분 |
| 18 | **원인 확정 실험** | **IP 평판(번아웃) 확정** — 아래 §8 |

## 8. 원인 확정 실험 — 변수 분리 (2026-09-08 저녁)

> 질문: "단순히 IP만 바꾼 것인가? IP 평판이 원인이 맞는가?"
> Unlocker 성공은 IP 교체와 통과 대행이 동시에 일어나 원인을 분리하지 못했다.
> → **한 변수만** 바꾸는 실험으로 확정한다.

### 실험 설계

| 변수 | 오전(차단) | 실험(통과) |
|---|---|---|
| 출발 IP | 사무실 KT 118.36.49.213 (번아웃 의심) | Bright Data 한국 ISP 고정 IP 31.40.194.114 (WS Telecom, 서울 — 신규) |
| 브라우저 방식 | StealthySession(patchright) + solve_cloudflare | **동일** (StealthySession + solve_cloudflare) |
| 대상 URL | /n/list?categoryCode=200000502 | 동일 |

### 준비 과정 (API 자동화 — 관리자 토큰)

- 계정 ID는 `GET /status` → `customer` 필드 (`hl_22fb0228`). 존 비밀번호는
  `GET /zone/passwords?zone=<name>`.
- 순수 주소 존: Residential(P2P)은 KYC 기업 인증 필요 → 대안 **ISP(고정 가정용)**.
- ISP 존 생성 시 **`plan`에 `bandwidth:"payperusage"` + `country:"kr"` + `ips:1`을
  반드시 포함** — `ips`를 빼면 `disable:"noips"` 상태로 남아 프록시가
  "Zone not found"를 반환한다 (문서 예시는 구버전과 불일치).
- 프록시 접속: `brd-customer-<계정ID>-zone-<존명>[:포트 22225/44445]`.

### 결과

| 시도 | 설정 | 결과 |
|---|---|---|
| 1 | 신규 KR IP + 우리 브라우저 (챌린지 해제 OFF) | 307→403 "Just a moment..." — 챌린지 미해제라 타임아웃 |
| 2 | 신규 KR IP + 우리 브라우저 (**챌린지 해제 ON**) | 챌린지 해제 성공 → **HTTP 200, 2.1MB, goodscode 84개, 정상 페이지** ✅ |

### 판정 (확정)

**원인은 출발 IP의 평판(번아웃)이 맞았다.** 같은 브라우저·같은 방식에서 IP만
바꾸자 오전에 3회 연속 403이던 요청이 정상 수신됐다.

### 교훈

1. **한 번에 한 변수만 바꾼다.** Unlocker 전환 시 IP와 방법이 함께 바뀌어
   원인 확정이 늦어졌다. 원인 규명이 목적이면 분리 실험이 먼저다.
2. 오전 판정(IP 평판)은 "맞긴 했지만" 검증 없는 추정이었다. 캡차 해제 후에도
   403이라는 정황 증거가 강했을 뿐.
3. 깨끗한 IP에서도 Cloudflare 챌린지는 **뜬다** — 차이는 "풀어도 통과되느냐"
   이다. 번아운 IP는 풀어도 통과가 안 되었다.
4. 대안 참고: 개인 계정은 Residential(P2P)이 KYC 막혀 있고, ISP(고정 가정용)
   공유형은 API로 즉시 사용 가능했다 — "집과 비슷한 IP"가 필요하면 ISP가
   개인 계정의 실용적 선택.

## 9. 대량 수집 실행 기록 — 중단 시점 스냅샷 (2026-09-08 21:40)

> 사용자 지시 "수집량을 더 늘려라(공격적으로)"에 따라 중분류 전체를 대상으로
> 실행, 2차 판매자정보 2차 패스 진행 중 사용자가 중단을 지시. 이하 중단 시점 값.

### 실행 구성

- 대상: 번들 트리의 중분류 전체 **717개**(코드 중복 제거 후)
- Phase A: Unlocker 동시 요청 10 workers, 카테고리당 최대 2페이지,
  요청 예산 하드 상한 1,400건 — `scratchpad/bulk_collect.py`
- Phase B: 기존 파이프라인(Storage/plan/SellerCrawler), 카테고리당 최대 6건,
  delay 0.3s
- 빈 카테고리 재시도 패스: `scratchpad/retry_empty.py` (같은 저장소, dedup 자동)

### 중단 시점 결과

| 항목 | 값 |
|---|---|
| Phase A 카테고리 | 717 (코드 확보 **652** / 여전히 빈 65 / 차단 3) |
| Phase A goodscode | **78,704개** (1차 42,966 + 재시도 +35,738) |
| Phase A 요청 | 1차 1,095 + 재시도 ~700 = **약 1,800건** |
| Phase B 판매자정보 | **4,248건** (1차 2,244 + 2차 패스 중단 시점 ~2,000) |
| 소요 시간 | Phase A 38분 + Phase B(1차) 24분 + 재시도+2차패스 약 30분 |
| 비용 | **구역별 청구 $3.79** = Unlocker 1,800건 $3.67(17.1GB) + ISP 실험 $0.12(10.2MB). 잔액 $6.94 (CLI `budget zones` 실측 — `pending_costs` 표시는 반영이 늦어 $0.06로 보였던 것) |
| 데이터 요청 변동성 | 200 수신에도 상품 0개인 "빈 껍데기" 응답이 간헐 발생(343개) → 재시도로 278개 회복, 65개 잔여 |

### 저장 위치 / 파일

- `output/gmarket_bulk_20260908_202418/`
  - `phase_a_results.json` — 카테고리 → goodscode 목록 (Phase A 원본)
  - `collected_ids.json` — 수집 완료 goodscode (내구성, 이어서 수집의 기준)
  - `gmarket_fast_<카테고리>_<시각>.json/.csv` — 카테고리별 판매자정보 (709쌍)
  - `gmarket_fast_ALL_*.json/.csv` — 전체 통합 (1차 패스 시점까지 — 2차 패스분은
    카테고리별 파일로만 저장됨, 통합 파일 재생성 필요)

### 이어서 수집하는 방법 (재개)

1. 빈 65개: `retry_empty.py` 재실행 (같은 저장소를 지정하면 중복 자동 제외)
2. Phase B 미수집분: `bulk_collect.py`의 Phase B는 `collected_ids.json` 기준으로
   미수집만 계획하므로 재실행하면 이어서 수집된다 (Phase A 재요청 없이 하려면
   `BULK_SKIP_PB=1` 대신 Phase B만 도는 별도 실행 또는 BULK_LIMIT=0 + 이미
   수집된 phase_a_results 로드 — 스크립트에 results 로드 분기 추가 필요)
3. 카테고리별 파일 → 통합 ALL 파일 재생성 유틸은 미구현 (필요 시 추가)

## 10. 완성 데이터셋 — 생활용품 3개 중분류 (2026-09-08 23:28)

사용자 지정 카테고리(트리 확인 → 승인 후 실행): 생활용품 > 청소용품(200002347) /
세탁용품(200000460) / 생활잡화(200000123). **끝까지 수집**(연속 2회 빈 페이지 종료,
상한 60p) + **2차 판매자정보 전부**.

| 카테고리 | 탐색 페이지 | goodscode |
|---|---|---|
| 생활용품 > 청소용품 | 12p | 624 |
| 생활용품 > 세탁용품 | 12p | 626 |
| 생활용품 > 생활잡화 | 12p | 628 |
| **합계** | | **1,878** |

- 3개 모두 **정확히 12페이지에서 종료** — 사이트가 목록 노출을 ~12페이지(600~700건)
  로 제한하는 것으로 보임 (totalCount 표기는 신뢰 불가: 청소용품 "총 1개" 표기, 실제 90+).
- Phase A 요청 37건 / 8.4분 → **이번 실행 발생 비용 $0.12** (CLI `budget zones`
  실측: gm_unlocker $3.67→$3.79, 대역폭 17.1→17.7GB. 요청당 약 $0.003).
  Phase B 1,878건은 무료 경로(mg 직접)라 $0. → 오늘 누계 $3.91, 잔액 $6.94.
- Phase B 1,878건 전부 수집: store_name/company_name/email/phone/address 100%,
  business_number 99.4%.
- 저장: `output/gmarket_dataset_household_20260908_225925/`
  (`gmarket_fast_ALL_20260908_232812.json/.csv` 통합 + 카테고리별 파일)

### 확대 수집 실측 상세 (2026-09-08 저녁)

| 카테고리(중분류) | 페이지 | 고유 goodscode |
|---|---|---|
| 여성의류 > 자켓/코트 (200000502) | 8 | 501 |
| 브랜드 여성의류 > 티셔츠 (200002669) | 8 | 485 |
| 브랜드 남성의류 > 정장 (200002678) | 8 | 481 |
| 브랜드 쥬얼리/시계 > 시계 (200000378) | 8 | 481 |

- 8페이지 전부 상이(신규 60개/페이지) — 실제 총량은 페이지 상한보다 큼
  ( totalCount 표기보다 수집량이 많은 경우도 있음: 자켓/코트 466 표기 vs 501 수집).
- 요청당 응답 1.3~2.3MB, 페이지당 30~70초 — 중분류 1개(8페이지) 약 3~4분.
- 전체(중분류 723개 × 평균 ~5페이지) 시도 시 요청 약 3,600건 ≈ $11 내외
  (요금 $3/1,000건 기준) + Phase B 판매자정보 요청 별도(무료, mg 경로).
- §10 실측 보완: 생활용품 3개 중분류는 모두 **12페이지에서 종료** — 목록 노출
  깊이에 사이트 측 상한(~12p, 600~700건)이 있는 것으로 보임.

### 남은 리스크 / 유의점

- Unlocker 응답이 페이지당 1.3~2.6MB — 대량 수집 시 대역폭·파싱 시간 고려 (파싱은
  정규식이라 가볍고, 필요 시 `__NEXT_DATA__` JSON 파싱으로 전환 가능).
- 동일 카테고리를 짧은 시간에 반복 요청하면 업체 측 성공률이 떨어질 수 있음 —
  페이지 사이 딜레이(기존 page_delay 2~3초) 유지 권장.
- API 토큰은 대화에 노출된 이력이 있으니 테스트 안정화 후 재발급 권장.
  토큰 저장 위치는 저장소 밖(환경변수 또는 gitignore된 로컬 파일).
