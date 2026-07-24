# 작업지시서: Gmarket 판매자 수집 시스템 v2.0 (Fast Crawl)

> 작성일: 2026-07-24
> 참조 문서: TECH_SPEC.md
> 대상: Windows Python UI 프로그램

---

## 1. 작업 개요

Gmarket 모바일 글로벌 서브도메인(`mg.gmarket.co.kr`)을 이용하여 22개 카테고리(베스트 10 + 슈퍼딜 12)의 판매자 사업자정보를 수집하는 Windows UI 프로그램을 구현한다.

### 핵심 변경사항 (기존 대비)

**프로그램 시작 시 "사전 조사(Pre-scan)" 단계를 반드시 수행한다.**

- 수집 시작 전, 선택된 모든 카테고리의 리스팅 페이지에 접근하여 **카테고리별 상품 수(goodscode 수)를 먼저 조사**
- 조사 결과를 UI에 표시 (카테고리명 / 상품 수 / 신규 대상 수)
- 사용자 확인 후 전체 데이터 수집 진행

---

## 2. 전체 수집 플로우 (3단계)

```
┌─────────────────────────────────────────────────────────────────┐
│ Phase 0: 사전 조사 (Pre-scan)                                    │
│   - 선택된 카테고리 각각의 리스팅 페이지 접근                    │
│   - goodscode 목록 추출 및 개수 카운트                           │
│   - collected_ids.json과 대조하여 신규 건수 산출                  │
│   - UI에 조사 결과 테이블 표시                                    │
│   - 예상 소요 시간 계산 및 표시                                   │
│   - 사용자 [수집 시작] 확인 대기                                  │
└────────────────────────────┬────────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│ Phase 1: 리스팅 추출 (카테고리당 1회)                            │
│   - StealthySession으로 리스팅 페이지 fetch                      │
│   - HTML에서 goodscode regex 추출                                │
│   - ※ Phase 0에서 이미 추출한 경우 재사용 (중복 접근 방지)       │
└────────────────────────────┬────────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│ Phase 2: 판매자정보 수집 (건별 HTTP GET)                         │
│   - mg.gmarket.co.kr/SellerInfo/SellerInfo?goodscode={code}     │
│   - requests + BeautifulSoup 파싱                                │
│   - 50건마다 중간 저장                                           │
│   - 요청 간 딜레이 (0.5초 + 랜덤 0~0.3초)                       │
└────────────────────────────┬────────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│ Phase 3: 저장 및 완료                                            │
│   - JSON + CSV (utf-8-sig) 저장                                  │
│   - collected_ids.json / fastcrawl_state.json 업데이트           │
│   - 최종 결과 요약 표시                                          │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Phase 0: 사전 조사 (Pre-scan) 상세

### 3.1 목적

수집 실행 전, 각 카테고리에 상품이 몇 개 있는지 미리 파악하여:
1. 사용자에게 전체 작업 규모를 사전 고지
2. "상품 없음" 카테고리를 사전 제외
3. 예상 소요 시간을 정확히 산출
4. 불필요한 Phase 2 접근을 방지

### 3.2 동작 순서

| 순서 | 동작 | 비고 |
|------|------|------|
| 1 | StealthySession 시작 | 헤드리스 Chromium 1개 인스턴스 |
| 2 | gmarket.co.kr warmup (Cloudflare 쿠키 획득) | 3초 대기 |
| 3 | 선택된 카테고리 순회하며 리스팅 fetch | 카테고리당 ~5초 |
| 4 | 각 카테고리 HTML에서 `goodscode=(\d+)` regex 추출 | 순서 유지 중복 제거 |
| 5 | collected_ids.json 로드 → 이미 수집한 ID 제외 | 신규 건수 산출 |
| 6 | 세션 종료 | |
| 7 | 조사 결과 UI 테이블에 표시 | |
| 8 | 예상 소요 시간 계산 | (총 신규 건수 × 0.7초) 기준 |
| 9 | 사용자 확인 대기 ([수집 시작] 버튼 활성화) | |

### 3.3 조사 결과 UI 표시 (예시)

```
┌─ 사전 조사 결과 ─────────────────────────────────────────────────┐
│                                                                   │
│  #  카테고리       전체 상품   신규 대상   이미수집   상태        │
│  ─────────────────────────────────────────────────────────────── │
│  1  신선식품         187건       42건      145건     수집 가능    │
│  2  가공식품         195건       63건      132건     수집 가능    │
│  3  생필품/육아      178건        0건      178건     완료됨       │
│  4  생활/주방        201건      201건        0건     수집 가능    │
│  ...                                                              │
│  22 디지털           164건       89건       75건     수집 가능    │
│  ─────────────────────────────────────────────────────────────── │
│  합계              3,847건      1,205건    2,642건               │
│                                                                   │
│  예상 소요 시간: 약 14분 3초 (0.7초/건 기준)                     │
│                                                                   │
│  [수집 시작]  [취소]                                              │
└───────────────────────────────────────────────────────────────────┘
```

### 3.4 Pre-scan 결과 데이터 구조

```python
@dataclass
class PrescanResult:
    category_name: str        # 카테고리명
    source: str               # "best" | "superdeal"
    total_codes: int          # 리스팅에서 추출된 전체 goodscode 수
    new_codes: int            # collected_ids 제외 후 신규 건수
    already_collected: int    # 이미 수집한 건수
    codes: list[str]          # 추출된 goodscode 목록 (Phase 1 재사용)
    status: str               # "collectable" | "completed" | "empty"
```

### 3.5 Pre-scan 최적화

- Phase 0에서 추출한 goodscode 목록을 **메모리에 캐싱**하여 Phase 1에서 재사용
- 동일 카테고리 리스팅을 2번 fetch하지 않음
- StealthySession은 Pre-scan에서 1회만 열고 닫음 (전체 카테고리 순회 후 종료)

---

## 4. Phase 1: 리스팅 추출

### 4.1 엔드포인트

| 구분 | URL 패턴 |
|------|----------|
| 베스트 | `https://www.gmarket.co.kr/n/best?groupCode={groupCode}` |
| 슈퍼딜 | `https://www.gmarket.co.kr/n/superdeal?categoryCode={categoryCode}` |

### 4.2 카테고리 목록

**베스트 (10개)**

| # | 카테고리명 | groupCode |
|---|-----------|-----------|
| 1 | 신선식품 | 100000006 |
| 2 | 가공식품 | 100000005 |
| 3 | 생필품/육아 | 100000007 |
| 4 | 생활/주방 | 100001001 |
| 5 | 패션/잡화 | 100000001 |
| 6 | 뷰티 | 100000003 |
| 7 | 디지털/가전 | 100001007 |
| 8 | 가구/홈 | 100001004 |
| 9 | 스포츠/건강 | 100001002 |
| 10 | 취미/문구/펫 | 100001003 |

**슈퍼딜 (12개)**

| # | 카테고리명 | categoryCode |
|---|-----------|-------------|
| 1 | 추천 | (파라미터 없음) |
| 2 | 브랜드패션 | 400000135 |
| 3 | 트랜드패션 | 400000136 |
| 4 | 뷰티/잡화 | 400000137 |
| 5 | 유아동 | 400000138 |
| 6 | 식품 | 400000139 |
| 7 | 생필품 | 400000140 |
| 8 | 가구/침구 | 400000141 |
| 9 | 생활/건강 | 400000143 |
| 10 | 스포츠/레저 | 400000146 |
| 11 | 가전/컴퓨터 | 400000148 |
| 12 | 디지털 | 400000149 |

### 4.3 접근 조건

- Cloudflare Managed Challenge 보호下 → Scrapling StealthySession 필수
- 베스트: `wait_selector='a[href*="goodscode"]'`, timeout 15초
- 슈퍼딜: 기본 timeout 20초
- goodscode 추출: `re.findall(r'goodscode=(\d+)', html)` → `dict.fromkeys()` 중복 제거
- 카테고리당 약 150~200개 추출

### 4.4 Pre-scan 캐시 재사용

```python
# Phase 0에서 이미 추출한 경우 Phase 1 스킵
if prescan_cache.get(category_name):
    codes = prescan_cache[category_name].codes
else:
    codes = fetch_listing_codes(cat)
```

---

## 5. Phase 2: 판매자정보 수집

### 5.1 엔드포인트

```
GET https://mg.gmarket.co.kr/SellerInfo/SellerInfo?goodscode={goodscode}
```

- 인증 불필요, Cloudflare 없음
- 응답: HTML (~18-21KB), 0.1~0.2초
- 레이트 리밋 미감지 (0.5초 간격 기준)
- 유효하지 않은 goodscode → 302 리다이렉트

### 5.2 필수 요청 헤더

```python
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}
```

### 5.3 파싱 규칙 (CSS 셀렉터)

| 데이터 | 셀렉터 | 매핑 필드 |
|--------|--------|-----------|
| 스토어명 | `h3.store-title` | store_name |
| Seller | `li.list-row` (label="Seller") | company_name |
| Representative | `li.list-row` (label="Representative") | phone |
| E-mail | `li.list-row` (label="E-mail") | email |
| Business Registration No. | `li.list-row` (label="Business Registration No.") | business_number |
| Address | `li.list-row` (label="Address") | address |

### 5.4 수집 레코드 스키마

```json
{
  "goodscode": "4805535812",
  "url": "https://item.gmarket.co.kr/Item?goodscode=4805535812",
  "store_name": "Nong_shim official store",
  "company_name": "에스엠맨테크",
  "ceo_name": "",
  "email": "smmentech@smmentech.co.kr",
  "phone": "(+82) 02-6952-5311",
  "business_number": "1978601078",
  "address": "서울시 광진구 구의강변로30, 목림빌딩 202호",
  "collected_at": "2026-07-24T11:46:58.852811",
  "source": "가공식품"
}
```

### 5.5 성공 판정

- store_name, company_name, email, phone, business_number 중 1개 이상 비어있지 않으면 성공
- 전부 비어있으면 None 반환 (MISS 처리)

### 5.6 수집 제어

| 항목 | 값 |
|------|-----|
| 요청 간격 | 0.5초 + random(0~0.3초) |
| 중간 저장 | 50건마다 collected_ids.json 저장 |
| 동시 요청 | 1개 (병렬 금지) |
| 재시도 | 네트워크 오류 시 3회 |

---

## 6. 에러 처리

| 상황 | 처리 | UI 표시 |
|------|------|---------|
| HTTP 302 (유효하지 않은 goodscode) | 해당 건 스킵 | "건너뜀" |
| HTTP 500/타임아웃 | 해당 건 스킵, 계속 진행 | "실패" (빨간색) |
| 네트워크 오류 | 3회 재시도 후 스킵 | "재시도 중..." |
| Phase 0/1 리스팅 403 | Cloudflare 차단, 30초 대기 후 재시도 | "차단 감지, 대기 중..." |
| Phase 0/1 리스팅 0건 | 해당 카테고리 스킵 | "상품 없음" |
| Pre-scan 전체 0건 | 수집 불가 알림, 종료 | "수집 가능한 상품 없음" |

---

## 7. UI 프로그램 설계

### 7.1 기술 스택

| 항목 | 선택 |
|------|------|
| UI 프레임워크 | PyQt6 / PySide6 |
| 스레딩 | QThread + Signal/Slot |
| 패키징 | PyInstaller |
| Python | 3.10+ (3.12 권장) |

### 7.2 스레드 구조

```
[Main Thread - UI]
    │
    ├── QThread (PrescanWorker)
    │     ├── Phase 0: StealthySession → 전체 카테고리 goodscode 조사
    │     └── Signal: prescan_complete(list[PrescanResult])
    │
    ├── QThread (CrawlWorker)
    │     ├── Phase 1: 리스팅 추출 (Pre-scan 캐시 재사용)
    │     ├── Phase 2: fetch_seller_info_fast()
    │     └── 저장
    │
    └── Signals:
          ├── prescan_progress(str, int, int)     # 카테고리명, 현재, 전체
          ├── prescan_complete(list)              # 조사 결과 목록
          ├── progress_category(str, int, int)    # 카테고리명, 현재, 전체
          ├── progress_item(str, int, int)        # goodscode, 현재, 전체
          ├── log_message(str)                    # 로그 텍스트
          ├── item_collected(dict)                # 수집된 레코드
          ├── error_occurred(str)                 # 에러 메시지
          └── finished(int, int)                  # 총 성공, 총 실패
```

### 7.3 UI 화면 구성

```
┌──────────────────────────────────────────────────────────────────┐
│  [Gmarket 판매자 수집기 v2.0]                     [_][□][X]      │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌─ 설정 ────────────────────────────────────────────────────┐   │
│  │ 카테고리: [✓전체선택] [신선식품] [가공식품] [생필품]...   │   │
│  │ 수집 수량: [200 ▼]   요청 간격: [0.5] 초                 │   │
│  │ 저장 경로: [C:\output        ] [찾아보기]                 │   │
│  └────────────────────────────────────────────────────────────┘   │
│                                                                  │
│  [사전 조사]  [수집 시작]  [일시정지]  [취소]  [이어서 수집]     │
│                                                                  │
│  ┌─ 사전 조사 결과 ──────────────────────────────────────────┐   │
│  │ 카테고리      | 전체 | 신규 | 완료 | 상태                 │   │
│  │ 신선식품      | 187  |  42  | 145  | 수집 가능            │   │
│  │ 가공식품      | 195  |  63  | 132  | 수집 가능            │   │
│  │ ...                                                       │   │
│  │ 합계: 3,847건 | 신규: 1,205건 | 예상: 14분 3초           │   │
│  └────────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌─ 진행 상황 ───────────────────────────────────────────────┐   │
│  │ 카테고리: [가공식품] (5/22)                                │   │
│  │ ████████████░░░░░░░░  127/200 (63%)                       │   │
│  │ 경과: 12:34  예상 남은 시간: 22:15                        │   │
│  │ 성공: 1,847  실패: 3  속도: 1.4건/초                     │   │
│  └────────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌─ 실시간 로그 ─────────────────────────────────────────────┐   │
│  │ [12:01:03] [가공식품] 4805535812 -> OK (5/6)              │   │
│  │ [12:01:04] [가공식품] 2435999418 -> OK (5/6)              │   │
│  │ [12:01:04] [가공식품] 1234567890 -> MISS                  │   │
│  └────────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌─ 수집 결과 미리보기 ──────────────────────────────────────┐   │
│  │ 스토어명        | 상호명      | 이메일    | 전화           │   │
│  │ Nong_shim off.. | 에스엠맨테크| sm@..     | 02-69..       │   │
│  └────────────────────────────────────────────────────────────┘   │
│                                                                  │
│  상태: 수집 완료 (1,957건) | 저장: C:\output\...                 │
└──────────────────────────────────────────────────────────────────┘
```

### 7.4 사용자 조작 플로우

```
[프로그램 시작]
    │
    ▼
[카테고리 선택 + 설정]
    │
    ▼
[사전 조사] 버튼 클릭
    │
    ▼
[Pre-scan 진행 중... 프로그레스 표시]
    │  (22개 카테고리 × ~5초 = 약 110초)
    ▼
[조사 결과 테이블 표시]
    │  (카테고리별 상품 수 / 신규 건수 / 예상 시간)
    ▼
[수집 시작] 버튼 클릭 (사용자 확인)
    │
    ▼
[Phase 2 수집 진행]
    │  (프로그레스 바 + 실시간 로그)
    ▼
[완료 및 저장]
```

### 7.5 취소/일시정지

- 수집 중 취소: 현재 건 완료 후 중지, 중간 저장 수행
- 일시정지: 현재 건 완료 후 대기, 재개 시 이어서 수집
- Pre-scan 중 취소: 즉시 StealthySession 종료

---

## 8. 상태 관리

### 8.1 collected_ids.json

```json
["1872449267", "2764998999", "2518517109", ...]
```

- 이미 수집한 goodscode 목록 (중복 방지)
- 50건마다 + 카테고리 종료 시 저장
- UI "초기화" 버튼으로 리셋 가능

### 8.2 fastcrawl_state.json

```json
{
  "completed": ["신선식품", "가공식품"],
  "total_success": 1957
}
```

- 카테고리별 완료 상태 (중단 후 재개용)
- "이어서 수집" / "처음부터" 선택 가능

---

## 9. 출력 파일

### 9.1 파일명 규칙

```
gmarket_fast_{카테고리명}_{YYYYMMDD_HHMMSS}.json
gmarket_fast_{카테고리명}_{YYYYMMDD_HHMMSS}.csv
```

- 카테고리명 특수문자(`/`, `\`)는 `_`로 치환
- CSV: utf-8-sig 인코딩 (Excel 호환)
- JSON: utf-8, indent=2

### 9.2 전체 통합 파일

```
gmarket_fast_ALL_{YYYYMMDD_HHMMSS}.json
gmarket_fast_ALL_{YYYYMMDD_HHMMSS}.csv
```

---

## 10. 의존성 및 환경

### 10.1 필수 패키지

```
requests>=2.28
beautifulsoup4>=4.12
scrapling>=0.4.11
PyQt6>=6.5 (또는 PySide6>=6.5)
```

### 10.2 설치 명령

```bash
pip install requests beautifulsoup4 scrapling PyQt6
scrapling install  # Chromium 브라우저 다운로드 (~300MB)
```

### 10.3 실행 환경

- Python 3.10+ (3.12 권장)
- Windows 10/11
- 네트워크: 한국 가정용 IP 기준 (프록시 불필요)

---

## 11. 성능 목표

| 지표 | 목표값 |
|------|--------|
| Pre-scan (22개 카테고리) | ~110초 |
| Phase 2 전체 수집 | ~35분 (2,000건 기준) |
| 건당 수집 시간 | 0.7초 (HTTP 0.2초 + 딜레이 0.5초) |
| 실패율 (429/403) | 0% |
| 카테고리당 goodscode | 150~200개 |
| 총 수집 성공 | ~1,900~2,000건 |

---

## 12. 구현 파일 구조

```
sft_gmarket_crawl_mgmode/
├── TECH_SPEC.md                  # 기술 명세서
├── WORK_ORDER.md                 # 본 작업지시서
├── src/
│   └── fast_crawl.py             # 핵심 수집 엔진 (기존)
├── app/
│   ├── __init__.py
│   ├── main.py                   # 프로그램 엔트리포인트
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py             # 카테고리 정의, 상수, 설정
│   │   ├── prescan.py            # Phase 0: 사전 조사 로직
│   │   ├── crawler.py            # Phase 1+2: 수집 엔진
│   │   └── storage.py            # 저장 (JSON/CSV/상태파일)
│   ├── workers/
│   │   ├── __init__.py
│   │   ├── prescan_worker.py     # QThread: Pre-scan 워커
│   │   └── crawl_worker.py       # QThread: 수집 워커
│   ├── models/
│   │   ├── __init__.py
│   │   └── records.py            # 데이터 클래스 (PrescanResult, SellerRecord)
│   ├── ui/
│   │   ├── __init__.py
│   │   ├── main_window.py        # 메인 윈도우
│   │   ├── widgets/
│   │   │   ├── __init__.py
│   │   │   ├── settings_panel.py # 설정 패널
│   │   │   ├── prescan_table.py  # 사전 조사 결과 테이블
│   │   │   ├── progress_panel.py # 진행 상황 패널
│   │   │   ├── log_panel.py      # 실시간 로그
│   │   │   └── result_table.py   # 수집 결과 미리보기
│   │   └── styles/
│   │       ├── __init__.py
│   │       └── theme.qss         # 스타일시트
│   └── utils/
│       ├── __init__.py
│       └── helpers.py            # 공통 유틸 (파일명 치환 등)
└── output/
    ├── collected_ids.json
    └── fastcrawl_state.json
```

---

## 13. 구현 우선순위

| 우선순위 | 작업 | 비고 |
|----------|------|------|
| P0 | Phase 0 Pre-scan 로직 (prescan.py) | **최우선** - 본 작업의 핵심 차별점 |
| P0 | Phase 2 판매자정보 수집 (crawler.py) | 기존 fast_crawl.py 이식 |
| P0 | QThread 워커 (prescan_worker, crawl_worker) | UI 응답성 보장 |
| P1 | 메인 윈도우 + 사전조사 테이블 UI | |
| P1 | 진행 상황 + 로그 패널 | |
| P1 | 저장 로직 (JSON/CSV/상태) | |
| P2 | 수집 결과 미리보기 테이블 | |
| P2 | 취소/일시정지/이어서 수집 | |
| P3 | PyInstaller 패키징 | Linux 빌드/기동 스모크 검증 완료(2026-07-24). Windows .exe는 Windows 환경에서 별도 빌드·검증 필요(크로스 컴파일 불가) |

---

## 14. 검증 기준 (Acceptance Criteria)

- [ ] 프로그램 시작 후 카테고리 선택 → [사전 조사] 클릭 시 22개 카테고리 상품 수가 테이블에 표시됨
- [ ] Pre-scan 결과에 신규/기수집 건수가 정확히 구분됨
- [ ] 예상 소요 시간이 계산되어 표시됨
- [ ] [수집 시작] 클릭 시 Pre-scan 캐시를 재사용하여 리스팅 재접근 없음
- [ ] Phase 2 수집 중 프로그레스 바 + 실시간 로그 정상 동작
- [ ] 50건마다 중간 저장 수행
- [ ] 수집 완료 시 JSON + CSV 파일 정상 생성
- [ ] 수집 중 [취소] 시 중간 저장 후 정상 종료
- [ ] collected_ids.json 기반 중복 수집 방지 동작
- [ ] "상품 없음" 카테고리는 자동 스킵

---

## 15. 주의사항

| 항목 | 내용 |
|------|------|
| 대표자명(ceo_name) | mg.gmarket.co.kr에 없음. 빈 문자열 유지 |
| 리스팅 상품 수 제한 | 카테고리당 최대 ~200개만 노출 (전체 상품 아님) |
| Phase 0/1 브라우저 필수 | Scrapling + Chromium 설치 (~300MB) |
| 요청 간격 | 0.3~1.0초 권장 (너무 빠르면 향후 차단 가능) |
| 동시 요청 | 절대 병렬 금지 (순차 1건씩) |
| Windows 파일명 | 카테고리명 `/` → `_` 치환 필수 |
| Pre-scan 세션 | StealthySession 1회만 생성/종료 (반복 생성 금지) |
