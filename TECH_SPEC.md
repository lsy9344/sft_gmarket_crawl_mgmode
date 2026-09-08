# Gmarket 판매자 수집 시스템 - 기술 명세서

> 버전: 2.0 (Fast Crawl)
> 작성일: 2026-07-24
> 용도: Windows Python UI 프로그램 구현 기반 문서

---

## 1. 시스템 개요

### 핵심 원리

Gmarket 모바일 글로벌 서브도메인(`mg.gmarket.co.kr`)의 판매자정보 페이지가
Cloudflare 보호 없이 직접 HTTP 접근 가능하다는 점을 이용한다.

```
[기존 방식] item.gmarket.co.kr → Cloudflare → 헤드리스 브라우저 필요 → 429 빈발
[신규 방식] mg.gmarket.co.kr  → 보호 없음  → 단순 HTTP GET → 차단 없음
```

### 2단계 수집 구조

| 단계 | 역할 | 기술 | 속도 |
|------|------|------|------|
| Phase 1 | 카테고리 리스팅에서 goodscode 목록 추출 | Scrapling StealthySession (헤드리스 브라우저) | 카테고리당 1회, ~5초 |
| Phase 2 | goodscode로 판매자정보 수집 | requests + BeautifulSoup (순수 HTTP) | 건당 0.2초 |

Phase 1은 Cloudflare 보호下の 리스팅 페이지 접근에 필요.
Phase 2는 보호 없는 엔드포인트이므로 브라우저/프록시 불필요.

---

## 2. 엔드포인트 명세

### 2.1 판매자정보 엔드포인트 (Phase 2 핵심)

```
GET https://mg.gmarket.co.kr/SellerInfo/SellerInfo?goodscode={goodscode}
```

| 항목 | 값 |
|------|-----|
| Method | GET |
| 인증 | 불필요 |
| Cloudflare | 없음 |
| 응답 형식 | HTML (서버 사이드 렌더링) |
| 응답 크기 | ~18-21 KB |
| 응답 시간 | 0.1~0.2초 |
| 서버 | Microsoft-IIS/10.0, ASP.NET MVC 4.0 |
| 레이트 리밋 | 미감지 (0.5초 간격 기준) |
| 에러 처리 | 유효하지 않은 goodscode → 302 리다이렉트 |

#### 필수 요청 헤더

```python
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}
```

#### 응답 HTML 구조

```html
<div class="vip-seller-info">
  <div class="vip-box-shadow">
    <!-- 스토어명 -->
    <h3 class="vip-article-title store-title">{스토어명} official store</h3>

    <ul class="vip-seller-info-list">
      <li class="list-row">
        <span class="list-label">Seller</span>
        <span class="list-selection">{상호명/사업자명}</span>
      </li>
      <li class="list-row">
        <span class="list-label">Representative</span>
        <span class="list-selection">{전화번호, 예: (+82) 02-6952-5311}</span>
      </li>
      <li class="list-row">
        <span class="list-label">Business hours</span>
        <span class="list-selection">{영업시간, 비어있을 수 있음}</span>
      </li>
      <li class="list-row">
        <span class="list-label">E-mail</span>
        <span class="list-selection">{이메일}</span>
      </li>
      <li class="list-row">
        <span class="list-label">FAX</span>
        <span class="list-selection">{FAX, 비어있을 수 있음}</span>
      </li>
      <li class="list-row">
        <span class="list-label">Business Registration No.</span>
        <span class="list-selection">{사업자등록번호}</span>
      </li>
      <li class="list-row">
        <span class="list-label">E-commerce Registration</span>
        <span class="list-selection">{통신판매업 신고번호}</span>
      </li>
      <li class="list-row">
        <span class="list-label">Address</span>
        <span class="list-selection">{주소}</span>
      </li>
    </ul>
  </div>
</div>
```

#### CSS 셀렉터 매핑

| 데이터 | 셀렉터 |
|--------|--------|
| 스토어명 | `h3.store-title` |
| 필드 목록 컨테이너 | `ul.vip-seller-info-list` |
| 각 행 | `li.list-row` |
| 라벨 | `span.list-label` |
| 값 | `span.list-selection` |

#### 라벨 → 필드 매핑 테이블

| HTML 라벨 (영문) | 매핑 필드 | 비고 |
|-----------------|-----------|------|
| Seller | company_name (상호명) | |
| Representative | phone (전화번호) | 대표자명 아님! 전화번호임 |
| E-mail | email | |
| Business Registration No. | business_number | |
| Address | address | 보너스 필드 |
| Business hours | (미수집) | |
| FAX | (미수집) | |
| E-commerce Registration | (미수집) | |

#### 미수집 필드

| 필드 | 사유 |
|------|------|
| 대표자명 (ceo_name) | 이 엔드포인트에 존재하지 않음. item.gmarket.co.kr (Cloudflare 보호)에만 있음 |

---

### 2.2 리스팅 페이지 (Phase 1)

#### 베스트 카테고리

```
GET https://www.gmarket.co.kr/n/best?groupCode={groupCode}
```

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

#### 슈퍼딜 카테고리

```
GET https://www.gmarket.co.kr/n/superdeal?categoryCode={categoryCode}
```

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

#### 리스팅 페이지 접근 조건

- Cloudflare Managed Challenge 보호下
- Scrapling StealthySession (Patchright 기반 헤드리스 Chromium) 필요
- JS 렌더링 필요 (goodscode 링크가 동적 생성)
- 베스트: `wait_selector='a[href*="goodscode"]'` 필요
- 슈퍼딜: 기본 타임아웃 20초로 충분

#### goodscode 추출 방법

```python
import re
codes = re.findall(r'goodscode=(\d+)', html_content)
codes = list(dict.fromkeys(codes))  # 순서 유지 중복 제거
```

- 카테고리당 약 150~200개 goodscode 추출됨

---

## 3. 데이터 스키마

### 수집 레코드 구조

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

### 필드 정의

| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| goodscode | string | Y | 상품 고유 코드 (숫자 문자열) |
| url | string | Y | 상품 페이지 URL (자동 생성) |
| store_name | string | Y | 스토어명 |
| company_name | string | Y | 사업자명/상호명 |
| ceo_name | string | N | 대표자명 (현재 미수집, 빈 문자열) |
| email | string | Y | 판매자 이메일 |
| phone | string | Y | 연락처 (국제번호 형식 포함) |
| business_number | string | Y | 사업자등록번호 |
| address | string | N | 사업장 주소 (보너스) |
| collected_at | string | Y | 수집 시각 (ISO 8601) |
| source | string | Y | 출처 카테고리명 |

### 성공 판정 기준

- store_name, company_name, email, phone, business_number 중 1개 이상 비어있지 않으면 성공
- 실제 관측: 대부분 5/6 필드 충전 (ceo_name 제외)

---

## 4. 수집 흐름 (UI 프로그램용)

### 전체 플로우

```
┌─────────────────────────────────────────────────────────┐
│ 1. 사용자 설정                                           │
│    - 수집할 카테고리 선택 (22개 중)                      │
│    - 카테고리당 최대 수집 수 (기본 200)                  │
│    - 요청 간격 (기본 0.5초)                              │
│    - 저장 경로                                           │
└────────────────────────┬────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────┐
│ 2. Phase 1: 리스팅 추출 (카테고리당 1회)                │
│    - StealthySession 시작                                │
│    - gmarket.co.kr warmup (Cloudflare 쿠키 획득)         │
│    - 카테고리 리스팅 페이지 fetch                         │
│    - HTML에서 goodscode 목록 regex 추출                  │
│    - 세션 종료                                           │
│    ※ UI: "카테고리 X 리스팅 추출 중..." 프로그레스        │
└────────────────────────┬────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────┐
│ 3. 중복 필터링                                           │
│    - collected_ids.json에서 이미 수집한 goodscode 제외    │
│    - 신규 대상만 선별                                    │
│    ※ UI: "신규 N건 / 전체 M건" 표시                      │
└────────────────────────┬────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────┐
│ 4. Phase 2: 판매자정보 수집 (건별 HTTP GET)              │
│    - requests.Session으로 GET 요청                       │
│    - BeautifulSoup으로 HTML 파싱                         │
│    - 결과 레코드 생성                                    │
│    - 50건마다 중간 저장                                  │
│    - 요청 간 딜레이 (0.5초 + 랜덤 0~0.3초)              │
│    ※ UI: 프로그레스 바 (현재/전체), 실시간 로그           │
└────────────────────────┬────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────┐
│ 5. 저장                                                  │
│    - JSON + CSV (utf-8-sig, Excel 호환)                  │
│    - collected_ids.json 업데이트                         │
│    - 상태 파일 (fastcrawl_state.json) 업데이트           │
│    ※ UI: 저장 완료 알림, 파일 경로 표시                  │
└─────────────────────────────────────────────────────────┘
```

### 에러 처리

| 상황 | 처리 | UI 표시 |
|------|------|---------|
| HTTP 302 (유효하지 않은 goodscode) | 해당 건 스킵 | "건너뜀" |
| HTTP 500/타임아웃 | 해당 건 스킵, 계속 진행 | "실패" (빨간색) |
| 네트워크 오류 | 3회 재시도 후 스킵 | "재시도 중..." |
| Phase 1 리스팅 403 | Cloudflare 차단, 30초 대기 후 재시도 | "차단 감지, 대기 중..." |
| Phase 1 리스팅 0건 | 해당 카테고리 스킵 | "상품 없음" |

### 취소/일시정지 (UI 필수 기능)

- 수집 중 취소: 현재 건 완료 후 중지, 중간 저장 수행
- 일시정지: 현재 건 완료 후 대기, 재개 시 이어서 수집
- 스레드 분리: UI 스레드와 수집 워커 스레드 분리 (응답성 유지)

---

## 5. 의존성

### 필수 패키지

```
requests>=2.28
beautifulsoup4>=4.12
scrapling>=0.4.11
```

### Scrapling 내부 의존성 (자동 설치)

- Patchright (Playwright fork, 헤드리스 Chromium)
- browserforge (브라우저 핑거프린트 생성)
- curl_cffi (TLS 핑거프린트)

### 설치 명령

```bash
pip install requests beautifulsoup4 scrapling
scrapling install  # Chromium 브라우저 다운로드
```

### Python 버전

- 3.10+ (match 문법, `dict | None` 타입 힌트 사용)
- 테스트 환경: Python 3.12 (Windows)

---

## 6. 성능 지표 (실측)

| 지표 | 값 |
|------|-----|
| 전체 22개 카테고리 수집 시간 | ~35분 |
| 카테고리 1개 리스팅 추출 | ~5초 |
| 판매자정보 1건 수집 | ~0.2초 (HTTP) + 0.5초 (딜레이) |
| 실패율 (429/403) | 0% |
| 카테고리당 추출 goodscode | 150~200개 |
| 총 수집 성공 | 1,957건 / 22개 카테고리 |
| 프록시 사용 | 불필요 |
| 트래픽 | ~69 MB 전체 (건당 ~21 KB) |

### 속도 비교

| 방식 | 22개 카테고리 | 429 발생 | 비용 |
|------|-------------|---------|------|
| 기존 StealthySession 전체 | 10~12시간 | 빈발 (70%) | 프록시 시 8~18만원 |
| **신규 Fast Crawl** | **35분** | **0건** | **무료** |

---

## 7. 상태 관리

### collected_ids.json

```json
["1872449267", "2764998999", "2518517109", ...]
```

- 이미 수집한 goodscode 목록 (중복 방지)
- 50건마다 + 카테고리 종료 시 저장
- UI에서 "초기화" 버튼으로 리셋 가능

### fastcrawl_state.json

```json
{
  "completed": ["신선식품", "가공식품", ...],
  "total_success": 1957
}
```

- 카테고리별 완료 상태 (중단 후 재개용)
- UI에서 "이어서 수집" / "처음부터" 선택 가능

---

## 8. 출력 파일

### 파일명 규칙

```
gmarket_fast_{카테고리명}_{YYYYMMDD_HHMMSS}.json
gmarket_fast_{카테고리명}_{YYYYMMDD_HHMMSS}.csv
```

- 카테고리명의 `/`, `\` 등 특수문자는 `_`로 치환
- CSV: utf-8-sig 인코딩 (Excel 열기 호환)
- JSON: utf-8, indent=2

### UI 추가 출력 옵션 (확장)

- Excel (.xlsx) 직접 저장
- SQLite DB 저장
- 전체 통합 파일 (ALL 라벨)

---

## 9. UI 프로그램 설계 가이드

### 권장 기술 스택

| 항목 | 권장 | 대안 |
|------|------|------|
| UI 프레임워크 | PyQt6 / PySide6 | tkinter, CustomTkinter |
| 스레딩 | QThread + Signal/Slot | threading + queue |
| 테이블 표시 | QTableWidget / QTableView | pandas + matplotlib |
| 패키징 | PyInstaller | cx_Freeze, Nuitka |

### 핵심 UI 구성

```
┌──────────────────────────────────────────────────────────┐
│  [Gmarket 판매자 수집기 v2.0]                    [_][□][X] │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  ┌─ 설정 ─────────────────────────────────────────────┐  │
│  │ 카테고리: [✓全选] [신선식품] [가공식품] [생필품]... │  │
│  │ 수집 수량: [200 ▼]   요청 간격: [0.5] 초          │  │
│  │ 저장 경로: [C:\output        ] [찾아보기]          │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  [수집 시작]  [일시정지]  [취소]  [이어서 수집]          │
│                                                          │
│  ┌─ 진행 상황 ────────────────────────────────────────┐  │
│  │ 카테고리: [가공식품] (5/22)                         │  │
│  │ ████████████░░░░░░░░  127/200 (63%)                │  │
│  │ 경과: 12:34  예상 남은 시간: 22:15                 │  │
│  │ 성공: 1,847  실패: 3  속도: 1.4건/초              │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  ┌─ 실시간 로그 ──────────────────────────────────────┐  │
│  │ [12:01:03] [가공식품] 4805535812 -> OK (5/6)       │  │
│  │ [12:01:04] [가공식품] 2435999418 -> OK (5/6)       │  │
│  │ [12:01:04] [가공식품] 1234567890 -> MISS           │  │
│  │ ...                                                │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  ┌─ 수집 결과 미리보기 ───────────────────────────────┐  │
│  │ 스토어명        | 상호명      | 이메일    | 전화   │  │
│  │ Nong_shim off.. | 에스엠맨테크| sm@..     | 02-69..│  │
│  │ CJ official st..| (주)CJ제일제당| cj@..    | 02-67..│  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  상태: 수집 완료 (1,957건) | 저장: C:\output\...        │
└──────────────────────────────────────────────────────────┘
```

### 스레드 구조

```
[Main Thread - UI]
    │
    ├── QThread (Worker)
    │     ├── Phase 1: fetch_listing_codes()  → Signal: progress_category
    │     ├── Phase 2: fetch_seller_info_fast() → Signal: progress_item
    │     └── 저장 → Signal: save_complete
    │
    └── Signals:
          ├── progress_category(str, int, int)  # 카테고리명, 현재, 전체
          ├── progress_item(str, int, int)      # goodscode, 현재, 전체
          ├── log_message(str)                  # 로그 텍스트
          ├── item_collected(dict)              # 수집된 레코드
          ├── error_occurred(str)               # 에러 메시지
          └── finished(int, int)                # 총 성공, 총 실패
```

### 취소 구현

```python
class CrawlWorker(QThread):
    def __init__(self):
        self._cancel = False
        self._pause = False

    def cancel(self):
        self._cancel = True

    def pause(self):
        self._pause = True

    def resume(self):
        self._pause = False

    def run(self):
        for code in targets:
            if self._cancel:
                self.save_intermediate()
                break
            while self._pause:
                time.sleep(0.1)
            # 수집 로직...
```

---

## 10. 주의사항 및 제약

### 기술적 제약

| 항목 | 설명 |
|------|------|
| 대표자명 미수집 | mg.gmarket.co.kr에 해당 필드 없음. 필요 시 item.gmarket.co.kr (Cloudflare) 접근 필요 |
| 리스팅 상품 수 제한 | 카테고리 리스팅 페이지가 최대 ~200개만 노출 (전체 상품이 아님) |
| Phase 1 브라우저 필요 | 리스팅 추출에는 Scrapling + Chromium 필수 (설치 ~300MB) |
| Windows 파일명 | 카테고리명에 `/` 포함 → 저장 시 `_`로 치환 필수 |

### 운영 가이드

| 항목 | 권장값 |
|------|--------|
| 요청 간격 | 0.3~1.0초 (0.5초 권장, 너무 빠르면 향후 차단 가능) |
| 동시 요청 | 1개 (병렬 금지) |
| 재수집 주기 | 주 1회 이하 권장 |
| collected_ids 초기화 | 새 상품 수집 시에만 (기존 데이터 유지) |

### 법적 고려

- 수집 데이터: 공개된 사업자 정보 (전자상거래법상 공개 의무)
- robots.txt: mg.gmarket.co.kr은 크롤링 제한 없음
- 이용 약관: 대량 자동 접근은 약관 위반 소지 → 과도한 빈도 금지

---

## 11. 기존 코드 참조

| 파일 | 역할 |
|------|------|
| `src/fast_crawl.py` | 핵심 수집 엔진 (이 문서의 구현체) |
| `src/crawler.py` | 기존 v3.1 크롤러 (StealthySession 전체, 참고용) |
| `src/full_sweep.py` | 기존 전체 순회 스크립트 |
| `src/auto_crawl.py` | 기존 자동 카테고리 전환 |
| `output/collected_ids.json` | 중복 방지 ID 목록 |
| `output/fastcrawl_state.json` | 카테고리별 진행 상태 |

---

## 12. 테스트 검증 결과

```
수집일: 2026-07-24
환경: Windows 10, Python 3.12, 단일 한국 가정용 IP
결과: 22/22 카테고리 완료, 1,957건 성공, 0건 차단
소요: ~35분 (Phase 1 리스팅 포함)
```
