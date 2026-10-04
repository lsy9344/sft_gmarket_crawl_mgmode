# 쿠팡 병렬 확장 — 구현·배포·운영 상태 전체 기록 (2026-10-01)

- 작성: 2026-10-01 21:20 KST (구현·배포 세션 종료 시점 스냅샷)
- 대상: 병렬 확장 작업 전체를 이어받는 사람 또는 에이전트
- 상위 문서: [PARALLEL_SCALE_OUT_DESIGN_20261001.md](PARALLEL_SCALE_OUT_DESIGN_20261001.md) (설계),
  [HANDOFF_PARALLEL_SCALE_OUT_20261001.md](HANDOFF_PARALLEL_SCALE_OUT_20261001.md) (조사 인계)
- 한 줄 상황: **설계 전 항목 구현 완료·테스트 477 passed. B 인스턴스(Decodo) 라이브 가동 중
  — 첫 목록·판매자 세션 모두 성공(판매자 63명 완성형 생성). 중간에 b01 세션 조기 사망
  사건이 있었으나 b02 교체로 완전 복구. 카나리는 22:30부터 15회 자동 진행.**

## 1. 워크트리·브랜치 지도 (2026-10-01 기준)

| 워크트리 | 브랜치 | 역할 | 미커밋 상태 |
|---|---|---|---|
| `~/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode` | main | 메인 앱(SellerCollector). decodo.py 원본 소스. A 캠페인과 무관하게 운영 | (별도 관리) |
| `.../sft_gmarket_crawl_mgmode-patchright-canary` | `experiment/coupang-patchright-canary` | **A 인스턴스 라이브 운용 중** (집 회선, 견과/축산 판매자 단계 — 다른 대화 담당). 이 세션에서 읽기만 함 | 32개 항목 (Top1000 캠페인 전체) |
| `.../sft_gmarket_crawl_mgmode-parallel-scaleout` | `experiment/coupang-parallel-scaleout` | **병렬 확장 구현 + B 라이브 운용**. canary의 미커밋 상태를 스냅샷해 거기에 구현함 | 36개 항목 (아래 §2) |

- 커밋 정책: 사용자 지시 전 커밋 금지(레포 관례). 두 워크트리 모두 미커밋.
- canary 워크트리의 A 예약 작업은 canary 경로를 그대로 씀(무손상). B 예약은 scaleout 경로 사용.

## 2. 이 세션의 구현 내역 (모두 scaleout 워크트리, 미커밋)

설계 §5 항목 ↔ 구현 매핑. 서브에이전트 3개 병렬 투입 + 메인 통합.

| 설계 항목 | 구현 | 비고 |
|---|---|---|
| §5.1 decodo 모듈 | `app/core/decodo.py` 신규 (메인 워크트리에서 바이트 동일 복사, diff 검증) | 자격은 `output/decodo_settings.json`(gitignore) — **앱 설정 탭 자격을 복사해 사용** |
| §5.2 브라우저 프록시 | `app/core/coupang/patchright_canary.py` — `patchright_browser(..., proxy: dict \| None = None)` → `launch_persistent_context(proxy=...)` | proxy=None은 A 현행과 동일 경로 |
| §5.3 실행 함수 통과 | `patchright_top_thousand.py` `run_top_pages(..., proxy=None)`, `patchright_top_sellers.py` `run_top_seller_batch(..., proxy=None)` → `factory(..., proxy=proxy)` | |
| §5.4 CLI | thousand/sellers CLI에 `--proxy-session-id`. 자격 미비 시 `proxy_credentials_missing` + **종료 코드 24**(브라우저 열기 전) | |
| §5.5 파이프라인 | `coupang_patchright_top_pipeline.py` — sid 통과 + 매 실행 `exit_ip_check` 이벤트 1회 로그(실패해도 계속, status-only는 네트워크 0회) | |
| §5.6 PS1 파라미터화 | `run_patchright_top1000.ps1`/`register_...ps1` — TaskName/RepoRoot/OutputDir/StateRoot/ProxySessionId(/CategoriesFile). 완성형 폴더명 TaskName 파생 | Parser 검사 PASS(실제 powershell.exe) |
| §5.7 통합 점검 | `patchright_instances_status.ps1` 신규 — A/B/C 가드·실행로그·출구IP·작업상태 JSON, 읽기 전용·네트워크 0회 | |
| §5.8 테스트 | 전체 **477 passed + 21 subtests** (기반 45 → 신규 40+). 오프라인 스모크: 자격없는 status-only=0, sid+자격없음=24, 신규폴더=category | |
| (설계 외·필요) 가족 파라미터화 | `--categories-file` — `load_categories_file()` 검증 포함, `run_top_pages`/`TopThousandStore`/`read_state`/`choose_action` threading. `build_all_finals(categories_file=…)`은 무관 family final 스킵 | B가 다른 가족을 쓰는 데 필수. 가족 파일: `scripts/prototypes/coupang_categories_kitchen_b.json` |
| (통합 발견) 신규 인스턴스 부트스트랩 | 빈 출력 폴더에서 파이프라인이 halted(23)로 빠지던 것 → 상품 파일 없으면 목록 단계부터 | 신규 배포 결함, 테스트 2개 추가 |
| (통합 발견) 기존 버그 | `build_all_finals`의 `FINAL_FAMILY_CATEGORIES` 미import NameError(완성형 경로 크래시) 수정 | A에도 있는 잠재 버그 |

주요 파일: 변경 7 + 신규 decodo.py·patchright_instances_status.ps1·coupang_categories_kitchen_b.json·테스트 2건 등.

## 3. B 인스턴스 배포 상태

정의(설계 §3.1): 작업 `PatchrightTop1000B-80Min` · 출력 `Desktop\PatchrightTop1000_B` ·
장부 `%LOCALAPPDATA%\SellerCollectorPatchrightCanary_B` (프로필 자동 분리) ·
가족 **주방 3개 하위** (185671 냄비/프라이팬, 185735 그릇/홈세트, 185872 밀폐저장/도시락 —
비식품이라 3P 수율 높음, 소규모라 카나리 겸용) · 세션 **`b02`** (b01은 사망, §4) ·
오프셋 A+40분 · 봉투 A와 동일(세션 8페이지/480 or 판매자 130개, 60분 최소 간격, 80분 예약).

예약: **2026-10-01 22:30 ~ 10-02 17:10, 15회** (재등록됨. 원래 17:45부터 18회였으나 §4 사건으로 조정).

### 실측 결과 (10-01)

| 시각 | 세션 | 결과 |
|---|---|---|
| 16:24:58~16:27:25 | 목록 8페이지 (b01, 출구 222.119.25.166 KR) | 홈·8페이지 전부 200, 차단 0, **3P 상품 80개** (카드 480장 중, 3P 비율 17%) |
| 17:45:01~04 | 판매자 첫 진입 (b01) | **ERR_TUNNEL_CONNECTION_FAILED 3초 실패** → 예약 자동 비활성 (§4) |
| 21:09:28~21:13:45 | 판매자 재시도 (b02, 출구 175.203.56.170 KR) | **완전 성공**: 상품 80개 전부 매핑, **고유 판매자 63명 완성형 저장 `coupang_판매자_63명.csv`**, API 71회 전부 프록시 경유 성공, 실패 매핑 0 |

- 판매자 단계(브라우저 내 fetch → 프록시 경유) **실증 완료**. 판매자 수율: 상품 80 → 63명 (79%).
- A는 이 전체 과정에서 정상 유지(15:45·17:05 등 실행 정상, blocked=false) — **간섭 없음 확인**.

## 4. 사건 기록 — b01 세션 조기 사망 (첫 카나리 관측)

| 시각(KST) | 사실 |
|---|---|
| 16:24~16:27 | b01 세션 정상 (목록 8페이지 성공) |
| 17:45 | b01 터널 실패. 같은 시각 `exit_ip_check`도 502 — **b01 세션이 게이트에서 죽음** |
| 21:07 | 진단: b01 → 502 (여전히 사망), **b02 → 175.203.56.170 KR 정상** → 계정/게이트는 정상, b01만 문제 |
| 21:08 | b02 수동 실행 1회 → 판매자 컨트롤이 17:45 실패로 halted 상태라 브라우저 없이 거부(23) |
| 21:09 | 컨트롤 수동 리셋(ready) 후 b02 재시도 → 완전 성공 (§3 표) |

**관측 결론 (설계 §10.1 관련 첫 데이터)**: `sessionduration-1440` 스펙과 무관하게
**스티키 세션은 ~1시간 21분 만에 죽을 수 있다**. 원인(게이트 측 만료/오류)은 미상.
운영 대응 절차(실증됨): ① `fetch_exit_ip`로 세션 진단(쿠팡 트래픽 0) ② 세션 사망 시 sid
교체는 **인프라 복구**로 허용(쿠팡 접촉 0건이므로 "당일 회선 교체 금지" 규율 — 사이트
차단용 — 에 해당하지 않음) ③ 판매자 컨트롤 halted 리셋 절차(§8 명령 참조).

## 5. 현재 인스턴스 현황 (21:20 기준)

| | A | B | C |
|---|---|---|---|
| 회선 | 집 직접 | Decodo `b02` (175.203.56.170) | 미생성 |
| 담당 | 견과/축산 판매자(다른 대화) | 주방 3개 가족 | (카나리 통과 후) |
| 예약 | 정상 운영 중 | **22:30~10-02 17:10, 15회** | — |
| 가드 | blocked=false | blocked=false, 오늘 3세션(16:24/17:45실패/21:09) | — |
| 산출 | 계속 누적 | 3P 80개 + **판매자 63명 완성형** | — |

## 6. 자동화·예약된 작업

- **ZCode 자동화 `automation-5206c569…`**: 2026-10-02 18:30 1회 — B 카나리 결과 점검·보고
  (성공 기준: 브라우저 차단 0, 503 ≤ 1회, A 무영향, 출구 IP 추이. C 등록 조치는 사용자 지시까지 대기)
- B 예약은 트리거 소진(10-02 17:10) 또는 오류 시 자동 종료. **연장은 재등록 필요**(§8 명령).

## 7. 비용·속도 기대치 (2026-10-01 채팅 분석 결과 기록)

- 판매자 1,000명 데이터셋(큰 카테고리 1개) = 인스턴스 1개로 **2~3일, Decodo $1.5~5** (집 회선이면 $0). 상품→판매자 변환율 실측 ~45-79%.
- 기대 배율: 2구성(A+B) +80~95%, 3구성 +165~195% (계획 기준. 풀 차단 시 B/C 동시 다운이 하한 결정).
- 비용은 인스턴스 수가 아니라 수집량에 비례. 4GB 플랜은 Decodo 가족 3~10개분 — 5개 구성 시 상향 필수.
- 첫 실측: 냄비/프라이팬 3P 비율 17% (식품 견과 20%·축산 5%와 비교·참고용)

## 8. 운영 명령 레퍼런스 (복붙용)

```powershell
# 전체 현황(읽기 전용)
powershell -File "\\wsl.localhost\Ubuntu-Restored\home\noah\Desktop\project\dev_busi\sft_gmarket_crawl_mgmode-parallel-scaleout\scripts\prototypes\patchright_instances_status.ps1"

# B 수동 1회
powershell -File "\\wsl.localhost\...\scaleout\scripts\prototypes\run_patchright_top1000.ps1" -TaskName "PatchrightTop1000B-80Min" -OutputDir "C:\Users\dltnd\Desktop\PatchrightTop1000_B" -StateRoot "C:\Users\dltnd\AppData\Local\SellerCollectorPatchrightCanary_B" -ProxySessionId "b02" -CategoriesFile "\\wsl.localhost\...\scaleout\scripts\prototypes\coupang_categories_kitchen_b.json"

# B 예약 연장/재등록 (FirstRun/RunCount 조정)
powershell -File "\\wsl.localhost\...\scaleout\scripts\prototypes\register_patchright_top1000_schedule.ps1" -TaskName "PatchrightTop1000B-80Min" -FirstRun "<시각>" -RunCount 15 -OutputDir "C:\Users\dltnd\Desktop\PatchrightTop1000_B" -StateRoot "C:\Users\dltnd\AppData\Local\SellerCollectorPatchrightCanary_B" -ProxySessionId "b02" -CategoriesFile "\\wsl.localhost\...\coupang_categories_kitchen_b.json"

# 긴급 중단
Disable-ScheduledTask -TaskName "PatchrightTop1000B-80Min"
```

- 판매자 컨트롤 halted 리셋(503 아닌 오류 후 재개 시): `PatchrightTop1000_B\top_seller_control.json`을
  `{"version":1,"status":"ready","resumed_at":"<현재시각>","resumed_from":"<사유>"}`로 저장(실증된 절차, §4).
- 세션 진단: 워크트리에서 `sticky_proxy_dict(load_settings(), '<sid>')` → `fetch_exit_ip` (쿠팡 트래픽 0).

## 9. 다음 단계 (우선순위순)

1. **10-02 18:30 자동 점검 결과 확인** → 성공이면 사용자에게 C 추가 물어보기
   (C 등록 = 가족 파일 하나[예: 반려동물 115674 등] + 등록 스크립트 `-TaskName PatchrightTop1000C-80Min -ProxySessionId c01 …`)
2. **B 예약 연장**: 10-02 17:10 트리거 소진 전 재등록 (§8 명령, 15~18회 단위)
3. B 주방 가족 완주 후 **다음 가족 로테이션** — 카테고리 후보는 메인 `output/coupang_category_tree.json`(쇼핑 15개 최상위)에서 미수집 가족 선택
4. 트래픽 실측: B 가족 1개 완주 후 Decodo 대시보드 GB 확인 → 플랜 상향 결정(§7)
5. 커밋 (사용자 결정 — 두 워크트리 미커밋 32+36개 항목)

## 10. 남은 것 / 미해결 (설계 §10 갱신)

| 항목 | 상태 |
|---|---|
| C 인스턴스 | 대기 (카나리 통과 + 사용자 결정) |
| 익일 사전점검(§3.2-4) 자동화 | **미구현** — 사이트 차단 재개 시에만 필요. 현재는 수동 절차(§8 세션 진단 명령) |
| 스티키 세션 수명 | **실측 시작** — b01 조기 사망(§4). b02 수명 추적 중(exit_ip_check 로그로) |
| 트래픽/플랜 | 가족 완주 후 실측 |
| 잠금 화면 headed Chrome(§10.4) | 미실측이지만 A 수 주간 무인 운영으로 사실상 무문제 |
| 세션 사망 자동 감지 | exit_ip_check가 로그만 남김(설계 의도). 실패 누적 시 알림 없음 — 개선 여지 |

## 12. 10-02 아침 갱신 (09:5x~10:0x)

**밤사이 경과**: 22:30(목록)·23:50(판매자 +11명)·01:10(목록)·02:30(목록) 5연속
성공(어제 21:09 포함) 후 **03:50 판매자 세션 실패 — b02 세션 사망** (ERR_TUNNEL,
어제 b01과 동일 양상, 쿠팡 접촉 0). 예약 자동 비활성.

**패턴 확정 (카나리 관측 #2)**: 두 세션 모두 실행 사이 **유휴 간격(~78분) 중에 사망**.
b01은 1번째 간격, b02는 5번째 간격(수명 ~6.5시간, 성공 5회) — 확률적 사망,
`sessionduration-1440` 스펙과 무관. 쿠팡 차단은 10-02 아침까지 **0건 유지**.

**아침 복구 (실증 절차 2회차)**: b02 사망/b03 정상(112.221.9.254) 진단 → 컨트롤
리셋 → b03 수동 검증 성공 (상품 55개 전부 매핑, 판매자 45명 추가, API 51회 성공,
**완성형 `coupang_판매자_119명.csv`**) → 예약 재등록 **b03, 10-02 11:20~18:00 6회**.
B 판매자 대기열 소진 — 다음 실행부터 목록 단계(그릇/홈세트) 재개.

**누적 (10-02 10:00)**: 3P 상품 155개 · 판매자 119명 · 실패 매핑 0 · 쿠팡 차단 0 ·
세션 사망 2회(b01, b02).

**운영 시사점**: 세션이 몇 시간마다 죽으므로 무인 24시간 운영엔 `exit_ip_check`
실패 시 자동 sid 증가 교체(b03→b04…) 파이프라인 개선이 사실상 필요 — 카나리
평가(10-02 18:30 자동 점검) 후 구현 결정 권장.

## 13. 10-02 정오 갱신 — 밤 대비 사전 처리 완료

사용자 요청("오늘 밤 전까지 미리 처리")으로 4건 처리 (12:5x 기준):

1. **세션 자동 교체 구현 (서브에이전트)** — `coupang_patchright_top_pipeline.py`:
   실행 시작 시 `proxy_session_state.json`의 마지막 성공 sid부터 점검, 세션 수준
   실패(response/connection/timeout/other)면 sid 끝자리 +1 교체로 최대 3회 재시도
   (b03→b04→b05→b06). 계정 수준(quota/auth/unknown_407)은 교체 없음. 성공 sid를
   자식에 전달·상태 파일 갱신, 이벤트에 `rotated`/`tried` 기록. `--status-only`·
   sid 미지정(A) 경로 무변경. **전체 485 passed + 24 subtests.** 이제 밤중 세션
   사망이어도 다음 예약에서 스스로 회복(쿠팡 트래픽 0인 점검만 추가, 회당 ≤4회).
2. **B 예약 연장**: 10-02 14:00 ~ **10-03 10:00, 16회** (sid b03, 자동 교체 활성).
   11:20(목록)·12:40(판매자) 실행 모두 정상 — 카나리 시작 후 B 정상 종료 9회,
   쿠팡 차단 0.
3. **C 인스턴스 가족 파일 준비**: `scripts/prototypes/coupang_categories_pet_c.json`
   (반려동물: 강아지 용품 118876 · 고양이 용품 118878 · 펫티켓 산책용품 381522).
   등록은 카나리 평가 후 사용자 결정 시 한 줄(§8 명령에 TaskName/C/sid c01만 바꿔).
4. **B 산출물 백업**: `/tmp/PatchrightTop1000_B_backup_1002` (판매자 119명 완성형 포함).
   A 건강 확인(11:45 실행·blocked=false).

## 14. 10-02 저녁 — 카나리 통과 판정 + C 추가 + interop 장애 (20:3x~)

### 카나리 판정: **통과** (사용자 승인하에 C 추가 진행)

- 10-02 B 실행 11회 중 10회 정상 (유일 실패 03:50 = 자동 교체 도입 전 세션 사망)
- **세션 자동 교체 실전 작동**: b03 사망 → b04(119.194.60.254) 자동 롤오버 → 이후 3연속 정상
- 쿠팡 차단 0건 · HTTP 503 0건 · A 무영향(blocked=false 유지)
- B 누적: 3P 217개 · 판매자 170명

### WSL interop 장애 (20:3x 발견)

`/mnt/c/.../powershell.exe` 실행이 `Exec format error` — binfmt_misc의 WSLInterop
엔트리가 소실됨. 복구에는 root 필요(암호 없는 경로 없음). **영향**: 이 세션에서
Windows 명령 직접 실행 불가. **무영향**: Windows 작업 스케줄러의 A/B 예약 실행
(WSL과 무관하게 네이티브 실행), WSL↔Windows 파일 접근(/mnt/c, \\wsl.localhost).
Linux `pwsh`(7)로 PS1 구문 검사는 가능 — 3개 스크립트 PASS 확인.
복구 옵션(사용자): WSL 재시작(세션 종료됨) 또는 root로
`echo ":WSLInterop:M::MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff::/init:" > /proc/sys/fs/binfmt_misc/register`.

### C 인스턴스 추가 — 피기백 등록 방식

interop 장애로 직접 등록 불가 → **B 러너(`run_patchright_top1000.ps1`)에 1회성
등록 블록** 삽입: 다음 B 예약 실행(22:00)에서 `PatchrightTop1000C-80Min` 등록
(반려동물 가족 `coupang_categories_pet_c.json`, sid c01, 22:50~10-03 10:50 10회).
마커 `c_registration_done.json`로 1회 보장, 전체 try/catch, B 본연 동작 무영향.
**확인 후 블록 제거 필요** (아래 §15 상태 확인).

### 밤 운용 계획 (10-02 → 10-03 아침)

| 인스턴스 | 예약 범위 | 비고 |
|---|---|---|
| A | ~10-03 01:05 | 이후 트리거 없음 — **연장은 A 담당 대화/사용자 결정** (중복 실행 위험으로 이쪽에서 임의 연장 안 함) |
| B | ~10-03 10:00 (16회) | 자동 세션 교체 활성 |
| C | 22:50 ~ 10-03 10:50 (10회) | 첫 실행이 처녀항해 — 실패 시 자동 비활성+로그 |

**사용자 결정 (10-02 저녁)**: A는 자연 종결(01:05)로 둔다 — 연장 없음.
라이브가 아닌 오프라인 테스트로 검증할 수 있는 것은 전부 별도 테스트로 추가.

## 15. 10-02 저녁 — 다중 인스턴스 오프라인 테스트 배치 (서브에이전트)

신규 `tests/test_parallel_instances_integration.py` 8개 (전체 스위트 **493 passed + 24 subtests**, 현행 코드 버그 0건):

1. 가족 파일 스키마 2종(kitchen·pet id 정확성) + **세 가족 상호 배타성**(A 기본 19·B 주방·C 반려동물 — 작업 분할 성립 조건)
2. **3인스턴스 격리 시뮬레이션** — A/B/C 나란히 실행: 각 상태·가드·회전상태·프로필 전부 별개, 교차 오염 0건, 자식 인자 정확
3. **회전 상태 인스턴스 격리** — B 세션 교체(b04→b05)가 C 상태(c01)에 무영향
4. **가드 독립성** — 같은 시각 다른 장부 클레임 둘 다 허용(간격 제한은 장부 단위)
5. **터널 장애 halted 고정** — ERR_TUNNEL 사유 컨트롤 halted → 종료 23 (현행 규격 pin, 실행 중 사망 시 §8 수동 리셋 절차)
6. **회전+가족파일 조합** — 교체된 sid와 categories-file이 같은 자식 명령에 전달

## 17. 관련 문서

- 설계: [PARALLEL_SCALE_OUT_DESIGN_20261001.md](PARALLEL_SCALE_OUT_DESIGN_20261001.md)
- 조사 인계: [HANDOFF_PARALLEL_SCALE_OUT_20261001.md](HANDOFF_PARALLEL_SCALE_OUT_20261001.md)
- A 캠페인: canary 워크트리 `PATCHRIGHT_TOP1000_COLLECTION_PLAN.md`, `PATCHRIGHT_EXPERIMENT_REPORT.md`
- 근거 원본: 메인 워크트리 `docs/coupang/HANDOFF_COUPANG_CATEGORY_20260929.md`,
  `research/CAMOUFOX_BLOCK_ROOT_CAUSE_20260927.md`, `DECODO_PROXY_METHODOLOGY_20260910.md`

## 16. 10-02 밤 — B 1차 가족 완주 + 2차 재등록 + 버그 2건 수정 (20:40~21:3x)

- **B 주방 1차 가족 완주 (28시간 만, 차단 0건)**: 20:40 실행에서 3개 카테고리
  목록 전부 완료 → 종료 30 → 완성형 `Desktop\garbage\PatchrightTop1000B_final_20261002_2040\`
  복사 + 예약 자동 비활성. 최종: **3P 상품 217개 · 판매자 170명 완성형
  (`coupang_판매자_170명.csv`) · 카테고리별 데이터셋 3종 + 통합 final_dataset_all.csv**.
- **C 등록 완료 (피기백)**: 편집이 20:40 B 트리거와 맞아떨어져 B 실행이 블록을
  실행, `done:true(20:40:05)` 마커. C 작업 Ready·트리거 10(22:50~10-03 10:50)
  확인. 블록은 제거(러너 원상복구, 구문 PASS). **interop은 21:xx 자체 복구됨.**
- **버그 수정 2건**:
  1. 러너 완성형 복사에서 한국어 글로브(`coupang_판매자_*명.csv`)가 PS 5.1의
     BOM 없는 UTF-8→ANSI 해석으로 깨져 조용히 누락 → ASCII 패턴 `coupang_*.csv`로
     수정. 10-02 완성형 폴더 누락분(판매자 170명)은 수동 보완 완료.
  2. 파이프라인이 완성형 완료(30)를 `pipeline_slot_failed`로 기록하던 라벨 오기 →
     `slot_finished`로 수정. 전체 스위트 **493 passed + 24 subtests** 유지.
- **B 2차 가족 재등록**: `coupang_categories_kitchen_b2.json` (주방조리도구 185976 ·
  컵/텀블러/와인용품 185797 · 수저/커트러리 185823) · 출력 `PatchrightTop1000_B2`
  (신규) · 장부/프로필 `Canary_B` 그대로(회선·프로필 연속성) · sid b04 ·
  **22:10 ~ 10-03 10:10, 10회 Ready**.

### 밤 운용 최종 (10-02 → 10-03 아침)

| 인스턴스 | 범위 | 가족 |
|---|---|---|
| A | ~01:05 자연 종결 (사용자 결정) | 견과/축산 |
| B(2차) | 22:10 ~ 10:10 | 주방 나머지 3개 |
| C | 22:50 ~ 10:50 | 반려동물 3개 (첫 실행) |

## 17. 10-03 아침 — 밤샘 결과 + A 완성형 마무리 + C 복구 (09:0x~09:2x)

### 밤샘 결과
- **B2: 9/9 전부 성공, 무차단·무교체(b04 밤샘 생존)** — 누적 3P 130개/판매자 113명
- **C: 22:50 첫 목록 성공 → 00:10 판매자 실행 중 사망**(API 57회 후 `Failed to fetch`
  = 첫 "실행 중" 세션 사망 사례) → 예약 자동 비활성. 저장은 원자 단위라 손실 0
- **A: 22:25 데이터 수집 완료 → 23:45 완성형 마무리에서 크래시(result=1)**.
  원인: canary 워크트리 파이프라인의 `FINAL_FAMILY_CATEGORIES` NameError —
  10-01 스케일아웃에서는 수정됐으나 canary쪽 미수정.

### 조치
1. **A 완성형 마무리 완료 (수정된 스케일아웃 코드로 실행, canary 미수정)**:
   `Desktop\garbage\PatchrightTop1000_final_20261003_0910\` —
   **판매자 1,129명 완성형(상호·대표·사업자번호·전화·이메일·주소 100%)** +
   카테고리별 10종 + final_dataset_all. 한국어 CSV 복사 버그 수정(ASCII 글로브)도
   이번 복사에서 정상 작동 확인.
2. **C 복구**: 컨트롤 ready 리셋 + 재등록 09:40~21:40 (10회). c01 사망 상태 —
   다음 실행 시작 시 자동 교체(c01→c02)가 처리(구현 목적 그대로).
3. **B2 연장**: 10:10~23:30 (11회) 재등록.

### 누적 실적 (쿠팡 차단 0건 유지)
| 캠페인 | 판매자 | 상태 |
|---|---|---|
| A 견과/축산 | **1,129명** | 완주·완성형 완료 |
| B 주방 1차 | 170명 | 완주·완성형 완료 |
| B2 주방 2차 | 113명 | 진행 중 (~23:30) |
| C 반려동물 | 50명 | 복구 후 재개 (09:40~) |

### canary 워크트리에 남은 알려진 버그 2건 (A 담당 대화에 인계 필요)
`scripts/prototypes/coupang_patchright_top_pipeline.py` NameError(완성형 경로),
`run_patchright_top1000.ps1` 한국어 글로브 복사 누락 — 둘 다 스케일아웃에는 수정돼 있음.

## 18. 전체 히스토리 종합 (2026-10-01 12:00 → 10-03 09:4x)

### 연표

| 일시(KST) | 사건 |
|---|---|
| 10-01 12:20 | 병렬 확장 조사 핸드오프 작성(사이드 채팅), 설계 검토 착수 |
| 10-01 오후 | 설계문서 작성(가능/조건/리스크/단계) — 근거: 차단 3층 분석, 회선 독립 실측 |
| 10-01 ~16:00 | 스케일아웃 워크트리 생성(canary 스냅샷), 서브에이전트 2개 병렬로 구현: 프록시 연결·PS1 파라미터화·통합 점검 스크립트, 453 passed |
| 10-01 16:24 | **B 첫 실접속 성공** (b01, 222.119.25.166) — 주방 1차 목록 8페이지, 3P 80개 |
| 10-01 17:45 | b01 세션 사망(터널 실패) → 예약 자동 비활성. 관측#1: 세션 조기 사망 가능 |
| 10-01 21:09 | b02 교체+컨트롤 리셋 후 **판매자 단계 프록시 경유 첫 성공** (63명 완성형) |
| 10-01 22:30 | B 재등록(b02), 카테고리 가족 파라미터화 완료(477 passed) |
| 10-02 03:50 | b02 세션 사망(5번째 유휴 간격) — 패턴 확정: 유휴 중 확률적 사망 |
| 10-02 09:58 | b03 교체 복구 — 판매자 45명 추가, 완성형 119명 |
| 10-02 정오 | **세션 자동 교체 구현**(exit_ip 실패→sid 증가 롤오버, 485 passed), B 연장 등록 |
| 10-02 20:40 | **B 1차 가족 완주(28시간, 판매자 170명, 차단 0)** → 완성형 자동 복사·종료 30. 피기백으로 C 등록 완료(interp 장애 우회) |
| 10-02 21~22시 | interop 자체 복구. 완성형 한글 CSV 복사 버그 수정(ASCII 글로브)+누락 보완. B2(주방 2차) 재등록 22:10~, C 22:50~ |
| 10-02 22:10~ | B2 첫 실행 성공. 22:50 C 처녀항해 성공(c01 58.236.163.6, B와 별도 회선 확인) |
| 10-03 00:10 | C 세션 실행 중 사망(API 57회 후 Failed to fetch) → 자동 비활성. 관측#2: 실행 중 사망도 존재 |
| 10-03 09:0x | **A 완성형 마무리 완료 — 판매자 1,129명 100% 완성형**(canary NameError 우회). C 복구 재등록(09:40~, 자동교체가 c01→c02 처리 예정). B2 연장(10:10~23:30) |
| 10-03 09:4x | canary 버그 2건 직접 수정(NameError·한글 글로브). 다음 가족 파일 준비(B3·C2). 트래픽 견적 확정. 커밋 진행(본 문서와 함께) |

### 누적 실적 (10-03 09:40 기준, 쿠팡 차단 0건·503 0건)

| 캠페인 | 판매자 완성형 | 3P 상품 | 상태 |
|---|---|---|---|
| A 견과/축산 (집 회선) | **1,129명** | 3,268 | ✅ 완주 — `garbage\PatchrightTop1000_final_20261003_0910` |
| B 주방 1차 (Decodo) | **170명** | 217 | ✅ 완주 — `garbage\PatchrightTop1000B_final_20261002_2040` |
| B2 주방 2차 (Decodo) | 113명 | 130 | 진행 중 (~23:30) |
| C 반려동물 (Decodo) | 50명 | 67 | 09:40 재개 (~21:40) |
| **합계** | **1,462명** | 3,682 | 병렬화 이후 41시간 |

- 병렬화 효과 실측: 단일 인스턴스 대비 판매자 확보 속도 약 2배+ (A 단독기준 하루 ~500명 → 현재 A완주+B 2캠페인 완주+C 진행)
- 트래픽 견적: Decodo 경유 목록 111장 + API 388회 ≈ **0.17~0.67GB** (플랜 여유 충분. 정확치는 대시보드 확인 필요)
- 세션 신뢰성 관측: b01 수명 ~1.5h, b02 ~6.5h, b04 12h+ 생존 — 사망은 확률적, 자동 교체로 무인 운용 확보

### 사건·장애 등기부 (전체)

1. b01 세션 사망(10-01 17:45) — 자동 교체 도입 전, 수동 복구
2. b02 세션 사망(10-02 03:50) — 자동 교체 도입 계기
3. WSL interop 소실(10-02 20:3x~21:xx) — 피기백 등록으로 우회, 자체 복구
4. 완성형 한글 CSV 복사 누락(인코딩) — ASCII 글로브 수정
5. C 실행 중 세션 사망(10-03 00:10) — 컨트롤 리셋+자동 교체로 복구
6. A 완성형 마무리 크래시(canary NameError) — 수정 코드로 마무리 수행, canary도 10-03 수정

### 다음 가족 준비 (완주 시 1줄 등록, §8 명령 참조)

- **B3** `coupang_categories_kitchen_b3.json` — 주방잡화 399678 · 주방수납/정리 186147 · 보온/보냉용품 185955
- **C2** `coupang_categories_pet_c2.json` — 관상어 용품 118879 · 소동물/가축용품 121391 · 고양이 영양제 486331

### 10-04 09:00 자동화 예약

가족 로테이션+결과 보고 자동화 등록(완주 시 B3/C2 재등록, 실패 시 진단 보고만).

## 19. 10-04 새벽 — B2 완주·로테이션 + 앱 탭 제품화 완료

- **B2 주방 2차 완주** (10-03 19:30, 완성형 판매자 69명 — `garbage\PatchrightTop1000B_final_20261003_1930`).
  밤샘 감시자의 완주 감지 패턴 불일치(`pipeline_complete` vs 실제 자식 exit-30 이벤트)로
  B3 자동 등록은 못 함 — 패턴 교훈 기록. **B3(주방 3차: 주방잡화·주방수납·보온/보냉) 수동 등록 완료**
  (10-04 03:30~15:30, 10회, sid b04 — 재등록.
- **C 반려동물 가족 진행 중** — 마지막 트리거(10-03 21:40) 후 대기 상태였던 것을
  연장 등록(10-04 04:10~16:10, 10회, sid c03 — 재등록). 첫 등록 시도는 출력 파이프가 오류를 삼켜 조용히 실패(예전 트리거 재활성만 됨) — 등록 후 트리거 boundary 직접 확인으로 발견·교훈: 등록 결과는 반드시 firstTrigger 원문으로 검증할 것.
- **앱 탭 제품화 완료** (별도 브랜치 `feat/coupang-parallel-category-tab`, 3커밋 푸시):
  '쿠팡 카테고리' 탭을 병렬 다중 인스턴스 엔진으로 교체 — 코어 포팅 7종·매니저·워커·
  패널·README, WSL 921 passed + Windows 게이트 914 OK, 라이브 스모크(직접+Decodo 동시)
  성공, EXE 빌드+`--verify-runtime` 통과. 배포(서명)·main 머지·새 PC 실측은 사용자 결정 대기.
- 운용 누적(10-04 02:40): A 1,129 + B1 170 + B2 69 완주, C 진행 중. 쿠팡 차단 0건 유지.

## 20. 10-04 아침 — WSL 의존성 사고 해구 + 로컬 경로 이전 + 앱 스모크

### 사고: 새벽 트리거 전멸 (03:30 B3 / 04:10 C, 종료 코드 2)
원인: 작업이 `\\wsl.localhost\...\run_patchright_top1000.ps1`을 실행하는데 **감시자 종료(02:38)
후 WSL이 유휴 종료하면서 UNC 경로 소실** → 파일 없음(2). 그동안 무인 운용이 가능했던 건
세션의 백그라운드 감시자가 WSL을 우연히 살려두고 있었기 때문(구조적 결함).
### 해구: 운용 스크립트의 Windows 로컅 이전
- `C:\Users\dltnd\SellerCollectorOps\scaleout\` 로 전체 복사(decodo 자격 포함) —
  작업 등록도 로컬 경로로 재등록(C 08:05~, B 09:10~ 각 10회).
- **로컬 경로 실세션 검증 완료**: B3 주방잡화 8페이지 200, 3P 70개, 종료 0 (07:44).
- 교훈: 무인 예약은 부팅 경로(\\wsl.localhost)에 의존하지 말 것 — WSL 자산은 개발용으로만.

### 앱(병렬 탭 빌드) 실동작 확인 (사용자 요청)
- `SellerCollector.exe`(parallel-tab 빌드) GUI 부팅 정상: 오늘 07:44:48 시작 로그 +
  Gmarket 진단 초기화 + 프로세스 생존·정리. `--verify-runtime`도 통과(10-03).
- 배포 서명은 사용자 지시로 **건너뜀**.
