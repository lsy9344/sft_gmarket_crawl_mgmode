# Coupang OMP 수집 모듈 — 구현 계획서

> 작성일: 2026-07-25 / 최종 개정: rev.4
> 대상 코드베이스: `sft_gmarket_crawl_mgmode` (Gmarket 판매자 수집기 v2.0, commit `76a0301`)
> 근거 문서: `coupang_crawl/` 7개 연구 문서 + `WORK_ORDER.md` + `TECH_SPEC.md`
> 검증 환경: `.venv` / Python 3.12 / `scrapling 0.4.11` / PyQt6
>
> **rev.2 반영 — 수집 항목 특이사항 2건**
> ① **동일 사업자번호 제거** → 출력 단위가 사업자 단위로 바뀜 (AD-10)
> ② **파워셀러 여부** → 확보 위치가 처리량을 좌우함 (AD-11)
>
> **rev.4 — 아키텍처 정정 (필독).** rev.2/3는 사업자정보(회사명·대표자명·
> 사업자등록번호·주소·전화)를 **공정위(FTC) DB 매칭**
> 으로 확보하는 것을 설계했다. 이는 잘못된 전제였다.
>
> **필수 수집 항목은 Coupang 상품 상세페이지 최하단 사업자정보 란에 전부
> 존재한다** (`PAGE_STRUCTURE.md` §3.2). 통신판매업신고번호는 수집 대상이
> 아니다. Gmarket이 `mg.gmarket.co.kr` 를 직접 읽어 판매자 정보를 얻듯,
> Coupang도 자신의 상세페이지 최하단을 직접 읽어 얻는다 — 외부 정부 DB를
> 조회·매칭할 이유가 없다. **rev.4는 공정위 연동을 전면 제거**하고,
> 상세페이지 직접 스크래핑을 사업자정보의 유일한 경로로 확정한다.

---

## 목차

| § | 내용 |
|---|------|
| 1 | 범위와 목표 |
| 2 | 통합 원칙 — Gmarket 무회귀 불변식 |
| 3 | 핵심 아키텍처 결정 (AD-1, AD-2, AD-5~AD-11 — AD-3/AD-4는 rev.4에서 폐기) |
| 4 | 연구 문서 정정 사항 (실측 검증 결과) |
| 5 | 파이프라인 설계 (Stage A~D) |
| 6 | 데이터 모델 / 출력 스키마 |
| 7 | 저장 계층 변경 명세 (Storage 파라미터화) |
| 8 | 모듈별 구현 명세 |
| 9 | 안티봇 / 예산 / 차단 복구 제어 |
| 10 | UI 통합 설계 |
| 11 | 설정 · 시크릿 관리 |
| 12 | 테스트 전략 |
| 13 | 구현 우선순위와 단계별 산출물 |
| 14 | 검증 기준 (Acceptance Criteria) |
| 15 | 리스크 레지스터 |
| 16 | 착수 전 확인이 필요한 결정 사항 |

---

## 1. 범위와 목표

### 1.1 목표

기존 Gmarket 수집기(`app/`)에 **Coupang OMP 판매자 사업자정보 수집 기능**을
추가한다. 기존 Gmarket 수집 동작은 1바이트도 바뀌지 않는다.

최종 산출물: Gmarket 스키마와 호환되는 **사업자정보 레코드** (JSON + CSV).
전부 **Coupang 자체 상품 상세페이지를 직접 스크래핑**해서 얻는다 — 외부
DB 연동 없음.

**출력 단위는 "사업자 1행"이다** (상품 1행이 아니다). 동일 사업자번호는
제거·병합되며, 상품 원본은 별도 파일로 보관한다.

### 1.2 수집 가능성 요약 (`DATA_FIELDS_MAPPING.md` 기준)

| 필드 | Gmarket | Coupang 확보 경로 | 판정 |
|------|---------|------------------|------|
| product_id | goodscode | `li[data-product-id]` (리스팅) | 직접 |
| url | item.gmarket | `/vp/products/{id}` 조합 | 직접 |
| store_name | `h3.store-title` | `span.prod-sale-vendor-name` (리스팅) | 직접 |
| company_name | mg.* | **상세페이지 최하단 사업자정보 란 직접 파싱** | 직접 |
| ceo_name | (Gmarket 불가) | **상세페이지 최하단 직접 파싱** | **Gmarket보다 우수** |
| business_number | mg.* | **상세페이지 최하단 직접 파싱** | 직접 |
| address | mg.* | **상세페이지 최하단 직접 파싱** | 직접 |
| phone | mg.* | **상세페이지 최하단 직접 파싱** (마스킹 가능) | 직접 |
| email | mg.* | **불가** | 미수집 |
| **power_seller** | (파워딜러 상당) | **리스팅/상세 배지** | **⚠ 위치 미확정 (AD-11)** |

**6/7 Gmarket 필드를 상세페이지에서 직접 확보** (이메일만 불가). 모든
필드가 **단일 경로**(Coupang 자체 페이지)에 의존하므로, 그 경로가 막히면
대체 확보 방법이 없다 — 이것이 계획 전체에서 가장 중요한 제약이다.

### 1.2b 수집 항목 특이사항

| # | 요구 | 성격 | 반영 |
|---|------|------|------|
| 1 | **동일 사업자번호 제거** | **구조 변경** | AD-10 / §6.3 / §13 P1-7 |
| 2 | **파워셀러 여부** | 필드 추가 + 스파이크 의존 | AD-11 / §13 P0-3 체크리스트 |

두 항목 모두 단순 필드 추가가 아니다. 사업자번호는 **수집이 끝나야 알 수
있는 키**라 중복 제거를 수집 이전에 할 수 없고, 파워셀러는 **어디서 잡히느냐가
처리량을 좌우한다**.

### 1.3 비목표 (이번 계획에서 제외)

- Coupang 로켓배송(직매입) 상품 — OMP는 3자 판매자 전용
- 모바일 앱 API 인터셉트 (`COLLECTION_STRATEGY.md` 기각 경로) — SSL Pinning
  우회는 기술적 보호조치 무력화에 해당해 법적 리스크가 크다. 채택하지 않는다.
- 유료 스크래핑 API 연동 — 직접 스크래핑 스파이크(P0-3) 실패 시의 대안으로만 검토
- Coupang 상품 가격/리뷰의 지속 모니터링 (1회성 스냅샷만)
- **공정위(FTC) 등 외부 정부 DB 연동** (rev.4에서 전면 제거) — 필수 항목이
  전부 Coupang 자체 페이지에 있으므로 불필요

### 1.4 전제 조건 (충족되지 않으면 수집 불가)

| 전제 | 상태 | 비고 |
|------|------|------|
| 한국 주거용 프록시 계정 | **미확보** | §16 결정 필요. **없으면 파이프라인 전체가 성립하지 않음** |
| 상세페이지 사업자정보 셀렉터/XHR 패턴 | **미검증** | §4.2 — 스파이크 필요. **대체 확보 경로가 없으므로 이 스파이크가 프로젝트 성립의 절대 전제조건** |

---

## 2. 통합 원칙 — Gmarket 무회귀 불변식

`app/core/crawler.py` · `app/core/storage.py` 는 6차에 걸친 리뷰로
데이터 유실 방지 프로토콜(fail-closed 승격, 원자적 쓰기, 격리 백업,
manifest 검증)이 촘촘히 박혀 있다. 이 코드를 갈아엎지 않는다.

**불변식 (모든 PR에서 검증)**

- **INV-1** 기존 테스트 6개 파일 전부 무수정 통과
  (`tests/test_crawler.py` 864줄, `tests/test_storage.py` 394줄 포함).
- **INV-2** Gmarket 실행 경로에서 생성되는 파일명 · 파일 내용 · CSV 헤더 ·
  `collected_ids.json` 포맷이 이전과 완전히 동일.
- **INV-3** Coupang 코드가 실패해도 Gmarket 수집이 영향받지 않는다
  (import 격리: `app/core/crawler.py` 는 Coupang 모듈을 import 하지 않는다).
- **INV-4** `scrapling` 등 무거운 의존성은 **함수 내부 지연 import**.
  `app/core/prescan.py:66` 의 기존 패턴을 따른다.
- **INV-5** 신규 엔진 코드는 Qt 비의존 (`app/core/base.py` Control + 콜백).
  워커만 `pyqtSignal` 로 중계한다.

**변경 허용 범위 (기존 파일)**

| 파일 | 변경 성격 | 위험도 |
|------|-----------|--------|
| `app/core/storage.py` | 상수 → `PlatformProfile` 주입 (기본값 = 현재 값) | 중 |
| `app/core/crawler.py` | `goodscode` 하드코딩 3곳 → profile 참조 | 중 |
| `app/core/base.py` | `Control.sleep()` 메서드 **추가만** | 하 |
| `app/ui/main_window.py` | 본문을 탭 위젯으로 감싸기 (속성명 유지) | 중 |
| `app/main.py` | 윈도 타이틀 문구만 | 하 |
| `requirements.txt` / `pyinstaller.spec` | **변경 없음** (rev.4: 신규 의존성 없음) | — |

---

## 3. 핵심 아키텍처 결정 (AD)

### AD-1. 중복 제거 단위를 "상품"이 아니라 "판매자"로 둔다 ★ 최중요

**문제.** `IMPLEMENTATION_ARCHITECTURE.md` 의사코드는 `collected_ids` 에
`product_id` 를 넣고 상품마다 상세 페이지를 연다. 그런데 우리가 원하는 것은
**상품 정보가 아니라 판매자 사업자정보**다. 한 판매자가 OMP에 상품 수십 개를
올리므로, 상품 단위로 상세 페이지를 열면 같은 사업자정보를 수십 번 재수집한다.

**결정.** 판매자 키(`seller_key` = 정규화된 store_name)를 1급 중복 제거
키로 삼는다. 파이프라인은:

```
상품 목록(대량, 저비용)  →  판매자 집합으로 축약(고비용 구간 진입 전)
                          →  판매자 단위로만 상세페이지 직접 스크래핑
                          →  확보한 판매자 정보를 상품 행으로 다시 fan-out
```

**효과 (가정 명시, rev.4 재산정).** OMP 5페이지 = 600상품, 상품당 판매자
중복도 2.5 → 고유 판매자 240명. **rev.4: 사업자정보는 상세페이지에서만
얻으므로 축약된 240명 전원을 방문한다** (외부 DB로 방문 자체를 줄일
방법은 없다).

| 방식 | 상세 페이지 fetch 수 | 소요 (150초/건 상각) |
|------|--------------------|--------------------|
| 상품 단위 (D2 없음) | 600 | ≈ 25시간 |
| **판매자 단위 (본 계획, 전원 방문)** | **240** | **≈ 10시간** |

*상각 150초/건 = 세션 내 지연 12초 + 쿨다운 750초 ÷ 배치 5건.*
중복도는 Stage A 실측 후 재산정한다. **D2(판매자 단위 축약)가 유일한
비용 절감 수단이다** — rev.2/3가 가정했던 "공정위 매칭으로 방문을 절반
줄인다"는 근거가 없어 폐기됐다(`COLLECTION_STRATEGY.md` rev.4 참조).

**저장 구조.** 상태 파일을 분리 보관한다.

- `coupang_collected_ids.json` — 처리 완료된 `product_id` 집합
  (기존 Storage 프로토콜 재사용)
- `coupang_sellers.json` — `seller_key → SellerProfile` 캐시
  (상세페이지에서 직접 확보한 사업자정보 + 확보 시각). 재방문 방지.

### AD-2. Storage/Crawler를 `PlatformProfile` 로 파라미터화한다

**문제.** `Storage` 는 Gmarket 스키마에 하드 결합돼 있다.

- `storage.py:487` — CSV writer가 `RECORD_FIELDS`(Gmarket 11필드) 고정.
  Coupang 필드를 넘기면 `extrasaction="ignore"` 로 **조용히 소실**.
- `storage.py:273` — 체크포인트 검증이 `item.get("goodscode")` 필수 요구.
  Coupang 레코드는 `product_id`/`business_number` 라 **전량 손상으로 오판 → 격리**.
- `storage.py:102` — 체크포인트 접두사가 `.partial_gmarket_fast_`.
- `config.COLLECTED_IDS_FILENAME` 공유 시 Gmarket goodscode와 Coupang
  ID가 한 파일에 섞인다.

**결정.** `app/core/schema.py` 에 `PlatformProfile`(frozen dataclass)을 두고
`Storage(output_dir, profile=GMARKET_PROFILE)` 로 주입한다. 기본값이 현재
상수와 동일하므로 Gmarket 경로는 무변화(INV-2).

**대안 검토.** `CoupangStorage` 별도 클래스 → 승격/manifest/격리 로직
600줄 중복. 기각.

### AD-3 / AD-4. (rev.4에서 폐기)

rev.2/3의 AD-3("공정위 매칭 2단계 알고리즘")과 AD-4("확신 없는
사업자정보는 쓰지 않는다" — 퍼지 매칭 오매칭 방지 규칙)는 **공정위 연동
자체가 제거되면서 불필요해졌다.** 매칭 알고리즘이 없으므로 매칭 신뢰도
개념도 없다.

AD-4의 데이터 무결성 정신("확신 없는 값을 쓰지 않는다")은 완전히
사라지지 않는다 — AD-10(사업자번호 정규화·체크섬 검증)이 이를 계승한다.
다만 이제 지키는 대상은 "동명 사업자 오매칭"이 아니라 "DOM 오인식으로
잘못된 문자열을 뽑는 파싱 오류"다.

이후 문서에서 AD-7, AD-10, AD-11 은 다른 문서에서도 이 번호로
참조되므로(`COLLECTION_STRATEGY.md`, `DATA_FIELDS_MAPPING.md`,
`BYPASS_TECHNICAL_GUIDE.md`) **번호를 유지**한다. AD-3/AD-4는 결번으로 둔다.

### AD-5. 상세 페이지 사업자정보는 **최하단 DOM 파싱 우선, XHR 캡처 폴백**

`PAGE_STRUCTURE.md` §4 정정: 사업자정보는 "팝업"이 아니라 **상세페이지
최하단 사업자정보 란**에 직접 표시된다. 통신판매업신고번호는 수집 대상이
아니며, **사업자등록번호**가 핵심 수집 항목이다.

`scrapling 0.4.11` `StealthySession` 은 세션 레벨
`capture_xhr`(URL 패턴 문자열)을 지원하고, 응답 객체가
`Response.captured_xhr: list[Response]` 를 채운다
(`scrapling/engines/toolbelt/custom.py:81`).

**결정.** 상세페이지 최하단 사업자정보 란을 DOM 파싱으로 직접 추출한다.
JS 렌더링으로 DOM 파싱이 실패할 경우 XHR 캡처(JSON)를 폴백으로 사용한다.
**rev.4: 이 경로가 사업자정보를 얻는 유일한 방법**이므로 견고성이 그만큼
더 중요하다.

- 1순위: `network_idle` 후 최하단 사업자정보 섹션 DOM 파싱 (클릭 불필요).
- 2순위(폴백): `capture_xhr` 로 사업자정보 AJAX 응답 캡처.
- `page_action` 콜백으로 최하단까지 스크롤하여 사업자정보 란을 노출시킨다.
- XHR이 잡히지 않으면 DOM 파싱으로 폴백하고, 그때는 `adaptive=True` 사용.
- **그래도 실패하면 재시도(§8.1) 후 미확정으로 남긴다 — 더 이상 갈 곳이 없다.**

**전제**: XHR 엔드포인트 패턴은 §4.2 스파이크에서 실측 확정해야 한다.

### AD-6. 쿨다운은 반드시 **중단 가능한 분할 대기**로 구현한다

Coupang 세션 쿨다운은 600~900초, 차단 복구는 900~1800초다. `time.sleep(900)`
을 그대로 쓰면 [취소] 버튼이 최대 30분간 먹통이 되고, 창을 닫을 때
`closeEvent`(`main_window.py:512`)가 좀비 스레드를 기다리며 UI가 얼어붙는다.

**결정.** `app/core/base.py` 의 `Control` 에 분할 대기 메서드를 **추가**한다.
`Prescanner._wait_blocked`(`prescan.py:145`)가 이미 쓰는 패턴의 일반화다.

```python
def sleep(self, seconds: float, poll: float = 0.5,
          on_tick: Callable[[float], None] | None = None) -> None:
    """취소/일시정지에 반응하는 분할 대기. 취소 시 CancelledError 전파."""
```

`on_tick(remaining)` 으로 UI에 남은 쿨다운을 초 단위 표시한다 — 15분간
멈춘 것처럼 보이는 진행바를 방지한다.

### AD-7. 일일 예산은 **디스크에 영속화**한다

`MAX_DAILY_ITEMS = 1000` 은 0% 차단률의 근거가 되는 하드캡이다. 메모리
카운터만 쓰면 앱 재시작으로 리셋되어 캡이 무의미해진다.

**결정.** `coupang_budget.json` 에 `{date, listing_fetches, detail_fetches,
blocks, last_block_at}` 를 원자적으로 기록. 날짜(KST)가 바뀌면 리셋.
차단 직후 재시작해도 `last_block_at` 기준 복구 쿨다운을 이어서 적용한다.

### AD-8. 프록시 미설정 시 **실행을 거부**한다 (fail-closed)

`PLATFORM_ANALYSIS.md`: 데이터센터/해외 IP는 즉시 차단, 한국 주거용 IP 필수.
프록시 없이 실행하면 사용자 실 IP가 Akamai 평판 목록에 오를 수 있고, 그
피해는 되돌릴 수 없다.

**결정.** `PROXY_REQUIRED=True`(기본)일 때 프록시 미설정이면 Stage A/C를
시작하지 않고 UI에 명시적 안내. 사용자가 의도적으로 끄려면 설정에서 명시적
체크 해제 + 확인 대화상자를 거치게 한다.

**rev.4 재강조.** Stage A(리스팅)와 Stage C(상세 스크래핑) 둘 다 프록시
필수이고, 나머지 Stage(B, D)는 그 산출물을 가공만 하므로 **파이프라인
전체가 프록시에 의존**한다. 부분 가치를 제공하는 "프록시 없이도 동작하는
경로"는 존재하지 않는다.

### AD-9. Stage 간 실행을 **분리**한다 (단일 "수집 시작" 버튼 없음)

Gmarket은 조사 2분 + 수집 35분이라 한 번에 돌린다. Coupang은 Stage C(상세
스크래핑)가 수 시간~수십 시간이다. 한 버튼에 묶으면 중간 실패 시 전부 날아간다.

**결정.** UI에서 Stage를 독립 실행 가능하게 한다
(`OMP 스캔` / `판매자 축약` / `상세 스크래핑` / `내보내기`).
각 Stage는 자기 산출물을 디스크에 남기고 다음 Stage는 그것을 입력으로 읽는다
— 즉 Stage 경계가 곧 체크포인트다.

### AD-10. 동일 사업자번호 제거 — 출력 단위를 사업자로 바꾼다 ★

**문제.** 산출물의 목적은 사업자 목록이다. 그런데 사업자번호는 상세페이지
스크래핑을 거쳐야 확정된다 — 즉 **최종 중복 제거 키를 수집 전에는 알 수
없다.** 게다가 한 사업자가 스토어를 여러 개 운영하면 AD-1의 판매자 단위
축약(D2)만으로는 같은 사업자가 여러 행으로 남는다.

**결정. 3단계 중복 제거로 나눈다.**

| 단계 | 키 | 시점 | 목적 |
|------|-----|------|------|
| D1 | `product_id` | 리스팅 직후 | 재수집 방지 (기존 `collected_ids` 프로토콜) |
| D2 | `seller_key` | 리스팅 직후 | **고비용 구간 진입 전 축약** (AD-1) |
| **D3** | **`business_number`** | Stage D | **최종 산출물 유일성** |

**세부 규칙**

1. **정규화 + 체크섬.** 표기가 `123-45-67890` / `1234567890` 로 섞이므로
   숫자 10자리를 정규 키로 삼고, **국세청 체크섬**으로 검증한다. 체크섬
   실패는 파싱 오류 신호이므로(rev.4: 오매칭이 아니라 DOM 오인식) 중복
   제거 키로 쓰지 않고 `resolved=False` 로 강등한다 — 잘못된 번호를
   통과시키면 D3의 유일성 보증 자체가 무너진다.
2. **대표 레코드 선택.** rev.4: 모든 레코드의 출처가 상세페이지 직접
   스크래핑으로 동일하므로 "방법 우선순위"는 의미가 없다. 대신:
   채워진 핵심 필드 수 내림차순 → 수집 시각 오름차순(먼저 수집한 것).
3. **버리지 않고 병합한다.** `store_names` / `store_count` /
   `product_count` 를 집계하고 `power_seller` 는 OR 로 합친다.
   "이 사업자가 스토어를 몇 개 운영하는가"는 그 자체로 가치 있는 신호다.
4. **번호 미확정 행은 분리한다.** `business_number` 가 비면 중복 제거가
   불가능하다. 본 목록에 섞으면 "중복 없음" 보증이 깨지므로
   `coupang_unresolved_{ts}` 로 분리 보관한다(버리지는 않는다 — 재시도로
   확정될 수 있는 후보다).
5. **실행 간 영속 제거.** 출력한 사업자번호를 `coupang_business_index.json`
   에 누적하고 `seller_key → business_number` 역인덱스를 함께 유지한다.
   다음 실행에서 **고비용 구간에 진입하기 전에** 스킵할 수 있어야 한다 —
   이미 확보한 사업자에 프록시 비용을 다시 쓰지 않는 것이 핵심이다.

상세 규칙은 `DATA_FIELDS_MAPPING.md` §3.

### AD-11. 파워셀러는 **가장 싼 위치에서** 확보한다

**문제.** 배지 문구("파워셀러")는 확인됐으나 DOM 위치가 미확정이다.
어디서 잡히느냐에 따라 비용이 완전히 다르다.

| 확보 위치 | 추가 요청 | 커버리지 |
|----------|----------|---------|
| **리스팅 배지** | **0** | **전 판매자** |
| 상세/XHR | 0 | **전 판매자** (rev.4: Stage C가 전원 방문하므로) |
| 판매자 스토어 페이지 | 판매자당 +1 | 전 판매자, 단 Stage C 처리량 **약 1/2** |

**결정.**

1. **스파이크에서 리스팅 배지를 최우선 확인한다**(P0-3 체크리스트). 여기서
   잡히면 사실상 무료다. 다만 rev.4에서는 상세/XHR(2순위)도 전 판매자를
   커버하므로, 후보 1의 가치는 "무료"라는 점 하나로 좁혀진다.
2. **원문을 보존한다.** `seller_grade` 에 배지 텍스트를 그대로 저장하고
   `power_seller` 는 파생 불리언으로 둔다. 불리언만 저장하면 플랫폼이
   문구를 바꿨을 때 과거 데이터를 재해석할 수 없다.
3. **미발견이 파이프라인을 막지 않는다.** 빈 값 + 로그 경고 1회(건마다
   경고하면 로그가 무의미해진다)로 처리하고 수집을 계속한다.
4. **병합은 OR.** 한 사업자의 스토어 중 하나라도 파워셀러면 그 사업자는
   파워셀러다.
5. **스토어 페이지까지 가야 한다면 수집 여부를 재협의한다**(§16-Q6) —
   처리량 절반은 사용자가 판단할 비용이다.

---

## 4. 연구 문서 정정 사항 (실측 검증 결과)

연구 문서의 코드 예시를 그대로 옮기면 동작하지 않는 지점들. **구현 시 아래를
따른다.**

### 4.1 Scrapling API 오류

| 문서 위치 | 문서 기재 | 실제 (scrapling 0.4.11) |
|-----------|-----------|------------------------|
| `SCRAPLING_TECH_ANALYSIS.md` §3.4, §5.1, §5.2 | `session.fetch(url, proxies=PROXY)` | **`proxy=`** (단수). `StealthSession`/`StealthFetchParams` 모두 `proxy: str \| dict \| tuple` |
| §3.5 | "Scrapling이 자동으로 DoH 라우팅" | `dns_over_https: bool` **세션 파라미터로 명시 지정 필요** |
| §3.6 | `session.fetch(url, block_ads=True)` | `block_ads` 는 **세션** 파라미터, fetch 파라미터 아님 |
| §5.4 | `Spider.add_session(sid=..., proxies=[...])` | 세션은 `proxy_rotator: ProxyRotator` 사용. `ProxyRotator(proxies, strategy=cyclic_rotation)` |
| §6.1 (구판) | `FetcherSession(...).get(...)` 를 클래스에서 직접 | `get/post/put/delete` 는 **컨텍스트 진입 후에만** 노출 (`with FetcherSession(...) as s: s.get(...)`) |
| §10 | `page.css("...::text").get()` | Scrapling `Selector` API. 파싱 헬퍼는 스파이크에서 실 응답으로 확정 |

검증 근거: `scrapling/engines/_browsers/_types.py` 의 `StealthSession` /
`StealthFetchParams` TypedDict, `scrapling/engines/toolbelt/proxy_rotation.py`.

**실측으로 확인된 유용한 미문서화 파라미터** (연구 문서에 없음):

| 파라미터 | 위치 | 용도 |
|----------|------|------|
| `capture_xhr: str \| None` | 세션 | 사업자정보 AJAX 응답 직접 캡처 (AD-5) — **rev.4: 사업자정보 확보의 유일한 경로** |
| `proxy_rotator: ProxyRotator` | 세션 | 세션별 IP 회전 |
| `retries` / `retry_delay` | 세션 | 내장 재시도 |
| `page_action: Callable` | 세션/fetch | 스크롤·클릭 등 행동 시뮬레이션 주입점 (L4 우회) |
| `real_chrome: bool` | 세션 | Firefox 소수 브라우저 리스크 대안 |
| `wait_selector` + `wait_selector_state` | 세션/fetch | 동적 렌더 완료 대기 |

### 4.2 미검증 셀렉터 — 코딩 전 스파이크 필수 ★★ (rev.4: 대체 경로 없음)

`SCRAPLING_TECH_ANALYSIS.md` (구판) 의 상세 페이지 셀렉터
(`.seller-company-name`, `.seller-ceo-name`, `.seller-business-number`,
`.seller-address`, `.seller-phone`)는 **어느 문서에서도 실측 근거가 없다.**
`PAGE_STRUCTURE.md` 는 같은 정보를 "동적 렌더링 팝업, 정적 HTML에 없음"이라고
기술해 서로 모순된다.

**결정.** Stage C 구현 전에 **셀렉터 발견 스파이크**(P0-3, §13)를 수행해
실 HTML/XHR 응답을 `coupang_crawl/fixtures/` 에 저장하고, 그 픽스처를 근거로
파서와 테스트를 작성한다. 픽스처 없이 파서를 먼저 쓰지 않는다.

리스팅 셀렉터(`ul#productList`, `li[data-product-id]`,
`span.prod-sale-vendor-name` 등)도 동일하게 1회 실측 확인 후 확정한다.

**rev.4: 이 스파이크가 실패하면 우회할 방법이 없다.** rev.2/3는 실패
시 "공정위 단독 도구로 축소"라는 퇴로가 있었지만, 공정위 연동을 제거한
지금은 유료 API 도입 또는 프로젝트 재검토만 남는다(§16-Q1).

### 4.3 robots.txt / ToS

`PAGE_STRUCTURE.md` 가 명시하듯 `/np/omp` 는 robots.txt Allow 목록에 없고
"crawling may violate ToS"로 표기돼 있다. 계획서 차원에서 다음을 반영한다.

- 설정에 `RESPECT_ROBOTS` 스위치를 두고, 기본값은 **robots.txt 허용 경로
  (`/np/categories/`, `/np/search`) 우선 사용**. OMP 직접 스캔은 사용자가
  명시적으로 켤 때만 활성화한다.
- 수집 대상은 공개된 상품 목록과 **법이 공시를 의무화한 사업자정보**로 한정.
  로그인 영역·개인정보는 다루지 않는다.
- 실제 운영 여부는 사용자 판단 사항이며(§16-Q4), 계획서는 두 경로를 모두
  구현 가능하게 설계한다.

---

## 5. 파이프라인 설계 (rev.4 — 4 Stages)

```
┌──────────────────────────────────────────────────────────────────────┐
│ Stage A. OMP 리스팅 스캔           [StealthySession L3 + KR 프록시]   │
│  /np/omp?listSize=120&page=N (또는 /np/categories/{id})               │
│  추출: product_id, itemId, vendorItemId, store_name, price, rating    │
│        power_seller (파워셀러 배지)                                    │
│  제어: 페이지간 8~15초, 세션당 ≤5페이지, 일일 예산 차감               │
│  산출물: coupang_listing_{ts}.json  (상품 원본 레코드)                │
├──────────────────────────────────────────────────────────────────────┤
│ Stage B. 판매자 축약 (D2)          [로컬, 네트워크 0]                 │
│  상품 → seller_key group by → 기확보 사업자 스킵(역인덱스)            │
│  수천 상품 → 수백 판매자                                              │
│  산출물: 방문 대상 판매자 목록 (판매자당 대표 상품 1건)               │
├──────────────────────────────────────────────────────────────────────┤
│ Stage C. 판매자 상세 직접 스크래핑  [StealthySession L3 + KR 프록시]  │ ★ 사업자정보 유일 경로
│  대상: Stage B 로 축약된 **모든** 판매자                              │
│  판매자당 대표 상품 1건의 상세 페이지 → 최하단 사업자정보 란 DOM 파싱  │
│  (XHR 캡처는 폴백)                                                    │
│  획득: company_name, ceo_name, business_number, address, phone        │
│        — Gmarket mg.* 와 동일한 원리                                  │
│  실패 시: 재시도 → 그래도 실패면 미확정 (대체 경로 없음)              │
│  제어: 배치 5건/세션, 쿨다운 600~900초, 차단 2회 시 중단               │
│  산출물: coupang_sellers.json 갱신 (건 단위 durable)                  │
├──────────────────────────────────────────────────────────────────────┤
│ Stage D. 사업자 집계 + 출력       [로컬]                              │
│  D3: 동일 business_number 제거·병합 (AD-10)                           │
│    - 대표 선택: 필드 충족도 → 수집순                                  │
│    - 병합: store_names/store_count/product_count, power_seller = OR   │
│    - 번호 미확정 행은 분리 (주 목록의 유일성 보증 유지)               │
│  BusinessIndex 갱신 (다음 실행에서 고비용 진입 전 스킵)               │
│  산출물: coupang_business_{ts}.json/.csv    ← business_number 유일    │
│         coupang_unresolved_{ts}.json/.csv  ← 미확정 (재시도 후보)     │
└──────────────────────────────────────────────────────────────────────┘
```

> **결과를 먼저 저장한 뒤에만 인덱스를 커밋한다.** 기존 `crawler.py` 의
> 내구성 프로토콜(`_commit_checkpoint`, `crawler.py:451`)과 같은 순서다 —
> 순서가 뒤바뀌면 "인덱스에는 있는데 파일에는 없는" 사업자가 생겨
> 영원히 재수집되지 않는다.

### 5.1 Stage A 의사코드 (핵심 제어 흐름)

```python
def scan_listing(self, plan: ListingPlan) -> ListingResult:
    from scrapling.fetchers import StealthySession          # 지연 import (INV-4)

    budget = self.budget.load()                              # AD-7
    detector = BlockDetector(limit=cfg.ABORT_AFTER_BLOCKS)
    products: list[dict] = []

    with StealthySession(
        headless=True,
        proxy=self.proxy.next(),                             # AD-8 / 정정 §4.1
        dns_over_https=True,
        block_ads=True,
        solve_cloudflare=True,
        timeout=cfg.FETCH_TIMEOUT_MS,
    ) as session:
        # 웜업: 홈 → (자연 유입 체인) → 목표. _abck 센서 실행 대기
        session.fetch(cfg.HOME_URL, network_idle=True)
        self.control.sleep(rand(*cfg.WARMUP_WAIT), on_tick=self.on_wait)  # AD-6

        for page_no in plan.pages:
            self.control.checkpoint()
            if budget.listing_exhausted():
                self.on_log("[예산] 일일 리스팅 한도 도달 — 중단"); break

            r = session.fetch(plan.url(page_no), network_idle=True,
                              google_search=False)
            budget.count_listing(); budget.save()            # 실패도 차감(요청은 나갔다)

            if detector.check(r):                            # 403/418/429/soft/키워드
                self.on_log(f"[차단] page {page_no}")
                if detector.should_abort():
                    budget.mark_block(); budget.save()
                    raise BlockedError(detector.reason)
                self.control.sleep(rand(*cfg.BLOCK_COOLDOWN), on_tick=self.on_wait)
                continue

            page_items = parse_listing(r)                    # §8 coupang_parser
            products.extend(page_items)
            self.on_page(page_no, len(page_items), len(products))

            self.control.sleep(rand(*cfg.PAGE_DELAY), on_tick=self.on_wait)

    return ListingResult(products, detector.stats, budget.snapshot())
```

### 5.2 Stage B 의사코드 (판매자 축약, 네트워크 0)

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

        if self.index.seller_known(seller_key):              # 이미 확보 — 스킵
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

### 5.3 Stage C 의사코드 (판매자 상세 직접 스크래핑, 배치 + 쿨다운)

```python
def enrich_sellers(self, targets: list[SellerTarget]) -> None:
    """rev.4: targets 는 Stage B 로 축약된 '전체' 판매자다.
    외부 DB 폴백이 없으므로, 여기서 실패한 판매자는 재시도 후 미확정으로 남는다.
    """
    detector = BlockDetector(limit=cfg.ABORT_AFTER_BLOCKS)

    for batch_no, batch in enumerate(chunked(targets, cfg.SESSION_BATCH_SIZE), 1):
        self.control.checkpoint()
        if self.budget.detail_exhausted():
            self.on_log("[예산] 일일 상세 한도 도달 — 다음 실행에서 계속"); return

        if batch_no > 1:                                     # 배치 간 쿨다운
            self.control.sleep(rand(*cfg.SESSION_COOLDOWN), on_tick=self.on_wait)

        with StealthySession(
            headless=True,
            proxy=self.proxy.next(),                         # 세션마다 IP 교체
            capture_xhr=cfg.SELLER_XHR_PATTERN,              # AD-5, 1순위 경로
            page_action=behavior.scroll_to_seller_section,   # L4 행동 시뮬레이션
            dns_over_https=True, block_ads=True,
        ) as session:
            session.fetch(cfg.HOME_URL, network_idle=True)
            self.control.sleep(rand(*cfg.WARMUP_WAIT), on_tick=self.on_wait)

            for target in batch:
                self.control.checkpoint()
                r = session.fetch(target.sample_url, network_idle=True)
                self.budget.count_detail(); self.budget.save()

                if detector.check(r):
                    if detector.should_abort():
                        self.budget.mark_block(); self.budget.save()
                        raise BlockedError(detector.reason)
                    break                                    # 세션 폐기 후 다음 배치

                profile, method = self._parse_seller(r)      # XHR 우선, DOM 폴백
                if profile is None:
                    continue                                 # 재시도는 상위 루프 담당

                brno = normalize_brno(profile.business_number)
                profile.business_number = brno if is_valid_brno(brno) else ""
                profile.resolved = bool(profile.business_number)
                profile.parse_method = method

                self.sellers.upsert(target.seller_key, profile)
                self.sellers.save()                          # 건 단위 durable (내구성 원칙)
                self.on_seller(target.seller_key, profile)

                self.control.sleep(rand(*cfg.ITEM_DELAY), on_tick=self.on_wait)

def _parse_seller(self, response) -> tuple[SellerProfile | None, str]:
    if (p := parse_seller_from_xhr(response.captured_xhr)):
        return p, "xhr"
    if (p := parse_seller_from_dom(response.html_content)):
        return p, "dom"
    return None, "none"
```

**내구성 순서 (기존 `crawler.py` 프로토콜 계승).**
결과를 디스크에 먼저 쓰고 → 그 다음에만 `collected_ids` 를 커밋한다.
`SellerStore.save()` 는 `Storage._atomic_write` 와 동일하게 tmp → replace.

### 5.4 Stage D 의사코드 (D3 중복 제거 + 출력)

```python
def export(self, label: str) -> ExportResult:
    profiles = self.sellers.all()
    by_seller = self.listing.group_by_seller()

    businesses, unresolved = dedupe_by_business_number(profiles, by_seller)

    fresh = [b for b in businesses if not self.index.known(b.business_number)]

    paths = self.storage.save_results([b.to_dict() for b in fresh], label=label)
    for b in fresh:
        self.index.add(b)
    self.index.save()

    unresolved_paths = self.storage.save_results(
        [p.to_dict() for p in unresolved], label=f"{label}_unresolved"
    )
    return ExportResult(paths, unresolved_paths, len(fresh), len(unresolved))
```

---

## 6. 데이터 모델 / 출력 스키마

### 6.1 `app/models/coupang_records.py`

```python
COUPANG_SUCCESS_FIELDS = ("business_number",)

@dataclass
class CoupangProduct:
    """Stage A 산출 — 리스팅에서 얻는 상품 원본."""
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
    power_seller: bool = False              # 리스팅 "파워셀러" 배지
    seller_grade: str = ""                  # 배지 원문 (AD-11 — 문구 변경 대비)
    source: str = ""                        # OMP 카테고리/정렬 라벨
    scanned_at: str = ""

@dataclass
class SellerProfile:
    """Stage B/C 산출 — 판매자 1명 (D2 축약 단위).

    rev.4: 사업자정보는 오직 상세페이지 직접 스크래핑에서만 채워진다.
    "매칭 신뢰도" 개념은 없다 — 확보했거나(그라운드 트루스) 못 했거나 둘 중 하나다.
    """
    seller_key: str                        # 정규화 store_name (조인 키)
    store_name: str = ""
    company_name: str = ""
    ceo_name: str = ""
    phone: str = ""
    business_number: str = ""              # 정규화 10자리 (체크섬 통과분만)
    address: str = ""
    power_seller: bool = False
    seller_grade: str = ""
    resolved: bool = False                 # 상세페이지에서 사업자정보를 확보했는지
    parse_method: str = "none"             # dom | xhr | none — 운영 모니터링용
    resolved_at: str = ""

    @property
    def is_resolved(self) -> bool:
        return bool(self.business_number)

@dataclass
class BusinessRecord:
    """Stage D 최종 출력 1행 — **사업자 단위** (D3 이후).

    상품 단위 레코드를 대체한다. 동일 사업자번호는 한 행으로 병합되며,
    상품 속성은 별도 리스팅 파일에 남는다.
    """
    business_number: str                   # ← 유일성 키
    # ... 아래 COUPANG_BUSINESS_FIELDS 순서와 1:1 대응
    def to_dict(self) -> dict: ...
    def is_valid(self) -> bool:
        return bool(self.business_number)
```

### 6.2 출력 필드 순서

두 산출물의 필드 순서는 `DATA_FIELDS_MAPPING.md` §6 이 단일 진실
공급원이다. 여기서는 요지만 옮긴다.

```python
COUPANG_BUSINESS_FIELDS = [          # 주 산출물 — 사업자번호 유일
    # ── Gmarket 호환 구간 ─────────────────────────────────
    "business_number", "company_name", "ceo_name",
    "email",                          # 항상 "" — Coupang 미공개
    "phone", "address",
    # ── 파워셀러 (AD-11) ──────────────────────────────────
    "power_seller", "seller_grade",
    # ── 사업자 집계 (AD-10 병합 결과) ─────────────────────
    "store_names", "store_count", "product_count",
    "sample_product_id", "sample_url",
    # ── 메타 ──────────────────────────────────────────────
    "source", "collected_at",
]

COUPANG_PRODUCT_FIELDS = [           # 부록 — Stage A 리스팅 원본
    "product_id", "item_id", "vendor_item_id", "url", "title", "store_name",
    "price", "rating", "review_count", "rocket_delivery",
    "power_seller", "seller_grade", "source", "scanned_at",
]
```

> **rev.4 제거**: `domain`, `business_status`, `handled_products` (공정위
> 전용 필드, 소스 소멸), `match_method`, `match_score`, `match_candidates`
> (매칭 알고리즘 자체가 없음).

`email` 은 스키마 정합성을 위해 유지하되 항상 빈 문자열이다
(Gmarket `ceo_name` 이 항상 빈 것과 같은 취급 — `WORK_ORDER.md §15` 관례).

### 6.3 출력 파일 구성

| 파일 | 프로필 | 유일성 | 내용 |
|------|--------|--------|------|
| `coupang_business_{ts}.json/.csv` | `COUPANG_BUSINESS_PROFILE` | **business_number** | 주 산출물 |
| `coupang_unresolved_{ts}.json/.csv` | 〃 | seller_key | 상세 실패 판매자 (재시도 후보) |
| `coupang_listing_{ts}.json` | — | product_id | 상품 원본 (부록) |

> **Storage 프로필이 2개 필요하다.** 사업자 파일과 리스팅 파일은 CSV 헤더도
> 중복 제거 키(`id_field`)도 다르다. §7.1 에 `COUPANG_BUSINESS_PROFILE` 과
> `COUPANG_LISTING_PROFILE` 을 각각 정의한다 — 하나의 프로필로 두 스키마를
> 쓰면 `_write_records` 가 한쪽 필드를 조용히 버린다(AD-2가 지적한 바로 그 사고).

### 6.4 상태 파일

| 파일 | 용도 |
|------|------|
| `coupang_collected_ids.json` | D1 — 처리 완료 `product_id` |
| `coupang_sellers.json` | D2 — `seller_key → SellerProfile` 캐시 (상세페이지 직접 확보 결과) |
| **`coupang_business_index.json`** | **D3 — 출력한 사업자번호 + `seller_key` 역인덱스 (AD-10.5)** |
| `coupang_budget.json` | 일일 예산 (AD-7) |

---

## 7. 저장 계층 변경 명세 (AD-2 구현)

### 7.1 신규 `app/core/schema.py`

```python
@dataclass(frozen=True)
class PlatformProfile:
    key: str                          # "gmarket" | "coupang"
    file_prefix: str                  # 출력/체크포인트 파일 접두사
    id_field: str                     # 체크포인트 검증 + 중복 제거 키 필드명
    record_fields: tuple[str, ...]    # CSV 헤더 순서
    collected_ids_filename: str
    state_filename: str
    promoted_manifest_filename: str
    lock_filename: str
    all_label: str = "ALL"

GMARKET_PROFILE = PlatformProfile(
    key="gmarket",
    file_prefix=config.FILE_PREFIX,                    # "gmarket_fast"
    id_field="goodscode",
    record_fields=tuple(RECORD_FIELDS),
    collected_ids_filename=config.COLLECTED_IDS_FILENAME,
    state_filename=config.STATE_FILENAME,
    promoted_manifest_filename=config.PROMOTED_MANIFEST_FILENAME,
    lock_filename=".gmarket_fast.lock",
)

COUPANG_BUSINESS_PROFILE = PlatformProfile(     # 주 산출물 — 사업자 단위
    key="coupang_business",
    file_prefix="coupang_business",
    id_field="business_number",                 # ★ D3 중복 제거 키
    record_fields=tuple(COUPANG_BUSINESS_FIELDS),
    collected_ids_filename="coupang_business_index.json",
    state_filename="coupang_state.json",
    promoted_manifest_filename=".coupang_business_promoted.json",
    lock_filename=".coupang.lock",
)

COUPANG_LISTING_PROFILE = PlatformProfile(      # 부록 — 상품 원본
    key="coupang_listing",
    file_prefix="coupang_listing",
    id_field="product_id",                      # D1 재수집 방지 키
    record_fields=tuple(COUPANG_PRODUCT_FIELDS),
    collected_ids_filename="coupang_collected_ids.json",
    state_filename="coupang_listing_state.json",
    promoted_manifest_filename=".coupang_listing_promoted.json",
    lock_filename=".coupang.lock",              # 같은 락 — 동일 파이프라인
)
```

> **왜 프로필이 2개인가.** 사업자 파일과 리스팅 파일은 CSV 헤더도 중복
> 제거 키도 다르다. 하나의 프로필로 두 스키마를 저장하면 `_write_records`
> 의 `extrasaction="ignore"` 가 한쪽 필드를 조용히 버린다 — AD-2가 지적한
> 바로 그 사고를 우리가 스스로 재현하게 된다.
>
> **락 파일은 공유한다.** 두 프로필은 같은 파이프라인의 산출물이므로 동시
> 실행될 일이 없고, 오히려 같은 출력 경로를 다른 프로세스가 건드리는 것을
> 함께 막아야 한다. `_held_locks` 키가 `(경로, lock_filename)` 이므로
> 같은 프로세스에서 두 Storage 를 만들어도 잠금은 한 번만 잡힌다.

> **순환 import 주의.** `schema.py` 가 `config` 를, `config` 가
> `models.records` 를 import 한다. `GMARKET_PROFILE` 정의를 `schema.py` 에
> 두면 `config → records`, `schema → config, records` 로 단방향이 유지된다.
> `storage.py` 는 `schema` 만 import 하도록 정리한다.

### 7.2 `storage.py` 변경 지점 (전부 기계적 치환)

| 현재 | 변경 후 |
|------|---------|
| `_LOCK_FILENAME = ".gmarket_fast.lock"` (모듈 상수, L27) | `self.profile.lock_filename` — `_acquire_output_lock(dir, filename)` 시그니처에 추가 |
| `_held_locks` 키 = 경로 | 키 = `(경로, lock_filename)` — 두 프로필이 같은 경로를 써도 각자 잠금 |
| `_PARTIAL_PREFIX` (모듈 상수, L102) | `self._partial_prefix` 인스턴스 속성 |
| `config.COLLECTED_IDS_FILENAME` (L127) | `self.profile.collected_ids_filename` |
| `config.STATE_FILENAME` (L131) | `self.profile.state_filename` |
| `config.PROMOTED_MANIFEST_FILENAME` (L346) | `self.profile.promoted_manifest_filename` |
| `item.get("goodscode")` (L273) | `item.get(self.profile.id_field)` |
| `RECORD_FIELDS` in `_write_records` (L487) | `self.profile.record_fields` |
| `config.FILE_PREFIX` (L359, L437) | `self.profile.file_prefix` |
| `find_leftover_partials` glob (L318) | `self._partial_prefix` 사용 |

`__init__` 시그니처: `def __init__(self, output_dir=None, profile=GMARKET_PROFILE)`
— 위치 인자 1개 호출(`Storage(tmpdir)`)이 전부라 기존 호출부 무영향.

### 7.3 `crawler.py` 변경 지점

| 위치 | 변경 |
|------|------|
| `_checkpoint_content_hash(records)` (L78) | `(records, id_field="goodscode")` — 기본값 덕분에 `tests/test_crawler.py:470` 등 기존 위치 인자 호출 그대로 통과 |
| `_extract_codes(records)` (L97) | `(records, id_field)` |
| `reconcile_leftover_checkpoints` (L240-241) | `storage.profile.id_field` 를 읽어 위 두 함수에 전달 — **시그니처 불변** → `main_window.py:288` 호출부 무영향 |
| `_run_plan` 의 `rec.get("goodscode")` (L700) | `rec.get(self.storage.profile.id_field)` |

`SellerCrawler` 자체(Gmarket Phase 2 HTTP 수집기)는 Coupang에서 재사용하지
않는다. 재사용하는 것은 **`Storage` 와 `reconcile_leftover_checkpoints`**
(체크포인트 승격 프로토콜)뿐이다.

### 7.4 회귀 방지 테스트 (INV-2 증명)

`tests/test_storage_profile.py` 신규:

- `GMARKET_PROFILE` 로 만든 `Storage` 가 생성하는 모든 파일명/CSV 헤더가
  변경 전 하드코딩 값과 문자열 단위로 동일한지 (골든 값 비교)
- 같은 `output_dir` 에 두 프로필 `Storage` 를 만들어도 상태 파일·체크포인트·
  manifest·락이 서로 침범하지 않는지
- `COUPANG_PROFILE` 체크포인트가 `product_id` 로 검증되고, `goodscode` 가
  없다는 이유로 격리되지 않는지

---

## 8. 모듈별 구현 명세

```
app/
├── core/
│   ├── schema.py                [NEW] PlatformProfile / GMARKET_/COUPANG_PROFILE
│   ├── base.py                  [MOD] Control.sleep() 추가 (AD-6)
│   ├── storage.py               [MOD] profile 파라미터화 (§7.2)
│   ├── crawler.py               [MOD] id_field 파라미터화 (§7.3)
│   └── coupang/                 [NEW] 패키지
│       ├── __init__.py
│       ├── config.py            설정 상수 + CoupangSettings 로더
│       ├── budget.py            일일 예산 영속화 (AD-7)
│       ├── block.py             BlockDetector / BlockedError
│       ├── behavior.py          page_action 콜백 + 지연 프로파일 (L4)
│       ├── proxy.py             프록시 공급자 (ProxyRotator 래퍼, AD-8)
│       ├── parser.py            리스팅 파서 + 판매자 상세 파서(XHR/DOM, 순수 함수)
│       ├── listing.py           Stage A 엔진
│       ├── sellers.py           SellerProfile 캐시 스토어 (D2, AD-1)
│       ├── dedup.py             D3 사업자번호 정규화·검증·병합 (AD-10)
│       ├── enrich.py            Stage C 엔진 — 판매자 상세 직접 스크래핑
│       └── pipeline.py          Stage 오케스트레이션 + Stage D 집계/출력
├── models/
│   └── coupang_records.py       [NEW] §6.1
├── workers/
│   ├── coupang_scan_worker.py   [NEW] Stage A
│   └── coupang_enrich_worker.py [NEW] Stage B+C
└── ui/
    ├── main_window.py           [MOD] QTabWidget 로 감싸기 (§10.1)
    └── coupang/                 [NEW]
        ├── panel.py             CoupangPanel (탭 본문)
        ├── settings_panel.py    프록시/예산 설정
        ├── stage_bar.py         Stage 진행 + 쿨다운 카운트다운
        └── seller_table.py      판매자/사업자 결과 테이블
```

> **rev.4 삭제**: `ftc_db.py`, `ftc_matcher.py`, `ftc_build_worker.py` —
> 공정위 연동이 없으므로 이 세 모듈은 존재할 이유가 없다.

### 8.1 주요 시그니처

```python
# core/coupang/parser.py — 네트워크 의존 없음 → 픽스처로 단위 테스트 가능
def parse_listing(html: str, source_label: str) -> list[CoupangProduct]: ...
def parse_seller_from_xhr(payloads: list[str]) -> SellerProfile | None: ...
def parse_seller_from_dom(html: str) -> SellerProfile | None: ...
def normalize_seller_key(store_name: str) -> str: ...
def parse_power_seller(node) -> tuple[bool, str]: ...   # (power_seller, seller_grade)

# core/coupang/dedup.py — AD-10 (전부 순수 함수 → 네트워크 없이 테스트 가능)
def normalize_brno(s: str) -> str: ...                  # 숫자 10자리 정규 키
def is_valid_brno(brno: str) -> bool: ...               # 국세청 체크섬
def dedupe_by_business_number(
    profiles: list[SellerProfile],
    products_by_seller: dict[str, list[CoupangProduct]],
) -> tuple[list[BusinessRecord], list[SellerProfile]]:  # (확정+병합, 미확정)
    ...

class BusinessIndex:                                    # 실행 간 영속 제거
    def known(self, business_number: str) -> bool: ...
    def seller_known(self, seller_key: str) -> bool: ...  # 고비용 진입 전 차단
    def add(self, record: BusinessRecord) -> None: ...
    def save(self) -> None: ...                         # 원자적 쓰기

# core/coupang/block.py
class BlockDetector:
    def check(self, response) -> bool: ...        # 403/418/429 / len<1000 / 키워드
    def should_abort(self) -> bool: ...           # block_count >= limit
    @property
    def reason(self) -> str: ...

# core/coupang/budget.py
class DailyBudget:
    def load(self) -> BudgetSnapshot: ...
    def count_listing(self, n: int = 1) -> None: ...
    def count_detail(self, n: int = 1) -> None: ...
    def mark_block(self) -> None: ...
    def listing_exhausted(self) -> bool: ...
    def detail_exhausted(self) -> bool: ...
    def recovery_wait_seconds(self) -> float: ...  # last_block_at 기준 잔여

# core/coupang/pipeline.py
class CoupangPipeline:
    def run_listing(self, plan: ListingPlan) -> ListingResult: ...      # Stage A
    def reduce_sellers(self, products: list[CoupangProduct]) -> list[SellerTarget]: ...  # Stage B
    def run_enrich(self, targets: list[SellerTarget]) -> EnrichSummary: ...  # Stage C
    def export(self, label: str) -> ExportResult: ...                  # Stage D
```

콜백은 기존 엔진 관례(`crawler.py:66-71`)를 그대로 따른다:
`on_log` / `on_stage` / `on_item` / `on_collected` / `on_error` +
Coupang 전용 `on_wait(remaining_seconds, reason)`.

---

## 9. 안티봇 / 예산 / 차단 복구 제어

### 9.1 타이밍 상수 (`core/coupang/config.py`)

`BYPASS_TECHNICAL_GUIDE.md` §5 + `IMPLEMENTATION_ARCHITECTURE.md` 값을 채택.

```python
WARMUP_WAIT       = (8.0, 15.0)     # _abck 센서(70KB) 실행 대기
PAGE_DELAY        = (8.0, 15.0)     # 리스팅 페이지 간
ITEM_DELAY        = (5.0, 15.0)     # 상세 상품 간
SESSION_BATCH_SIZE = 5              # 세션당 상세 건수
SESSION_COOLDOWN  = (600.0, 900.0)  # 배치 간
BLOCK_COOLDOWN    = (900.0, 1800.0) # 차단 감지 후 복구
ABORT_AFTER_BLOCKS = 2              # 세션/실행 중단 임계
MAX_DAILY_LISTING_FETCHES = 200     # 페이지 요청 상한
MAX_DAILY_DETAIL_FETCHES  = 500     # 상세 요청 상한 (합계 ≤ 1,000 정신)
FETCH_TIMEOUT_MS  = 30000
DETAIL_RETRY_LIMIT = 2              # 상세 파싱 실패 시 재시도 (대체 경로 없으므로 중요)
```

**Gmarket과의 대비** (같은 코드베이스에 두 정책이 공존한다는 점을 명확히):

| 항목 | Gmarket | Coupang | 배율 |
|------|---------|---------|------|
| 요청 간격 | 0.5초 + jitter 0.3 | 5~15초 | ~20x |
| 세션당 건수 | 제한 없음(HTTP) | 5건 | — |
| 세션 쿨다운 | 없음 | 600~900초 | — |
| 차단 허용 | 3회 | 2회 | 보수적 |
| 일일 목표 | ~2,000건 | ~500건 | 0.25x |

### 9.2 차단 감지 (`block.py`)

`PLATFORM_ANALYSIS.md` + `BYPASS_TECHNICAL_GUIDE.md` §10 통합:

```python
BLOCK_STATUS = {403, 418, 429}
SOFT_BLOCK_MIN_BYTES = 1000
BOT_KEYWORDS = ["자동화된 테스트 소프트웨어", "접근이 제한", "보안 절차",
                "확인 절차", "잠시 후 다시 시도"]
```

판정 순서: 상태코드 → 본문 길이(soft block) → 키워드(앞 5,000자만 검사).
`BlockDetector` 는 상태를 갖고(`block_count`, `reason`), 세션 단위로 생성한다.

**주의.** Gmarket의 `config.BOT_KEYWORDS`(`config.py:74`)와 별도 목록이다.
`app.utils.helpers.contains_bot_challenge` 를 재사용하지 않고 Coupang 전용
판정 함수를 둔다 — 키워드 집합이 다르고 soft-block 규칙이 추가되기 때문.

### 9.3 행동 시뮬레이션 (`behavior.py`, Akamai L4)

`SCRAPLING_TECH_ANALYSIS.md` §4.1 결론: Camoufox가 L1~L3을 C++ 레벨에서
처리하고, **L4(행동)와 L5(IP)는 우리 코드 몫**이다.

- `page_action(page)` 콜백: 점진적 스크롤(여러 단계, 랜덤 간격) → 판매자
  정보 섹션까지 도달 → 필요 시 클릭. Playwright/Camoufox `Page` API 사용.
- JS `scrollTo()` 직접 호출 지양 — 문서가 탐지 가능성을 지적한다.
- 자연 유입 체인: 홈 → 카테고리 → 상품. `google_search=False` 로 두고
  참조 체인은 실제 내비게이션으로 만든다.
- 지연은 균등난수가 아니라 로그노멀 유사 분포를 권장(사람 행동에 더 근접).
  1차 구현은 균등난수로 두고 스파이크 결과에 따라 조정한다.

#### 9.3a 웜업 상호작용 주입 (rev.5 신규) ★

**대기만으로는 부족하다.** Akamai 센서는 상호작용 이벤트를 수집하므로,
홈 fetch 직후 아래를 호출한다 (`BYPASS_TECHNICAL_GUIDE.md` §11 전체 구현):

```python
def warmup_interact(page, control) -> None:
    """센서 텔레메트리 생성 — 마우스/키보드/스크롤/클릭 주입."""
    page.mouse.move(randint(400, 800), randint(300, 500))   # C++ 궤적
    for _ in range(randint(2, 4)):
        page.keyboard.press("PageDown")                      # JS scrollTo 아님
        control.sleep(uniform(0.3, 1.2))
    page.keyboard.type("쿠팡", delay=randint(80, 200))       # 키스트로크 다이나믹스
    control.sleep(uniform(0.5, 1.5))
    page.keyboard.press("Escape")
    page.mouse.click(randint(100, 300), randint(600, 800))  # 비상품 영역 클릭
    control.sleep(uniform(1.0, 3.0))
    page.keyboard.press("Home")
```

적용 순서: `session.fetch(HOME_URL)` → **`warmup_interact()`** → `warmup_delay()`.

#### 9.3b 세션 간 랜덤화 (rev.5 신규)

세션 간 메타패턴 정규성을 깨뜨린다 (`BYPASS_TECHNICAL_GUIDE.md` §12):

| 항목 | 고정 (rev.4) | 랜덤화 (rev.5) |
|------|-------------|----------------|
| 배치 크기 | 항상 5 | `randint(3, 5)` — `AdaptiveBackoff.effective_batch_size` |
| 웜업 대기 | 균등(8, 15) | 로그노멀(중앙값 10초) |
| 정렬 옵션 | bestAsc | 4종 중 랜덤 |
| 세션 시작 전 | 홈 바로 fetch | 30% 확률로 무관 페이지 경유 (decoy) |
| 페이지 간 지연 base | (8, 15) 고정 | 세션마다 ±3초偏移 |

```python
class SessionRandomizer:
    def batch_size(self) -> int: ...        # 3~5
    def warmup_duration(self) -> float: ... # lognormvariate(log(10), 0.4)
    def maybe_decoy_visit(self, session, control) -> None: ...  # 30%
    def sorter(self) -> str: ...            # 4종 랜덤
```

### 9.4 프록시 (`proxy.py`, L5)

```python
class ProxyProvider:
    """설정에서 프록시 목록을 읽어 세션마다 하나씩 내준다.
    rev.5: canary 검증 + blacklist 추가."""
    def next(self) -> str | dict | None: ...
    def is_configured(self) -> bool: ...
    def rotator(self) -> "ProxyRotator": ...   # scrapling ProxyRotator 위임
    def blacklist(self, proxy: str) -> None: ...  # 차단 시 해당 IP 제외
```

- 형식: `http://user:pass@host:port` 또는 Playwright 스타일 dict.
- `PROXY_REQUIRED=True` 인데 미설정이면 `ProxyNotConfiguredError` 를 던져
  Stage A/C 진입을 막는다(AD-8).
- **자격증명은 로그에 절대 출력하지 않는다.** 로그에는 호스트:포트만
  마스킹해 표시(`kr-***.example:8000`). 로그 패널은 사용자가 스크린샷을
  공유할 수 있는 화면이다.

#### 9.4a Canary 검증 (rev.5 신규)

`next()` 호출 시 할당 전 경량 품질 검증을 수행한다
(`BYPASS_TECHNICAL_GUIDE.md` §13 전체 구현):

```python
def _canary(self, proxy: str) -> bool:
    """curl_cffi로 coupang.com 홈 status + body 크기만 확인.
    403/429 이거나 body < 5KB → 해당 IP는 이미 flagged."""
```

- 최대 3회 시도 후 전부 실패하면 `ProxyExhaustedError`.
- 차단 감지 시 `blacklist(proxy)` 로 현재 실행에서 영구 제외.
- **세션 내 IP 고정(sticky session)** 필수 — 공급자별 session ID 문법은
  스파이크 시 확인 (`BYPASS_TECHNICAL_GUIDE.md` §13).

### 9.5 적응적 백오프 (`AdaptiveBackoff`, rev.5 신규)

고정 쿨다운의 한계를 보완한다 (`BYPASS_TECHNICAL_GUIDE.md` §14):

```python
class AdaptiveBackoff:
    """차단 이력 기반 점진적 전략 변경."""
    def on_block(self, context: dict) -> None: ...
    @property
    def cooldown_multiplier(self) -> float: ...   # 1.0 → 2.0 → 3.0 → 4.0
    @property
    def effective_batch_size(self) -> int: ...    # 5 → 4 → 3 → 3
```

| 레벨 | 트리거 | 조치 |
|------|--------|------|
| 1 | 같은 실행 내 2회 차단 | 쿨다운 ×2, 배치 -1 |
| 2 | 3회 차단 | 쿨다운 ×3, 배치 -2, 시간대 분산 |
| 3 | 4회 차단 | 프록시 공급자 교체 권장 알림 |
| 4 | 5회+ | 24시간 전면 중단 (`budget.suspend_until`) |

`behavior.py` 의 `batch_cooldown()` 이 `cooldown_multiplier` 를 참조한다.

### 9.6 파서 건강 모니터링 (`ParserHealthMonitor`, rev.5 신규)

셀렉터/XHR 변경을 조기에 감지한다:

```python
class ParserHealthMonitor:
    """최근 N건의 파싱 성공률을 슬라이딩 윈도우로 추적."""
    def record(self, method: str, success: bool) -> None: ...
    @property
    def xhr_success_rate(self) -> float: ...
    def is_degraded(self) -> bool:
        """XHR 성공률 < 50% → 엔드포인트 변경 가능성. 사용자 알림."""
```

- `parse_method` 필드(§6.1)와 연동 — xhr/dom/none 비율을 실행마다 집계.
- `is_degraded()` 시 UI에 "셀렉터/XHR 변경 감지 — 스파이크 재실행 권장" 경고.
- 파이프라인을 중단시키지는 않되, 미확정 비율 급증 시 자동 경고 1회.

### 9.7 HTTP/2 + 핑거프린트 일관성 (rev.5 신규, 스파이크 검증)

코드 변경이 아니라 **P0-3 스파이크 검증 항목**이다
(`BYPASS_TECHNICAL_GUIDE.md` §15 전체 절차):

- mitmproxy로 Camoufox HTTP/2 프레임 캡처 → 실제 Firefox와 비교
- browserleaks.com에서 Canvas/WebGL/Screen/Timezone 일관성 확인
- httpbin.org/headers에서 헤더 순서 검증
- **실패 시**: `real_chrome=True` 전환 또는 BrowserForge 설정 조정

---

## 10. UI 통합 설계

### 10.1 `main_window.py` 최소 침습 리팩터

현재 `_build_ui()`(L65-121)가 central widget에 직접 위젯을 쌓는다.
테스트(`tests/test_main_window.py`)가 `win.settings`, `win.btn_prescan`,
`win.prescan_table`, `win.log` 등 **속성명에 직접 의존**하므로 속성명을
유지해야 한다.

```python
def _build_ui(self) -> None:
    self._tabs = QTabWidget()
    self._tabs.addTab(self._build_gmarket_tab(), "Gmarket")
    self._tabs.addTab(self._build_coupang_tab(), "Coupang OMP")
    self.setCentralWidget(self._tabs)
    self.statusBar().showMessage("준비됨")

def _build_gmarket_tab(self) -> QWidget:
    """기존 _build_ui 본문을 그대로 이동 — self.settings/self.btn_* 등
    속성 대입은 한 줄도 바꾸지 않는다(기존 테스트 무수정 통과)."""

def _build_coupang_tab(self) -> QWidget:
    from app.ui.coupang.panel import CoupangPanel     # 지연 import
    self.coupang = CoupangPanel()
    return self.coupang
```

**종료 처리 확장.** `_active_worker()`(L494)는 현재 Gmarket 워커 2개만
확인한다. Coupang 워커 2종을 포함하도록 확장하되, 목록을 하드코딩하지 말고
`CoupangPanel.active_worker()` 에 위임한다.
`closeEvent` 의 취소 요청도 Coupang `Control` 에 전파해야 한다 — 쿨다운
중이면 AD-6 덕분에 최대 `poll`(0.5초) 안에 반응한다.

**윈도 타이틀**: "판매자 수집기 v2.1 (Gmarket / Coupang)".

### 10.2 `CoupangPanel` 구성

```
┌─ 설정 ──────────────────────────────────────────────────────┐
│ 프록시   [●설정됨 kr-***:8000 / ○미설정]  [설정...]         │
│ 대상     ( )OMP 전체  (•)카테고리 [식품 ▾]  ( )검색어[____] │
│ 페이지   [1] ~ [5]     정렬 [bestAsc ▾]   listSize [120 ▾]  │
│ 필터     [ ] 파워셀러만 수집  (리스팅 배지 확보 시에만 활성) │
│ 일일 예산  리스팅 [200] / 상세 [500]   오늘 사용 12 / 0      │
│ 저장 경로  [/.../output          ] [찾아보기]                │
└──────────────────────────────────────────────────────────────┘
[Stage A: OMP 스캔] [Stage B: 판매자 축약] [Stage C: 상세 스크래핑]
[Stage D: 내보내기] [일시정지] [취소]

┌─ 진행 ──────────────────────────────────────────────────────┐
│ Stage A  ███████░░░░░░  3/5 페이지   상품 360건              │
│ 다음 요청까지  00:11   (페이지 간 지연)         ← AD-6 카운트│
│ 차단 0회 · 예산 리스팅 15/200 · 상세 0/500                   │
└──────────────────────────────────────────────────────────────┘
┌─ 사업자 결과 (중복 제거 후) ────────────────────────────────┐
│ # │사업자번호  │ 상호명    │대표자│★│스토어(수)  │상태     │
│ 1 │123-45-67890│(주)에이비씨│홍길동│★│ABC마켓 외1 │확보(xhr)│
│ 2 │     —      │ —        │ —   │ │굿딜        │미확보   │ ←회색
└──────────────────────────────────────────────────────────────┘
  요약: 사업자 128건(중복 제거 -37) / 미확정 24건 / 파워셀러 41건
  ★ = 파워셀러 · "스토어(수)" 클릭 시 병합된 스토어 전체 표시
[실시간 로그 패널 — 기존 LogPanel 재사용]
```

**색상 규칙** (`prescan_table.py:25` 관례 계승):
확보(`resolved=True`)=녹색, 미확보=회색. `parse_method` 는 툴팁으로
표시(xhr/dom 중 무엇으로 파싱됐는지 — 운영 모니터링용).

### 10.3 Stage 버튼 활성화 규칙

| Stage | 활성 조건 |
|-------|-----------|
| A OMP 스캔 | 프록시 설정됨(또는 명시적 우회) + 일일 예산 잔여 |
| B 판매자 축약 | 스캔 결과 존재 |
| C 상세 스크래핑 | 프록시 설정됨 + 축약된 판매자 존재 + 예산 잔여 |
| D 내보내기 | 스캔 결과 존재 |

실행 중에는 다른 Stage 버튼과 설정 위젯을 모두 비활성화
(`SettingsPanel.set_controls_enabled` 관례).

---

## 11. 설정 · 시크릿 관리

```
coupang_settings.json          (출력 디렉터리 하위, .gitignore 등록)
{
  "proxy": {"required": true, "urls": ["http://user:pass@host:port"]},
  "budget": {"listing": 200, "detail": 500},
  "respect_robots": true
}
```

- **환경변수 우선**: `COUPANG_PROXY_URLS` 가 있으면 파일 값을 덮어쓴다
  (CI/배포에서 파일에 비밀을 남기지 않기 위해).
- `.gitignore` 추가 필요: `coupang_settings.json`. (현재 `.gitignore` 는
  `output/` 만 무시하므로 설정 파일이 그대로 추적된다 — 시크릿 유출 경로.)
- 로그·예외 메시지에서 자격증명 마스킹 (§9.4).

**의존성**: 신규 추가 없음. `scrapling[fetchers]>=0.4.11` 만으로 충분하다
(공정위 연동 제거로 `thefuzz`/`python-Levenshtein` 불필요).
`pyinstaller.spec` 도 `hiddenimports` 추가 없이 그대로 사용한다.

---

## 12. 테스트 전략

기존 관례(`tests/__init__.py`): core 계층은 PyQt6/scrapling 없이 테스트한다.
Coupang도 동일하게 **네트워크 0, 브라우저 0** 으로 커버한다.

### 12.1 신규 테스트 파일

| 파일 | 대상 | 방식 |
|------|------|------|
| `test_storage_profile.py` | §7.4 무회귀 + 2프로필 격리 | 골든값 비교 |
| `test_coupang_parser.py` | 리스팅/상세/XHR 파싱 | `coupang_crawl/fixtures/*.html`, `*.json` |
| `test_coupang_block.py` | 403/418/429, soft block, 키워드, abort 임계 | 가짜 Response 객체 |
| `test_coupang_budget.py` | 일일 리셋, 영속화, 재시작 후 복구 대기 | tmpdir + 시간 주입 |
| `test_control_sleep.py` | 분할 대기: 취소 즉시 반응, 일시정지, on_tick | 짧은 시간(0.2초) |
| `test_coupang_dedup.py` | D3 전반 (AD-10) | 순수 함수 — 네트워크·픽스처 불필요 |
| `test_coupang_pipeline.py` | Stage 오케스트레이션, 판매자 캐시 재사용, 미확정 분리 출력 | 엔진 fetch 계층 monkeypatch |

**`test_coupang_dedup.py` 필수 케이스** (AD-10의 각 규칙에 1:1 대응)

| 케이스 | 기대 |
|--------|------|
| `123-45-67890` vs `1234567890` | 동일 키로 병합 |
| 체크섬 실패 번호 | 중복 제거 키로 사용 안 함, `resolved=False` 강등 |
| 9자리/11자리/문자 혼입 | `normalize_brno` 가 빈 문자열 반환 |
| 같은 사업자 · 스토어 3개 | 1행 + `store_count=3`, `store_names` 3개 결합 |
| 대표 선택: 필드 충족도 동률 | 수집 시각 오름차순 |
| `power_seller` false + true 혼재 | 병합 결과 **true** (OR) |
| `business_number` 빈 행 | 주 목록에서 제외, 미확정 목록으로 |
| `BusinessIndex` 에 이미 있는 번호 | 주 목록에서 제외 (재출력 안 함) |
| `seller_known()` 조회 | 상세 스크래핑 진입 전에 True 반환 |
| 인덱스 저장 중 실패 | 결과 파일은 남고 인덱스는 이전 상태 유지 (원자적 쓰기) |

**`test_coupang_parser.py` 파워셀러 케이스** (AD-11)

| 케이스 | 기대 |
|--------|------|
| 배지 있는 리스팅 픽스처 | `power_seller=True`, `seller_grade="파워셀러"` |
| 배지 없는 픽스처 | `power_seller=False`, `seller_grade=""`, 예외 없음 |
| 배지 문구 변형 (`우수판매자` 등) | 원문이 `seller_grade` 에 보존됨 |

### 12.2 픽스처 확보 (스파이크 산출물)

`coupang_crawl/fixtures/` 에 저장:
`omp_listing_page1.html`, `product_detail.html`, `seller_info_xhr.json`,
`blocked_403.html`, `soft_block_200.html`.

**픽스처에서 개인정보를 제거한다** — 실제 사업자 정보는 테스트에 커밋하지
않고 값을 치환한 익명 버전만 저장한다.

### 12.3 수동 검증 (자동화 불가 영역)

| 항목 | 방법 | 성공 기준 |
|------|------|----------|
| Akamai 우회 | 프록시 + StealthySession으로 OMP 1페이지 | 200 + 상품 ≥ 100건 |
| 세션 지속성 | 5페이지 연속 스캔 | 차단 0회 |
| 상세 XHR 캡처 | 상품 3건 | 사업자정보 필드 ≥ 3개 확보 |
| 24시간 안정성 | Stage C 장시간 실행 | 차단률 < 5%, 예산 캡 준수 |
| 취소 반응성 | 쿨다운 중 [취소] | 1초 내 UI 복귀 |

---

## 13. 구현 우선순위와 단계별 산출물

### P0 — 기반 + 실현 가능성 검증 (다른 모든 작업의 전제)

| # | 작업 | 산출물 | 선행 |
|---|------|--------|------|
| P0-1 | `Control.sleep()` 추가 (AD-6) | `base.py` + `test_control_sleep.py` | — |
| P0-2 | `PlatformProfile` 도입 + Storage/Crawler 파라미터화 (AD-2, §7) | `schema.py`, `storage.py`, `crawler.py`, `test_storage_profile.py`, **기존 테스트 전부 통과** | — |
| P0-3 | **셀렉터/XHR 발견 스파이크** ★★ (**파워셀러 배지 위치 포함** — AD-11) | `fixtures/*`, `PAGE_STRUCTURE.md` §2/§5 갱신, 셀렉터 확정표 | 프록시 |
| P0-4 | **우회 메커니즘 검증** (rev.5 신규) | 아래 4건 실측 리포트 | P0-3 + 프록시 |

**P0-4 세부 항목** (`BYPASS_TECHNICAL_GUIDE.md` §11~§15):

| # | 검증 | 성공 기준 |
|---|------|----------|
| 4a | 웜업 상호작용 유무에 따른 `_abck` valid 전환 비교 | 상호작용 있을 때만 valid 전환 |
| 4b | HTTP/2 프레임 + 헤더 순서 (mitmproxy 캡처) | 실제 Firefox와 SETTINGS/HEADERS 순서 일치 |
| 4c | 핑거프린트 일관성 (browserleaks.com) | WebGL=데스크톱 GPU, TZ=Asia/Seoul, Screen=1920×1080+ |
| 4d | 프록시 canary 유효성 | flagged IP 할당 시 canary가 403 반환 → blacklist 동작 |

> **P0-3 실패 시 계획 재검토.** rev.4: 공정위 연동을 제거했으므로 실패
> 시 대체 경로가 **없다**. 유료 스크래핑 API 도입 또는 프로젝트 범위
> 재검토(§16-Q1)만 남는다. 이 스파이크가 P0의 사실상 유일한 리스크 게이트다.
>
> **P0-4 실패 시** (rev.5): 4b/4c 불일치 → `real_chrome=True` 전환 검토.
> 4a 실패(상호작용 있어도 valid 전환 안 됨) → `page_action` 패턴 재설계
> 또는 유료 API 어댑터 검토.

### P1 — 수집 파이프라인

| # | 작업 | 산출물 |
|---|------|--------|
| P1-1 | `config.py` / `budget.py` / `block.py` / `proxy.py` | 제어 계층 + 테스트 3종 |
| P1-2 | `parser.py` (픽스처 기반) | 파서 + `test_coupang_parser.py` |
| P1-3 | `listing.py` (Stage A) + `coupang_scan_worker.py` | 리스팅 스캔 동작 |
| P1-4 | `sellers.py` + Stage B(축약) 통합 | 판매자 캐시, 인덱스 스킵 로직 |
| P1-5 | `enrich.py` (Stage C) + `coupang_enrich_worker.py` | 상세 직접 스크래핑 동작 (재시도 포함) |
| P1-6 | `dedup.py` — D3 사업자번호 제거 (AD-10) | 정규화·체크섬·대표선택·병합 + `BusinessIndex` + `test_coupang_dedup.py` |
| P1-7 | `pipeline.py` Stage D 집계/출력 | `coupang_business_*` / `coupang_unresolved_*` |

> **P1-6 은 P1-5 와 독립이다.** `dedup.py` 는 순수 함수 + 로컬 파일만
> 다루므로 프록시·네트워크 없이 완성하고 테스트할 수 있다. 프록시 결정이
> 늦어져도 이 작업은 선행 가능하다.

### P2 — UI

| # | 작업 | 산출물 |
|---|------|--------|
| P2-1 | `main_window.py` 탭 리팩터 (§10.1) | 기존 테스트 무수정 통과 |
| P2-2 | `CoupangPanel` + 설정 패널 | 설정 입출력 |
| P2-3 | `stage_bar.py` (쿨다운 카운트다운 포함) | 진행 표시 |
| P2-4 | `seller_table.py` (확보/미확보 상태 표시) | 결과 미리보기 |
| P2-5 | 종료/취소 전파 확장 | 좀비 스레드 0 |

### P3 — 운영 · 배포

| # | 작업 | 산출물 |
|---|------|--------|
| P3-1 | 24시간 안정성 검증 | 차단률/처리량 실측 리포트 |
| P3-2 | `pyinstaller.spec` 확인 (변경 불필요 예상) + Windows 빌드 검증 | 실행 파일 |
| P3-3 | `README.md` Coupang 절 추가, 운영 가이드 | 문서 |

---

## 14. 검증 기준 (Acceptance Criteria)

**무회귀 (최우선)**
- [ ] 기존 테스트 6개 파일 전부 무수정 통과
- [ ] Gmarket 탭 전체 플로우(사전조사 → 수집 → 저장)가 변경 전과 동일 동작
- [ ] Gmarket 출력 파일명 · CSV 헤더 · `collected_ids.json` 포맷 불변

**Stage A (OMP 스캔)**
- [ ] 프록시 미설정 시 실행이 거부되고 명확히 안내됨 (AD-8)
- [ ] 1페이지에서 `listSize` 만큼 상품 추출, product_id/store_name 채워짐
- [ ] 403/418/429/soft block 감지 → 2회 시 중단 + 복구 쿨다운
- [ ] 일일 예산 초과 시 중단, 앱 재시작 후에도 카운터 유지 (AD-7)

**Stage B (판매자 축약)**
- [ ] 같은 seller_key 상품이 1개 대상으로만 축약됨
- [ ] `BusinessIndex`/캐시에 이미 있는 판매자는 대상에서 제외됨

**Stage C (판매자 상세 직접 스크래핑)**
- [ ] Stage B 로 축약된 **모든** 판매자가 방문 대상에 포함됨 (일부만 대상이 아님)
- [ ] XHR 캡처 우선, 실패 시 DOM 파싱 폴백 동작
- [ ] 파싱 실패 시 `DETAIL_RETRY_LIMIT` 만큼 재시도 후 미확정으로 남음
- [ ] 판매자 단위로만 상세 fetch (같은 판매자 상품 2건 → fetch 1회) — AD-1
- [ ] 배치 5건 / 쿨다운 600~900초 / 세션마다 프록시 교체
- [ ] 쿨다운 중 [취소] 시 1초 내 반응, 창 종료 시 좀비 스레드 없음 (AD-6)
- [ ] 건 단위로 판매자 캐시가 durable 저장되어 크래시 시 재수집이 최소화됨
- [ ] 체크섬 실패 사업자번호는 `resolved=False` 로 강등됨

**동일 사업자번호 제거 (AD-10)**
- [ ] `123-45-67890` 와 `1234567890` 이 **같은 사업자로 병합**됨
- [ ] 국세청 체크섬 실패 번호는 중복 제거 키로 쓰이지 않음
- [ ] 스토어명이 다른 같은 사업자가 **1행으로 병합**되고
      `store_names`/`store_count`/`product_count` 가 정확히 집계됨
- [ ] `power_seller` 가 **OR** 로 병합됨 (스토어 중 하나라도 true → true)
- [ ] 사업자번호 미확정 행은 `coupang_unresolved_*` 로 **분리** 출력되고
      주 산출물에 섞이지 않음
- [ ] 재실행 시 이미 출력한 사업자번호는 **상세 수집 이전에** 스킵됨
      (프록시 요청이 발생하지 않음을 카운터로 검증)
- [ ] 주 산출물 CSV에 `business_number` 중복 행이 **0건**

**파워셀러 (AD-11)**
- [ ] 배지 발견 시 `power_seller` + `seller_grade`(원문)가 함께 기록됨
- [ ] 배지 미발견 시 빈 값으로 저장되고 **수집이 중단되지 않음**, 경고는 1회만
- [ ] 리스팅에서 확보 가능한 경우 상세 요청이 추가로 발생하지 않음

**Stage D (출력)**
- [ ] JSON + CSV 생성, 헤더가 `COUPANG_BUSINESS_FIELDS` 순서와 일치
- [ ] Gmarket 호환 필드가 동일 순서로 선두에 위치
- [ ] 동일 `business_number` 레코드 1건만 유지 (빈 값은 중복 제거 대상 아님 → 분리 출력)
- [ ] 리스팅 원본이 `COUPANG_LISTING_PROFILE` 로 별도 저장되고 필드 소실 없음
- [ ] CSV 수식 주입 이스케이프 적용 (`_csv_safe` 재사용)
- [ ] `email` 은 항상 빈 문자열, 이유가 README에 문서화됨

**보안/운영**
- [ ] 로그·파일 어디에도 프록시 자격증명 평문 노출 없음
- [ ] `coupang_settings.json` 이 `.gitignore` 에 포함됨

---

## 15. 리스크 레지스터

| ID | 리스크 | 영향 | 확률 | 완화 |
|----|--------|------|------|------|
| R1 | 상세 페이지 셀렉터/XHR 미발견 | **파이프라인 전체 불가** | 중 | P0-3 스파이크를 **선행 게이트**로. **rev.4: 실패 시 대체 경로 없음** — §16-Q1 (유료 API/보류) |
| R2 | 프록시 미확보 | **파이프라인 전체 불가** | 중 | §16-Q2에서 조기 결정. **rev.4: 대체 무료 경로 없음** |
| R4 | Akamai 정책 강화로 우회 실패 | 수집 중단 | 중 | 차단 감지·복구 내장, 유료 API 어댑터 자리 확보 |
| R5 | Storage 파라미터화가 기존 내구성 로직 훼손 | **데이터 유실** | 낮 | 기계적 치환만 허용, 기존 테스트 무수정 + 골든값 테스트 추가 |
| R6 | 쿠팡 셀렉터 변경 | 파싱 실패 | 중 | `adaptive=True`, XHR 우선, 픽스처 기반 회귀 테스트 |
| R7 | 프록시 비용 (월 10~30만원) | 운영 비용 | 확정 | 판매자 단위 축약(D2)으로 요청 수를 2.5배 절감 (25시간→10시간, `COLLECTION_STRATEGY.md` 참조) |
| R8 | robots.txt / ToS | 법적·계정 리스크 | 중 | `RESPECT_ROBOTS` 기본 활성, 허용 경로 우선, §16-Q4 사용자 결정 |
| R11 | DOM 오인식으로 잘못된 사업자번호 추출 | 데이터 오염 | 중 | 정규화 + 국세청 체크섬 검증 (AD-10.1) — 실패 시 강등, 병합하지 않음 |
| R12 | 파워셀러가 스토어 페이지에서만 확보 가능 | Stage C 처리량 1/2 | 중 | P0-3에서 조기 판정. 그 경우 §16-Q6 재협의 (수집 포기 / 비용 수용) |
| R13 | 상세 파싱 실패로 미확정 목록이 계속 쌓임 | 커버리지 저하 | 중 | 재시도 배치(같은 상세 스크래핑을 나중에 재시도) — 외부 DB 없이 자체 재시도만 가능 |
| **R14** | **웜업 상호작용 부족 → 센서 텔레메트리 공백 (rev.5)** | **L2/L4 탐지율 상승** | **중** | `warmup_interact()` 주입 (§9.3a). P0-4a에서 유무 비교 검증 |
| **R15** | **HTTP/2 프레임/헤더 순서 불일치 (rev.5)** | **L1 탐지 — 전 요청 차단** | **낮~중** | P0-4b mitmproxy 검증. 불일치 시 `real_chrome=True` (§9.7) |
| **R16** | **세션 간 메타패턴 상관 분석 (rev.5)** | **배치 단위 탐지 → IP 평판 하락** | **중** | `SessionRandomizer` (§9.3b) — 배치·정렬·타이밍·decoy 랜덤화 |
| **R17** | **프록시 풀 오염 — flagged IP 할당 (rev.5)** | **세션 즉시 차단, 풀 평판 연쇄 하락** | **중** | canary 검증 + blacklist (§9.4a). 공급자 다변화 |
| **R18** | **셀렉터/XHR 변경 미감지 (rev.5)** | **미확정 급증까지 인지 불가** | **중** | `ParserHealthMonitor` (§9.6) — XHR 성공률 < 50% 시 자동 경고 |

> **rev.4 제거된 리스크**: "FTC 매칭률이 기대 이하", "잘못된 매칭이
> 출력에 섞임"(퍼지 매칭 자체가 없음), "FTS5 미지원 SQLite 빌드" —
> 매칭 알고리즘과 SQLite 의존성이 전부 제거되어 해당 없음.

---

## 16. 착수 전 확인이 필요한 결정 사항

아래는 계획을 실행에 옮기기 전 **사용자 결정이 필요한 항목**이다.
결정 전까지 P0-1, P0-2(기반 리팩터)는 선행 가능하다 — 프록시도 Coupang
접근도 필요 없다. **rev.4: 그 외 모든 작업(P0-3 이후)은 프록시 확보와
스파이크 성공에 의존한다** — 공정위 같은 우회 경로가 더 이상 없다.

**Q1. 스파이크(P0-3)가 실패하면 어디까지 축소할 것인가?** (rev.4 갱신)
   공정위 폴백이 제거됐으므로 선택지가 줄었다.
   (a) 유료 스크래핑 API(Scrapfly 등) 도입 검토
   (b) 프로젝트 보류

**Q2. 프록시 예산을 집행할 것인가?** 월 10~30만원. **rev.4: 미집행 시
   파이프라인 전체가 동작하지 않는다** (부분 가치를 제공하는 무료 경로 없음).
   집행한다면 공급자(Bright Data / Oxylabs / Smartproxy / IPRoyal) 선택 필요.

**Q3. 수집 규모 목표는?** 일 500건인지, 총 5,000건 1회성인지에 따라 예산
   상수와 Stage C 소요 시간이 달라진다.

**Q4. robots.txt 미허용 경로(`/np/omp`) 직접 스캔을 허용할 것인가?**
   기본값은 허용 경로(`/np/categories/`, `/np/search`) 우선으로 설계했다.
   OMP 직접 스캔은 사용자가 명시적으로 켜야 동작한다.

**Q5. 사업자번호 중복 제거를 Gmarket 수집분까지 확장할 것인가?**
   Gmarket 레코드에도 `business_number` 가 있으므로 **플랫폼을 가로지르는
   중복 제거**가 가능하다.
   (a) **플랫폼별 독립** (기본값) — 각 산출물의 완결성 유지
   (b) 공유 인덱스 — 이미 Gmarket에서 확보한 사업자는 Coupang에서 스킵
   → (b)를 택하면 프록시 요청이 더 줄지만, Coupang 산출물만 따로 보면
   "빠진 사업자"가 생긴다. 두 목록의 용도에 따라 갈린다.

**Q6. 파워셀러가 판매자 스토어 페이지에서만 확보된다면?**
   P0-3 스파이크 결과에 달렸다. 스토어 페이지까지 가야 하면 판매자당 요청이
   1회 늘어 **Stage C 처리량이 약 절반**이 된다.
   (a) 파워셀러 수집 포기 (처리량 유지)
   (b) 비용 수용 (처리량 절반)
   → 리스팅 배지나 상세/XHR에서 확보되면 이 질문 자체가 사라진다
   (rev.4: Stage C가 전 판매자를 방문하므로 상세/XHR 경로의 커버리지
   제약이 없다).

---

## 부록 A. 문서 → 구현 매핑

| 연구 문서 | 반영 위치 |
|-----------|-----------|
| `PLATFORM_ANALYSIS.md` (Akamai 5계층, 차단 지표) | §9.2 `block.py`, AD-8 |
| `PAGE_STRUCTURE.md` rev.4 (URL/셀렉터/페이지네이션, §3.2 사업자정보 유일 출처) | §8 `parser.py`, §4.2 스파이크, §10.2 대상 설정 |
| `COLLECTION_STRATEGY.md` rev.4 (4단계 파이프라인, 공정위 제거) | §5 파이프라인 |
| `BYPASS_TECHNICAL_GUIDE.md` (타이밍/행동/프록시) | §9.1 상수, §9.3 `behavior.py`, §9.4 `proxy.py` |
| `DATA_FIELDS_MAPPING.md` rev.4 (필드/중복제거/파워셀러, 공정위 제거) | §6 스키마, **AD-10 / AD-11** |
| `IMPLEMENTATION_ARCHITECTURE.md` rev.4 (모듈 구조/의사코드, 공정위 제거) | §8 모듈 명세 (AD-1/AD-10으로 파이프라인 재설계) |
| `SCRAPLING_TECH_ANALYSIS.md` rev.4 (Scrapling API, 공정위 예제 제거) | §4.1 정정 반영 후 §5 의사코드 |

## 부록 B. 개정 이력 요약

| 항목 | rev.1 | rev.2/3 | **rev.4 (최종)** |
|------|-------|---------|--------------------|
| 사업자정보 확보 | 상품 단위 상세 방문 | 공정위 DB 매칭(1차) 또는 매칭+보강 | **Coupang 상세페이지 직접 스크래핑, 유일 경로** |
| 상세 수집 대상 | 상품 전체 | 공정위 미매칭 판매자만 | **축약된(D2) 전체 판매자** |
| 외부 의존성 | 없음 | SQLite+FTS5, thefuzz, python-Levenshtein | **없음** |
| 출력 단위 | 상품 1행 | 사업자 1행 + 상품 원본 별도 | 동일 (유지) |
| 중복 제거 | product_id | + seller_key + business_number | 동일 (유지) |
| 오매칭 리스크 | — | 동명 사업자 퍼지 매칭 모호성 | **없음 — 파싱 실패/차단만 리스크** |
| Stage 수 | — | 5~6개 (공정위 적재/매칭/보강 포함) | **4개 (A~D)** |
| 신규 필드 (rev.2) | — | `power_seller`, `seller_grade`, `store_names`, `store_count`, `product_count`, `sample_*` | 유지, `domain`/`business_status`/`handled_products` 는 제거 |
| 프록시 의존도 | 부분 | 일부 Stage는 무관 | **전체 — 프록시 없이는 파이프라인 성립 불가** |

## 부록 C. 기존 코드 참조 인덱스

| 참조 | 위치 | 왜 중요한가 |
|------|------|-------------|
| 내구성 커밋 순서 | `app/core/crawler.py:451-468` | 결과 저장 → ID 커밋 순서. Coupang `sellers.py` 도 동일 원칙 |
| 체크포인트 승격 fail-closed | `app/core/crawler.py:155-297` | Coupang Storage도 그대로 계승 |
| 원자적 쓰기 / 손상 격리 | `app/core/storage.py:461-512`, `537-603` | `sellers.py`, `dedup.py`, `budget.py` 저장에 동일 적용 |
| 취소 반응 분할 대기 | `app/core/prescan.py:145-149` | AD-6 `Control.sleep()` 의 원형 |
| 지연 import 관례 | `app/core/prescan.py:66` | INV-4 |
| 콜백 타입 관례 | `app/core/crawler.py:66-71` | Coupang 엔진 콜백 시그니처 |
| 워커 예외 경계 | `app/workers/crawl_worker.py:75-80` | Coupang 워커 2종 동일 패턴 |
| 종료 시 워커 대기 | `app/ui/main_window.py:512-565` | §10.1 확장 지점 |
| 상태 색상 관례 | `app/ui/widgets/prescan_table.py:25-31` | §10.2 확보/미확보 색상 |
