# 판매자 정보 수집기 v3.0 (Gmarket + Coupang + Foodspring)

Gmarket 베스트 10 + 슈퍼딜 12 = **22개 카테고리**의 판매자 사업자정보를 수집하는
Windows용 PyQt6 데스크톱 프로그램. 프로그램 시작 시 **사전 조사(Pre-scan)** 로
카테고리별 상품 수·신규 대상 수를 먼저 파악한 뒤 사용자 확인을 거쳐 수집한다.

> 상세 사양: [`WORK_ORDER.md`](WORK_ORDER.md) · [`TECH_SPEC.md`](TECH_SPEC.md)
> **Coupang 수집**: [`docs/coupang/`](docs/coupang/) 참조 — `/np/omp` '전체' 탭 + 스토어 API 3개로 필수 7항목 100% 확보.
> **Foodspring 수집**: [`foodspring_crawl/REPORT_CRAWL_METHOD.md`](foodspring_crawl/REPORT_CRAWL_METHOD.md) 참조 —
> `/special/wcpd` 전국 택배 배송 기획전 상품 10,000개 + 판매자 사업자정보를 Excel로 출력.
> Gmarket/Coupang/Foodspring **3탭 UI**로 통합 완료.

## 설치

```bash
pip install -r requirements.txt

# 브라우저 런타임 (소스 실행): 고정 버전 순서를 지키며 설치
python scripts/setup_coupang_runtime.py
```

배포본은 `SellerCollector.exe` 단일 파일이다. 최초 1회 명령 프롬프트에서
런타임 설치 모드를 실행한 뒤 GUI를 연다.

```bat
SellerCollector.exe --setup-runtime
SellerCollector.exe --verify-runtime
SellerCollector.exe
```

수동 설치가 필요하면 반드시 `sync → fetch 고정버전 → set 고정버전` 순서를 사용한다:

```bash
python -m camoufox sync
python -m camoufox fetch official/stable/152.0.4-beta.28
python -m camoufox set official/stable/152.0.4-beta.28
patchright install chromium
```

> `requirements.txt` 는 base `scrapling` + StealthySession 런타임 의존(`patchright`,
> `msgspec`, `anyio`, `protego`, `curl_cffi`)과 `camoufox[geoip]==0.5.4` 를 설치한다.
> `scrapling[fetchers]` extra 는 `playwright==1.61` 을 강제해 camoufox(playwright<1.61)
> 와 충돌하므로 쓰지 않는다 — StealthySession 은 playwright 비의존인 patchright 를
> 쓰므로 playwright 1.60 으로 동작한다. 두 탭 모두 브라우저 미설치 상태에서도 앱은
> 기동되며, Gmarket은 정확한 Patchright Chromium revision을, Coupang은
> Camoufox/브라우저/GeoIP를 preflight로 확인해 setup 도구를 안내한다.

- Python 3.10+ (3.12 권장), Windows 10/11
- 네트워크: 한국 가정용 IP 기준 (프록시 불필요)

## 실행

```bash
python -m app.main
```

(프로젝트 루트에서 실행. `python app/main.py` 로도 실행 가능하다.)

## 사용 플로우 (WORK_ORDER §7.4)

1. **카테고리 선택 + 설정** — 수집 수량(기본 200), 요청 간격(기본 0.5초), 저장 경로
2. **[사전 조사]** — 22개 카테고리 리스팅을 조사하여 전체/신규/이미수집 건수와
   예상 소요 시간을 테이블에 표시 (StealthySession 1회 생성/종료)
3. **[수집 시작]** — Pre-scan 캐시(goodscode)를 재사용해 리스팅 재접근 없이 Phase 2 수집
4. **완료/저장** — 카테고리별 + 통합(ALL) JSON/CSV 저장

부가 버튼: **[일시정지]/[재개]**, **[취소]**(현재 건 완료 후 중단 + 중간 저장),
**[이어서 수집]**(사전 조사 후 남은 신규 건만 자동 수집), **[초기화]**(수집 이력/상태 리셋).

## Bright Data 계정 설정 (2026-09-09 신규 "설정" 탭)

Gmarket 카테고리 탭(Web Unlocker)과 Coupang 카테고리 탭(ISP 프록시, 선택)의
Bright Data 사용량은 **설정 탭에 입력한 자신의 계정 API 토큰에서 차감**된다.
토큰·존·국가 입력, 잔액 조회(토큰 검증), ISP 프록시 자격(계정 ID/존/비밀번호 —
"토큰으로 가져오기"로 자동 채움)을 지원하며, 자격 증명은 저장소 밖
`output/brightdata_settings.json` 에만 저장된다. 상세:
[`docs/BRIGHTDATA_ACCOUNT_SETTINGS.md`](docs/BRIGHTDATA_ACCOUNT_SETTINGS.md).

### 차단 방어·수집량 기본값 (2026-09-09 검토 반영 — AKAMAI_REVIEW §11)

- **쿠팡 2차 판매자 API 연속 실패 중단**: getStoreReview 가 연속 5회 실패하면
  `blocked` 로 중단하고 blockguard 쿨다운을 기록한다 — 차단 진행 중 재요청으로
  회선 IP 평판을 더 깎지 않는다. individualInfo 배치는 연속 3회.
- **Gmarket 리스팅 빈 껍데기 회복**: 200 수신 + goodscode 0개 응답은 1회
  재요청으로 회복을 시도한다(대량 실측에서 재시도 81% 회복). `x-brd-error`
  헤더가 있는 200 은 빈 페이지가 아니라 오류로 재시도한다.
- **Gmarket 판매자정보 연속 실패 중단**: mg 요청이 연속 10건 실패하면 그
  카테고리를 중단하고 부분 저장한다(실패 건은 커밋되지 않아 이어서 수집됨).
- **기본값 정렬**: 쿠팡 카테고리 최대 페이지 17→30(종료 판정이 실제 목록 끝을
  담당 — 과거 "17페이지 상한"은 §8 실측으로 폐기), 2차 요청 간격 3초 중심
  (2.5~3.5초, 1,794건 무차단 실측값).
- Gmarket 카테고리 탭은 시작 전 잔액을 조회해 예상 요청/비용을 로그로 남기고,
  잔액 부족이 예상되면 확인을 요청한다.

## 아키텍처

```
app/
├── main.py                 # 엔트리포인트 (QApplication + theme.qss)
├── core/
│   ├── config.py           # 카테고리 22종·엔드포인트·타이밍 상수 (단일 설정 소스)
│   ├── base.py             # Control (취소/일시정지, threading.Event 기반)
│   ├── prescan.py          # Phase 0: StealthySession 1회로 전체 카테고리 조사
│   ├── plan.py             # [수집 시작] 시점에 확정되는 불변 CrawlPlan
│   ├── crawler.py          # Phase 2: CrawlPlan 그대로 실행 + 판매자정보 수집·저장
│   └── storage.py          # JSON/CSV(utf-8-sig) + collected_ids/state/체크포인트 관리
├── models/records.py       # PrescanResult, SellerRecord
├── workers/                # QThread 워커 (엔진 콜백 → pyqtSignal 중계)
│   ├── prescan_worker.py
│   └── crawl_worker.py
├── ui/
│   ├── main_window.py      # 위젯·워커 배선, 사용자 플로우/상태 전이
│   ├── widgets/            # settings/prescan_table/progress/log/result 패널
│   └── styles/theme.qss
└── utils/helpers.py        # 파일명 치환·goodscode 추출·시간 포맷
```

**설계 원칙**: `core` 엔진은 Qt에 의존하지 않는다. `Control`(취소/일시정지)은
**UI 스레드의 `MainWindow`가 생성·소유**하고 워커(`QThread`) 생성자에 주입한다 —
워커는 이를 그대로 엔진(`Prescanner`/`SellerCrawler`)에 전달할 뿐 절대 새로
만들지 않는다. 이렇게 해야 백그라운드 스레드가 도는 중에도 UI 스레드에서 즉시
일시정지/취소 버튼을 통해 `Control` 상태를 바꿀 수 있다. 엔진 콜백(로그/진행/
수집 결과)은 워커가 `pyqtSignal`로 UI 스레드에 중계하므로 UI 응답성이 유지된다.
취소는 **건 경계**에서만 반영되어 진행 중인 건과 부분 결과를 유실하지 않는다.

**내구성 원칙**: goodscode 를 `collected_ids.json`에 "수집 완료"로 커밋하기 전에
그 결과 레코드를 먼저 디스크(체크포인트 또는 최종 결과 파일)에 저장한다.
`SellerCrawler` 는 성공한 건을 즉시 공유 `collected_ids` 에 반영하지 않고
로컬 `newly_collected` 집합에만 모아두었다가, 체크포인트 저장이 **성공한
뒤에만** 병합·커밋한다(체크포인트 저장이 실패하면 예외가 그대로 전파되고
`collected_ids` 는 호출 전 상태 그대로 유지된다). 이 순서를 지키면 중간에
프로세스가 죽어도 최악의 경우 재수집(중복)만 발생할 뿐, "ID 는 수집됨으로
표시됐는데 결과 파일에는 없는" 영구 유실은 발생하지 않는다.

**계획 고정 원칙**: [수집 시작]을 누르는 순간, `build_crawl_plan()` 이 그
시점의 `collected_ids` 스냅샷으로 카테고리별 `target_codes` 를 `max_items`
캡까지 포함해 완전히 확정한 불변 `CrawlPlan` 을 만든다. `SellerCrawler.crawl()`
은 이 계획을 그대로 실행하며, 실행 도중 `collected_ids.json` 이 바뀌어도
(다른 프로세스, 체크포인트 승격 등) 이미 확정된 대상 목록 자체는 흔들리지
않는다 — Pre-scan 에서 사용자가 확인한 대상과 실제로 수집되는 대상이 항상
일치한다. 카테고리가 `max_items` 로 잘려 일부만 수집됐다면(`capped=True`)
`fastcrawl_state.json` 에 "완료"로 기록하지 않는다. 같은 goodscode 가 서로
다른 카테고리 리스팅에 함께 노출되는 경우(교차 태깅)에도 `build_crawl_plan()`
이 이미 앞선 카테고리가 예약한 코드를 추적(`reserved`)해 뒤따르는 카테고리가
같은 코드를 또 담지 않도록 막는다 — 그렇지 않으면 같은 건이 두 번 수집되어
ALL 통합 파일에 중복 행이 남는다.

**체크포인트 승격**: [수집 시작] 시 `CrawlPlan` 을 확정하기 *전에*
`main_window.on_start()` 가 먼저 `reconcile_leftover_checkpoints()` 를 호출해
Pre-scan 결과/계획과 무관하게 출력 디렉터리를 직접 훑어
(`Storage.find_leftover_partials`) 남아 있는 모든 체크포인트를 최종 파일로
승격한다(`SellerCrawler.crawl()` 시작 시에도 방어적으로 한 번 더 실행한다 —
UI 를 거치지 않고 `crawl()` 을 직접 호출하는 경로를 위한 이중 안전장치).
어떤 카테고리가 다음 조사에서 "완료됨"으로 판정되어 계획에서 제외되더라도,
이전에 죽은 실행이 남긴 체크포인트 데이터가 영구히 숨겨지지 않는다. 승격을
계획 확정보다 먼저 실행해야, 아직 승격되지 않은 체크포인트 속 goodscode 를
계획이 "신규"로 잘못 담는 어긋남도 함께 방지된다.

승격 결과는 `ReconcileResult` 로 구조화된다 — `promoted_ids`(새로 커밋된
코드), `failed_labels`(결과 저장 자체 실패), `corrupt_labels`(손상되어 격리된
체크포인트), `quarantine_failed_labels`(손상 감지 + **격리(백업)조차 실패**해
원본이 위험하게 남은 경우), `cleanup_failed_labels`(저장은 성공했지만
체크포인트 삭제 실패), `commit_failed_labels`(저장은 성공했지만
`collected_ids` 저장 실패). 이 중 `failed_labels`/`corrupt_labels`/
`quarantine_failed_labels` 가 하나라도 있으면(`.blocking`) **fail-closed** 로
그 즉시 오류를 반환하고 **단 하나의 카테고리도 수집을 시작하지 않는다**:
- **결과 저장 자체 실패**: 실패한 카테고리를 바로 이어서 새로 수집하면
  체크포인트가 새 데이터로 덮어써져 미승격 상태로 남아있던 이전 데이터가
  영구히 사라질 수 있다(그 사이 ID 는 이미 커밋돼 있어 "유령 ID"가 된다).
- **손상되어 격리된 체크포인트**: 문법은 유효하지만 구조가 잘못된 JSON 은
  원본을 `*.corrupt_*.bak` 으로 백업하지만 자동으로 정상 결과로 복구되지는
  않는다 — 다른 카테고리까지 포함해 전체 수집을 막고 로그로 명확히 알린다.
- **격리(백업) 자체가 실패한 경우**: `Storage._quarantine_corrupt()` 가
  rename 도, 대체 수단인 복사도 모두 실패하면(디스크 공간 부족 등) 원본이
  보호되지 않은 채 원래 자리에 그대로 남는다. `Storage.load_partial_results_status()`
  가 이를 `LoadStatus.QUARANTINE_FAILED` 로 명시적으로 구분해 반환하므로,
  절대 "빈 체크포인트"로 오인해 그 원본을 지우는 일이 없다.

반면 `cleanup_failed_labels`/`commit_failed_labels` 는 fail-closed 대상이
아니다 — 결과 데이터 자체는 이미 durable 하게 저장됐으므로, 최악의 경우
체크포인트 파일이 남거나 ID 커밋이 다음 조사로 미뤄지는 데 그친다(재수집만
발생, 영구 유실 없음).

승격은 **결정적 파일명**(`{라벨}_recovered_{내용해시}`)에 저장한다
(`Storage.promote_partial`/`promoted_partial_path`). 내용 해시는 **레코드
전체(필드 값 포함)** 의 canonical JSON 을 SHA-256 으로 해싱한다 — goodscode
목록만 해싱하면 같은 상품이라도 판매자 정보가 갱신되어 다른 내용으로 다시
체크포인트된 경우 예전 해시와 충돌해 서로 다른 데이터가 같은 파일을 덮어쓸
위험이 있다. 같은 내용을 다시 승격해도 항상 같은 경로에 안전하게
덮어쓰고(멱등), 내용이 다른 별개의 승격 사례는 해시도 달라 서로 다른 파일에
남아 충돌하지 않는다.

체크포인트 승격(결정적 파일명)과 카테고리 정상 최종 저장(`save_results`,
타임스탬프 파일명)은 서로 다른 이름 체계를 쓴다. 정상 저장 직후 체크포인트
삭제만 실패해 같은 체크포인트가 다음 실행에도 남아 있으면, reconcile 이 이를
"아직 승격 안 됨"으로 오인해 별도의 `_recovered_{hash}` 파일을 또 만들어
같은 goodscode 가 두 파일에 중복 출현할 수 있다. 이를 막기 위해 **승격 완료
manifest**(`.promoted_hashes.json`, `Storage.mark_promoted`/
`load_promotion_manifest`)를 둔다 — 정상 저장이 끝나면 그 결과 파일 경로를
이 manifest 에 기록한다.

**manifest 는 신뢰의 근거가 아니라 참고용 힌트일 뿐이다**(6차 리뷰 HIGH-2
회귀 방지) — reconcile 은 두 단계로 "이미 승격됐는지"를 판단한다: ① 먼저
`promoted_partial_path(label, content_hash)` 로 계산되는 결정적 경로가
**실제로 디스크에 존재하는지 직접 확인**한다(manifest 없이도 가능 — 파일명
자체가 결정적이므로). ② 없다면 manifest 에서 힌트를 찾되, 참조된 파일이
**실제로 아직 존재하는지 검증한 뒤에만** 신뢰한다. 두 경우 모두 아니면
실제로 승격을 실행한다. 이 설계 덕분에 manifest 가 손상되거나 사라져도
(6차 리뷰 HIGH-3) 최악의 경우 이미 정상 저장된 내용이 `_recovered_hash`
파일로 한 번 더(중복) 승격될 뿐, 데이터가 유실되지는 않는다 — manifest
로드 실패를 fail-closed 로 막을 필요가 없다.

체크포인트 삭제는 **결과 저장(또는 이미 존재 확인) + manifest 기록 + ID
커밋이 모두 성공한 뒤에만** 시도한다(6차 리뷰 HIGH-1 회귀 방지) — 예전에는
`collected_ids` 저장이 실패해도 체크포인트를 무조건 지웠는데, 그러면
재시작 후 `collected_ids.json` 에도 체크포인트에도 그 goodscode 가 남지
않아 다음 조사가 "신규"로 오판했다. 이제는 ID 커밋이 실패하면 체크포인트를
보존한다 — 다음 재시도는 위 ①의 파일 존재 확인 덕분에 재승격 없이 곧장 ID
커밋만 다시 시도한다. `Storage.clear_partial()` 은 삭제 성공 여부를
**반환값(bool)** 으로 알린다.

**collected_ids.json 손상 격리 실패도 fail-closed** 다(6차 리뷰 HIGH-4):
collected_ids.json 은 중복 방지의 단일 진실 공급원이므로, 격리(백업)조차
실패해 원본이 위험한 상태로 남아있는데 빈 집합으로 계속 진행하면 대량
재수집이 발생하고, 그 뒤 저장이 백업 없는 원본을 조용히 덮어써 영구히
잃는다. `crawl()`/`main_window.on_start()` 모두 `load_collected_ids_status()`
로 이 상태를 확인하고 실행을 중단한다. `fastcrawl_state.json`(참고용
이력)과 승격 manifest 도 같은 위험이 있지만 안전이 덜 critical 해서
`mark_completed()`/`mark_promoted()` 가 QUARANTINE_FAILED 일 때 그 위에
덮어쓰지만 않도록(원본 보존) 조용히 건너뛴다.

**직접 호출(재계획 필요) 방어선**: `SellerCrawler.crawl(plan)` 은 UI 를 거치지
않고 미리 만들어둔 `CrawlPlan` 으로 직접 호출될 수도 있다. 이 경우 방금 실행한
승격이 `collected_ids` 를 바꿨는데, 전달받은 계획이 그 변경 이전 스냅샷으로
만들어진 낡은 계획이라면 그대로 실행 시 같은 goodscode 가 승격분과
신규재수집분으로 두 번 저장된다. 승격 후 `collected_ids` 와 `plan` 의
`target_codes` 가 겹치면 `summary.replan_required=True` 로 표시하고 계획을
실행하지 않는다. `main_window.on_start()` 는 `CrawlPlan` 을 만들기 *전에* 이미
승격을 실행하므로 정상 UI 경로에서는 이 조건이 절대 발생하지 않는다 — 이는
core 계층을 UI 없이 직접 쓰는 코드/테스트를 위한 방어선이다.

**출력 경로 일치 검증**: `crawl()` 은 시작 시 `CrawlPlan.output_dir` 이 실제
`Storage.output_dir` 과 일치하는지 확인한다(계획의 카테고리가 비어 있어도
검사한다 — 빈 계획이라고 봐주면 다른 Storage 경로의 체크포인트가 이 검사
없이 조용히 승격될 수 있다). 둘이 어긋나면(설정/코드 실수) 감사용
`plan_hash`/`plan.output_dir` 는 A 를 가리키는데 실제 파일은 B 에 조용히
저장되는 위험한 불일치가 생길 수 있어, 하드 실패로 조기에 잡는다.

**저장 경로 단위 잠금**(6차 리뷰 MEDIUM): `Storage` 는 생성 시 출력
디렉터리에 배타적 잠금을 건다(Windows 는 경로 해시 기반 named mutex,
POSIX 는 `.gmarket_fast.lock` + `fcntl`). 서로 다른 프로세스(예: 사용자가 실행 파일을 실수로 두
번 실행)가 같은 저장 경로를 동시에 쓰면 체크포인트의 고정 `.tmp` 파일이나
manifest 의 read-modify-write 가 서로 덮어쓰며 손상될 수 있기 때문이다.
잠금은 **인스턴스 단위가 아니라 프로세스+경로 단위**로 한 번만 건다 —
`main_window._make_storage()` 는 버튼을 누를 때마다 같은 경로로 새
`Storage` 인스턴스를 만드는데, 이 기존 패턴이 자기 자신과 충돌하면 안
되기 때문이다. 실제로 막는 것은 "다른 프로세스가 같은 경로를 쓰는 상황"
뿐이다. 잠금을 만들거나 적용할 수 없으면 상태/결과 안전을 보장할 수 없으므로
수집을 시작하지 않는 fail-closed 방식이다.

## 출력 (`output/` 또는 사용자 지정 경로)

- `gmarket_fast_{카테고리}_{YYYYMMDD_HHMMSS}.json` / `.csv` — 카테고리별
  (같은 초에 같은 라벨로 중복 저장되면 `_2`, `_3` ... 을 붙여 덮어쓰지 않는다)
- `gmarket_fast_ALL_{YYYYMMDD_HHMMSS}.json` / `.csv` — 통합
- `gmarket_fast_{카테고리}_recovered_{내용해시}.json` / `.csv` — 이전 실행이
  남긴 체크포인트를 승격한 결과(체크포인트 삭제까지 정상적으로 끝났다면
  보통 남지 않는다). 같은 체크포인트를 재승격해도 항상 같은 파일에
  덮어쓰므로(멱등) 중복 파일이 쌓이지 않는다.
- `collected_ids.json` — **중복 방지의 단일 진실 공급원(source of truth)**.
  Pre-scan 은 항상 이 파일과 대조해 신규/기수집 여부를 다시 계산하므로,
  `fastcrawl_state.json`의 "완료" 표시와 무관하게 항상 정확하다.
- `fastcrawl_state.json` — 카테고리별 누적 성공 건수 등 **참고용 이력**. 재개
  로직을 이 파일에 의존하지 않는다(위 collected_ids 기준이 항상 우선).
- `.partial_gmarket_fast_{카테고리}.json` — 카테고리 수집 도중의 체크포인트
  파일(숨김성 접두사 `.`). 정상 종료 시 자동 삭제되며, 비정상 종료 후 다음
  실행에서 자동으로 복구·병합된다. 사용자가 직접 다룰 필요는 없다.
- `{원본파일명}.corrupt_{timestamp}.bak` — `collected_ids.json`/`fastcrawl_state.json`/
  체크포인트 파싱이 손상되어 실패하면 원본을 이 이름으로 백업하고 빈 상태로
  새로 시작한다(자동 복구/손실 방지용 백업이며 자동으로 지워지지 않으니 확인
  후 정리 가능). 확장자를 `.json` 이 아닌 `.bak` 으로 둔 이유는, 체크포인트
  백업이 `.json` 으로 끝나면 체크포인트 탐색 glob(`.partial_*.json`)에
  백업 파일 자신이 다시 걸려 재귀적으로 또 손상 처리되거나(`.corrupt_T1.corrupt_T2.json`),
  `[초기화]`가 유일한 백업까지 지워버리는 사고가 있었기 때문이다 —
  `Storage.find_leftover_partials()` 도 `.corrupt_` 를 포함한 이름은 한 번 더
  방어적으로 제외한다. 타임스탬프는 마이크로초까지 포함하고, 그래도 경로가
  충돌하면 `_2`, `_3` ... 을 붙여 먼저 만든 백업을 덮어쓰지 않는다(6차 리뷰
  MEDIUM 회귀 방지 — 초 단위만 쓰면 같은 파일이 같은 초에 두 번 손상·격리될
  때 앞선 백업이 조용히 사라질 수 있었다).
- `.promoted_hashes.json` — 체크포인트 승격 완료 manifest(내부 관리용,
  사용자가 직접 다룰 필요 없음 — 손상되거나 사라져도 데이터 유실 없이
  최악의 경우 중복 파일 하나가 다시 생성될 뿐이다).
- `.gmarket_fast.lock` — POSIX 저장 경로 단위 프로세스 잠금 파일(내부 관리용).
  Windows에서는 같은 경로 해시를 사용하는 OS named mutex로 잠그므로 이 파일은
  생성되지 않는다.

`[초기화]` 버튼은 `collected_ids`/`fastcrawl_state`뿐 아니라 남은 체크포인트
파일도 모두 삭제한다. 체크포인트 삭제가 일부라도 실패하면(디스크/권한 문제)
"초기화 완료"로 조용히 넘기지 않고 실패한 파일명을 명시한 경고를 띄운다 —
그렇지 않으면 다음 실행의 체크포인트 승격 로직이 지워지지 않은 옛 체크포인트를
되살려 초기화 의도와 달리 `collected_ids` 를 다시 채울 수 있기 때문이다.

CSV는 `utf-8-sig`(Excel 호환), JSON은 `utf-8`/indent=2. 카테고리명의 `/`·`\` 등
Windows 금지문자는 `_`로 치환된다. CSV 셀 값이 `=`, `+`, `-`, `@`, LF(`\n`),
CR/TAB 등으로 시작하면 Excel 수식 주입(CSV Injection)을 막기 위해 앞에 `'`를
붙여 강제로 텍스트 처리한다(스크래핑한 판매자 정보는 신뢰할 수 없는 외부
입력이므로 필수 방어). `collected_ids.json`/`fastcrawl_state.json`/체크포인트는
문법 오류뿐 아니라 **의미적 손상**(예: 배열이어야 할 자리에 객체가 온 경우)도
검증하여 격리한다 — 크래시 대신 안전하게 빈 상태로 복구한다. 체크포인트는
최상위가 배열인지뿐 아니라 **각 원소가 dict 이고 유효한 문자열 goodscode 를
갖는지**도 검증한다 — 예전에는 `[1]` 처럼 원소 타입이 잘못된 경우를 통과시켜,
승격 시 JSON은 쓰고 CSV 작성 중에야 실패해 재시도할 때마다 쓰레기 파일이
쌓였다.

JSON/CSV 저장은 둘 다 임시 파일에 완전히 쓴 뒤 원자적으로 교체한다
(`Storage._write_records`). JSON 교체는 성공했는데 CSV 교체만 실패하는 좁은
창(6차 리뷰 MEDIUM)에도 대비해, 그 경우 이미 옮겨진 JSON 을 최선을 다해
되돌려(삭제) "JSON 만 있고 CSV 는 없는" 반쪽짜리 결과 쌍이 남지 않게 한다.

## 참고

- `ceo_name`(대표자명)은 `mg.gmarket.co.kr`에 없어 항상 빈 문자열 (WORK_ORDER §15)
- 리스팅은 카테고리당 최대 ~200개만 노출 (전체 상품 아님)
- 동시 요청 금지 (순차 1건씩), 요청 간격 0.3~1.0초 권장
- Pre-scan 결과 상태는 4가지: **수집 가능** / **완료됨** / **상품 없음** /
  **차단/오류**(주황) — 마지막은 네트워크 오류·Cloudflare 차단·봇 감지로 리스팅
  자체를 읽지 못한 경우이며, 진짜로 상품이 0개인 "상품 없음"과 구분된다.
- 수집 중 예상치 못한 오류가 발생하면 "수집 완료"가 아니라 **"수집 실패"** 로
  명확히 표시되며, 그 시점까지의 부분 결과는 저장된다.
- 창을 닫을 때 사전 조사/수집이 진행 중이면 확인 후 취소를 요청하고, 워커가
  실제로 완전히 종료된 뒤에만 창이 닫힌다(고정 타임아웃이나 폴링이 아니라
  워커의 `finished` 시그널을 워커 생성 시점에 미리 연결해두고 기다린다 —
  확인 대화상자가 열려 있는 동안 워커가 먼저 끝나도 시그널을 놓치지 않는다).
  이 확인 대화상자(`QMessageBox`)는 모달이지만 Qt 의 중첩 이벤트 루프는 그
  동안에도 다른 시그널을 계속 처리한다 — 그래서 대화상자를 띄우기 *전에* 먼저
  `_close_prompt_active` 가드를 켜서, '이어서 수집'의 자동 수집 시작 같은
  동작이 그 틈에 새 워커를 만들지 못하도록 막는다. 사용자가 "아니오"를
  선택하면 다시 풀린다.
- PyInstaller one-file 빌드에서도 출력 경로가 임시 번들 디렉터리가 아니라
  실제 실행 파일이 위치한 디렉터리(`sys.executable` 기준)를 가리킨다.
- 기존 CLI 엔진 `src/fast_crawl.py`는 참고용으로 유지 (본 앱이 대체)

## 패키징 (P3, 선택)

Windows 배포 후보는 `scripts\build_windows.bat`으로 만든다. 이 스크립트는
Python 3.12의 깨끗한 `.venv-win`을 만들고, 고정된 직접 의존성을 설치해 전체
테스트를 통과한 뒤 PyInstaller 6.21.0으로 빌드한다.

```bat
scripts\build_windows.bat
```

`pyinstaller.spec` 은 GUI와 런타임 설치 모드를 포함한 실행 파일 하나를 생성한다:

| 실행 파일 | 용도 | 콘솔 |
|---|---|---|
| `SellerCollector.exe` | 메인 2탭 UI + `--setup-runtime` 설치 + `--verify-runtime` 검증 | 설치 모드만 표시 |

배포 시 단일 EXE와 스크립트가 만든 `SHA256SUMS.txt`를 함께 제공한다. 사용자는
최초 1회 `SellerCollector.exe --setup-runtime`을 실행하여 **두 브라우저
런타임(Coupang용 Camoufox+GeoIP, Gmarket용 patchright Chromium)**을 설치한 뒤
인자 없이 `SellerCollector.exe`를 실행한다.

### 배포 시 유의사항 (2026-07-28 clean-cache 재검증)

최신 Windows 후보의 산출물 hash, Hyper-V 체크포인트, 설치 전/후 preflight,
양 플랫폼 실수집 행·필드 검증과 종료 후 잔류 프로세스 결과는
[`docs/RELEASE_READINESS.md`](docs/RELEASE_READINESS.md)에 기록한다.

- **인터넷 필수**: `SellerCollector.exe --setup-runtime`은 Coupang용 Camoufox(GitHub ~492MB) +
  GeoIP(jsdelivr ~45MB) + Gmarket용 patchright Chromium(Playwright CDN ~150MB) 등
  총 약 1.4GB 를 내려받는다. 사내망/방화벽이 `github.com`·`objects.githubusercontent.com`·
  `cdn.jsdelivr.net`·`raw.githubusercontent.com`·`api.github.com`·`playwright.download.prss.microsoft.com`
  (Playwright 브라우저 CDN) 중 하나라도 막으면 설치가 실패한다.
- **코드 서명**: 2026-07-28 내부 후보의 단일 EXE는
  `CN=SellerCollector Internal Release` 자체서명 인증서로 Authenticode 서명하고
  타임스탬프를 추가했다. 수신 PC에 함께 제공한 공개 인증서를 신뢰 저장소에 설치한
  경우에만 서명이 `Valid`다. 공개 CA 서명이나 SmartScreen 평판은 아니므로 조직
  외부 배포에는 별도의 공개 신뢰 코드 서명이 필요하다. 정확한 인증서와 hash는
  [`docs/RELEASE_READINESS.md`](docs/RELEASE_READINESS.md)를 따른다.
- **동일 사용자로 실행**: 설치 모드와 GUI는 per-user 캐시(`%LOCALAPPDATA%\camoufox`,
  `%LOCALAPPDATA%\ms-playwright`)를 공유하므로 반드시 같은 Windows 사용자 계정으로
  실행한다. 설치기만 "관리자 권한으로 실행"하면 경로가 달라져 앱이 브라우저를 찾지 못한다.
- **방화벽(Gmarket)**: Gmarket 첫 수집 시 patchright Chromium("Google Chrome for
  Testing")에 대한 Windows 방화벽(인바운드) 팝업이 뜰 수 있다. 아웃바운드 수집은
  허용 없이도 정상 동작하므로 팝업은 닫아도 된다.
- **GeoIP 30일 만료**: preflight 가 GeoIP DB 를 30일(`GEOIP_MAX_AGE_DAYS`) 이내로만
  허용한다. 30일이 지나면 앱이 Coupang 탭을 거부하므로
  `SellerCollector.exe --setup-runtime`을 다시 실행해 GeoIP를 갱신해야 한다.

> **VM 실측으로 발견·수정한 배포 블로킹 버그 (2026-07-27)** — 복원한 Windows 11 VM 에
> 배포물을 설치해 두 플랫폼 실수집까지 검증하며 다음을 수정했다(현재 모두 수정·검증 완료):
> 1. **Coupang preflight**: 실제 `camoufox fetch` 는 `config.json` 을 `active_version`
>    만으로 다시 써서(channel/pinned 키 없음) 정상 설치인데도 런타임을 거부 →
>    `app/core/coupang/preflight.py` 에서 channel/pinned 검사 제거(version.json 으로 고정 유지).
> 2. **Gmarket 브라우저 런타임**: frozen 빌드에 `patchright` 모듈 미번들 +
>    stealth Chromium 미설치로 사전조사 즉시 실패 → `pyinstaller.spec` 에 patchright 번들,
>    `requirements.txt` 에 런타임 의존 고정, `SellerCollector.exe --setup-runtime`이
>    patchright Chromium 을 설치하도록 통합.
>
> 소스 수정 반영본으로 재빌드해야 배포 exe 에 적용된다: Windows 에서
> `scripts\build_windows.bat`.

## Coupang CLI 어댑터

```bash
python coupang_crawl/coupang_omp_crawler.py [--max-scroll-pages N] [--output PREFIX]
```

### 종료 코드

| 코드 | 의미 |
|---|---|
| 0 | 성공 (레코드 있음) |
| 1 | 실행 오류 (API/세션/네트워크) |
| 2 | 레코드 없음 (no_items, template_not_captured) |
| 3 | 저장 실패 |
| 130 | 사용자 취소 |

우선순위: 저장실패(3) > 정리실패·cleanup_error(1) > 취소(130) > 레코드없음·no_items/template_not_captured(2) > 오류(1) > 레코드없음·빈 결과(2) > 성공(0)

### 출력 파일

- `{prefix}.json` / `{prefix}.csv` — 최종 결과 (UTF-8 / UTF-8-sig)
- `{prefix}_partial.json` / `{prefix}_partial.csv` — 취소/오류 시 부분 결과
- 같은 prefix의 결과가 이미 있으면 `_2`, `_3` ... 을 붙여 기존 JSON/CSV 쌍을
  덮어쓰지 않는다. 출력 폴더는 프로세스 단위로 잠겨 앱 두 개가 같은 폴더에
  동시에 저장하는 것도 차단한다.
- CSV는 수식 주입 방어(`=`,`+`,`-`,`@` 앞에 `'` 부착)

## 테스트

```bash
python -m unittest discover -s tests -v
```

`tests/`는 표준 라이브러리 `unittest`만 사용하며 `requests`/`bs4`를 스텁으로
대체해 PyQt6·scrapling 없이도 `core` 계층(저장/취소/에러 처리/CSV 인젝션 방지
등)을 검증한다.

`tests/test_main_window.py`는 실제 PyQt6 를 필요로 하는 `main_window.py` UI
통합 테스트(종료 확인 대화상자 경쟁 조건, fail-closed 다이얼로그, 초기화 실패
경고 등)를 담고 있다. PyQt6 가 없는 환경에서는 `unittest.skipUnless` 로 자동
건너뛴다(위 명령이 정상 종료하며 `skipped=N` 으로 표시된다). PyQt6 를 설치한
환경에서는 다음과 같이 실행하면 실제 Qt 이벤트 루프로 검증된다(디스플레이가
없는 CI/서버에서는 `QT_QPA_PLATFORM=offscreen` 사용):

```bash
pip install PyQt6
QT_QPA_PLATFORM=offscreen python -m unittest tests.test_main_window -v
```
