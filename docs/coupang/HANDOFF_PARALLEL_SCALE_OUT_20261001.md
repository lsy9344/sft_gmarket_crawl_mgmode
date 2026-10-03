# 쿠팡 수집 병렬 확장(인스턴스 N개 × Decodo) 조사 핸드오프

- 작성: 2026-10-01 12:20 KST (사이드 채팅에서 작성)
- 대상: 병렬 확장(한 호스트에서 수집 인스턴스 2~3개 × Decodo 회선 분리)을
  이어서 조사·구현할 사람 또는 에이전트
- 상태: **조사 단계 — 구현 착수 전.** 이 문서는 사용자가 추가 리서치/분석을
  진행하기 위한 인계용이다.

## 1. 한 줄 상황

한 호스트(현재 Windows)에서 인스턴스 2~3개 병렬은 기술적으로 가능하고,
**Decodo 세션(출구 IP)·Chrome 프로필·안전 장부를 인스턴스별로 분리하면
수집량은 띄운 수만큼 선형 증가(2개=2배, 3개=3배)**가 관측 근거상 맞다.
단, 아래 조건·리스크가 성립 전제다. 실험 브랜치 patchright 실행기에는
아직 프록시 인자가 없어 **첫 구현 작업은 `patchright_browser`에 proxy
인자 추가**다. 재사용 가능한 Decodo 스티키 세션 코드가 메인 워크트리에
이미 있다(§4).

## 2. 질문과 답 (사이드 채팅 원문 요약)

- Q1: "윈도우 샌드박스 사용하여 decodo 사용해서 여러개 돌리면 수집량이 n배?"
- Q2: "샌드박스 아니더라도 이 윈도우에서 가능? 각 decedo 별도 부여하면 3배가 맞지?"
- A: 가능·3배 맞음(전제: 회선/IP·프로필·장부 분리 + 스케줄 엇갈림).
  윈도우 샌드박스는 **호스트당 1 인스턴스만 실행 가능**해 병렬 장치가 아니며,
  휘발성(재부팅 시 출력 소실) 때문에 상시 운용에 부적합 — 병렬은 호스트
  프로필 방식으로 한다.

## 3. 현재 메커니즘 (실험 브랜치 — 병렬화의 기준선)

워크트리: `\\wsl.localhost\Ubuntu-Restored\home\noah\Desktop\project\dev_busi\sft_gmarket_crawl_mgmode-patchright-canary`
(WSL 경로 `/home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode-patchright-canary`, 브랜치 `experiment/coupang-patchright-canary`)

| 구성 | 파일 | 인스턴스 분리 가능성 |
|---|---|---|
| 브라우저 실행 | `app/core/coupang/patchright_canary.py` → `patchright_browser()`: `launch_persistent_context(channel="chrome", headless=False)` | **프록시 인자 없음 — 유일한 코드 변경 지점**. `user_data_dir`가 다르면 동시 실행 가능 |
| 안전 장부 | 같은 파일 `claim_live_attempt`/`settle_live_attempt`, `canary_guard.json` | `--state-root`별 독립. 최소 60분 간격, 세션 600개·10페이지, 차단 시 잠금 |
| Chrome 프로필 | `profile_dir(state_root)` → `<state_root>/chrome_profile` | 장부를 나누면 프로필도 자동 분리 |
| 출력 | `--output-dir` (현재 `C:\Users\dltnd\Desktop\PatchrightTop1000`) | 인스턴스별 폴더 |
| 예약 | `scripts/prototypes/register_patchright_top1000_schedule.ps1` + `run_patchright_top1000.ps1` | 인스턴스별 작업명. **작업당 트리거 상한 48개** 실측(49개 이상 등록 시 "같은 형식의 노드가 너무 많습니다" 오류) |
| 운용값 | 80분 간격, 세션당 목록 8페이지(480 위치) 또는 판매자 130개, 요청 사이 3초, HTTP 503은 3시간 뒤 1회 재시도 | 인스턴스마다 동일하게 적용 |

핵심: 실행 CLI(`scripts/prototypes/coupang_patchright_top_thousand.py`,
`coupang_patchright_top_sellers.py`)는 이미 `--output-dir`/`--state-root`/
`--profile-root`를 전부 받으므로, 프록시 인자만 추가하면 인스턴스 복제는
등록 스크립트 수준의 작업이다.

## 4. 재사용 자산 — 메인 워크트리 Decodo 구현 (실측 완료)

워크트리: `/home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode` (main)

`app/core/decodo.py`:

- `sticky_proxy_dict(settings, session_id)` → Playwright `proxy` 인자 dict:
  - `server`: `http://gate.decodo.com:<port>`
  - `username`: `user-<id>-session-<sid>-sessionduration-1440-country-kr`
    (스티키 1440분=24시간, 한국 고정 — 실측 확정 형식)
  - `password`: 자격
- `make_session_id(attempt)` — 시도마다 다른 세션 ID → **새 출구 IP 할당**
- `fetch_exit_ip(proxy)` — `http://ip.decodo.com/json`으로 실제 출구 IP 확인
- `credentials_ready()`, `proxy_summary()` (로그 마스킹)

이 dict는 Playwright/Patchright의 `launch_persistent_context(proxy=...)`
형식과 동일해 **patchright 쪽에 그대로 넘길 수 있다.**

운용 규율(메인 앱 `app/core/coupang/decodo_run.py`, 실측 기반):

- 시도 실패 시 세션 ID 교체로 회선 교체, 최대 3회, 재시도 대기 90초
- 새 회선 차단이 **2회 연달아** 나오면 "환경 차단"으로 보고 브라우저 없이
  스킵 → 다음 날 재개 안내 (2026-09-28/29 실측: 이 상태 재시도는 0/8 성공)
- 사전점검(브라우저 없는 403)도 차단으로 카운트

## 5. 관측 근거 — "IP 축 × 지문(프로필) 축을 함께 분리해야 한다"

메인 워크트리 `docs/coupang/HANDOFF_COUPANG_CATEGORY_20260929.md` 타임라인:

| 일시 | 관측 | 해석 |
|---|---|---|
| 9/27 19:48~21:57 | 같은 Decodo IP에서 Camoufox 403 / Edge·Chromium 200 | **지문 축**: IP가 같아도 브라우저 지문에 따라 통과/차단 갈림 |
| 9/27 22:14~23:58 | Chromium + Decodo로 카테고리 2~6 완주 | **Decodo+Chromium 조합 목록 단계 검증됨** |
| 9/28 23:58 | 집 회선(좋은 브라우저) 판매자 단계 진입 직후 403 | **회선 평판·량 축**: 회선마다 독립 평판 — 이것이 병렬 N배의 근거 |
| 9/28~29 | 새 Decodo 회선 연속 차단 2회 → 재시도 전부 실패 | 회선 교체에도 한계 — 풀/환경 단위 차단 존재(§7 리스크) |

결론: 인스턴스마다 (별도 Decodo 세션=별도 IP) × (별도 Chrome 프로필=별도
쿠키·이력)을 묶어야 독립 회선으로 취급된다.

## 6. 제안 설계 (구현 시 참고)

인스턴스 정의 예(3개 기준):

| 인스턴스 | 출력 폴더 | 장부/프로필 루트 | 회선 | 예약 작업명 | 시작 오프셋 |
|---|---|---|---|---|---|
| A(현재) | `Desktop\PatchrightTop1000` | `%LOCALAPPDATA%\SellerCollectorPatchrightCanary` | 집 회선 직접 | `PatchrightTop1000-80Min` (기존) | 기준 |
| B | `Desktop\PatchrightTop1000_B` | `...\Canary_B` | Decodo 세션 B(고정 sid) | `PatchrightTop1000B-80Min` | +27분 |
| C | `Desktop\PatchrightTop1000_C` | `...\Canary_C` | Decodo 세션 C(고정 sid) | `PatchrightTop1000C-80Min` | +53분 |

- 승격 순서: **2개(B만)로 시작 → 24시간 무차단 관찰 → C 추가**. 처음부터
  3개 띄우지 않는다.
- 세션 ID 정책 결정 필요: 인스턴스당 **고정 sid**(장기 스티키, 회선 평판
  축적) vs 시도마다 갱신(메인 앱 방식, 차단 회피 중심). 병렬 확대 목적이면
  고정 sid + 인스턴스별 장부가 자연스럽다. 이 결정은 조사 항목 §8-1.
- 집 회선 A와 Decodo B/C는 IP가 다르므로 동시 운영 간섭은 없을 것으로
  보되, §8-6으로 확인 권장.

## 7. 리스크와 완화

| 리스크 | 내용 | 완화 |
|---|---|---|
| Decodo 풀 평판 공유 | 세션이 달라도 출구 IP가 같은 ASN 풀이면 풀 단위 차단 시 전 인스턴스 동시 타격(9/28~29 실측 사례) | 인스턴스별 `fetch_exit_ip` 기록·비교(서로 다른 대역인지), 연속 차단 2회 시 해당 인스턴스만 자동 비활성화(현재 실행기 방식 유지) |
| 지문 상관 | 같은 Chrome/OS 지문이 상이 IP에서 유사 행동 패턴으로 반복 | 시작 시각 엇갈림, 인스턴스별 프로필·쿠키 독립, 세션당 페이지 수 유지 |
| 대역폭 비용 | 목록 HTML은 페이지당 MB 단위 — 트래픽 N배 | 판매자(API, 경량) 비중 높은 단계에 우선 배치 검토 |
| 운영 복잡도 | 장부·차단 지점·로그 N배 | 인스턴스별 상태 조회(status-only) 통합 점검 스크립트 |
| 작업 스케줄러 제약 | 작업당 트리거 48개 상한, Interactive 로그온 필요(headed Chrome) | 48개 초과 시 이어서 재등록(기존 방식), 잠금 화면 시 동작 검증(§8-5) |

## 8. 미해결 질문 — 추가 조사/분석 항목 (이 문서의 핵심 인계물)

1. **세션 ID ↔ 출구 IP 매핑 특성**: `sessionduration-1440` 고정 sid가 실제로
   24시간 같은 IP를 유지하는지, 만료 후 어떻게 되는지(Decodo 문서/실측).
   인스턴스별 고정 sid 정책의 근거.
2. **풀 편중**: 고정 sid 2~3개의 출구 IP가 같은 ASN/지역에 몰리는지 확인
   방법(`fetch_exit_ip` 로그 수일간 수집 후 비교).
3. **동일 지문 + 상이 IP의 상관 감지 가능성**: 쿠팡/Akamai가 IP 간 행동
   상관으로 계정 없는 세션을 묶는 사례/근거 조사(외부 리서치).
4. **대역폭·비용 실측**: 목록 1페이지(60카드) 평균 트래픽, 판매자 API 1회
   평균 트래픽 → 인스턴스당 일 비용 계산.
5. **headed Chrome + 작업 스케줄러**: 화면 잠금/RDP 세션 종료 시 headed
   Chrome이 계속 동작하는지(Interactive 로그온 조건의 실제 의미) 실측.
6. **집 회선 + Decodo 동시 운영 간섭 여부**: NAT/회선 공유로 인한 영향
   없음 확인(이론상 없음).
7. **Windows Sandbox 병용 여부**: 병렬이 목적이면 불필요(호스트당 1개
   한계). 격리 검증용으로만 유지할지 결정.

## 9. 구현 착수 시 필요 변경 목록 (체크리스트)

1. `app/core/coupang/patchright_canary.py` — `patchright_browser()`에
   `proxy: dict | None = None` 인자 추가, `launch_persistent_context(proxy=...)`
   전달. (메인 워크트리 `app/core/decodo.py`의 `sticky_proxy_dict` 반환값과
   그대로 호환 — 의존성 복사 여부 결정 필요: 단일 파일 카피 or 재구현)
2. 실행 CLI(`coupang_patchright_top_thousand.py` 등)에 `--proxy-session-id`
   인자 → `sticky_proxy_dict`로 조립해 `browser_scope_factory`에 전달.
3. 인스턴스별 등록 스크립트(기존 `register_patchright_top1000_schedule.ps1`
   파라미터화: 작업명/출력/장부/시작시각/세션ID).
4. 오프라인 테스트: 프록시 인자 전달 경로, 세션 ID별 프록시 dict 조립.
5. 첫 실증: 인스턴스 B 단독 24시간(집 회선 A는 계속) → 차단·503·지간 비교.

## 10. 현재 진행 캠페인 스냅샷 (2026-10-01 12:1x)

- 상품(1단계): 견과/건과+축산 완료분 **고유 3,268개**. 축산 추가 수집은
  사용자 지시로 이번 턴까지만 하고 종료 예정.
- 판매자(2단계, 완성형: 상호·대표자·사업자번호·전화·이메일·주소):
  **메인 작업(다른 대화)에서 구현 진행 중** — `app/core/coupang/
  patchright_top_sellers.py`, `scripts/prototypes/coupang_patchright_top_
  pipeline.py`, `tests/test_coupang_patchright_top_sellers.py`가 12:08에
  추가된 것 확인. 이 사이드 채팅에서는 중복 작업하지 않음.
- 최종 산출물 형식 표준: `Desktop\garbage\0928 coupang\`(판매자 319명 CSV
  + xlsx) 참조.
- **권장: 병렬 확장은 완성형 판매자 단계가 안정 궤도에 오른 뒤 착수.**

## 11. 관련 문서 전체 경로

실험 브랜치 (`.../sft_gmarket_crawl_mgmode-patchright-canary/docs/coupang/`):

- `PATCHRIGHT_EXPERIMENT_REPORT.md` — patchright 실험 전체 이력(간격·봉투 변천)
- `PATCHRIGHT_TOP1000_COLLECTION_PLAN.md` — 현재 캠페인 계획·운용 규칙
- `PATCHRIGHT_FULL_FRUIT_COLLECTION_PLAN.md` — 과일 캠페인(판매자 단계 원형)

메인 워크트리 (`.../sft_gmarket_crawl_mgmode/docs/coupang/`):

- `HANDOFF_COUPANG_CATEGORY_20260929.md` — §5 근거의 원본 타임라인
- `research/CAMOUFOX_BLOCK_ROOT_CAUSE_20260927.md` — 지문 차단 원인 확정
- `research/VENDOR_PROXY_VERIFICATION_20260928.md` — 판매자 단계 Decodo 전환 검증
- `research/PROXY_SELLER_PROPOSAL_REVIEW_20260928.md` — 프록시 제안 검토(60/100)
- `research/SANDBOX_DIRECT_BLOCK_20260928.md` — 직접 회선 차단 검토
- `research/BROWSER_ENGINE_DECISION_20260927.md` — Chromium 전환 결정
- 코드: `app/core/decodo.py`, `app/core/coupang/decodo_run.py`
