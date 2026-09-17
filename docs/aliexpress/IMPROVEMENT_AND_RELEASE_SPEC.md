# AliExpress 카테고리 탭 제품 개선 및 배포 준비 사양서

- **작성일**: 2026-09-17 KST
- **문서 상태**: 검토 완료 및 개선 사양 확정
- **관련 기능**: [`AliexpressCategoryPanel`](file:///home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/app/ui/aliexpress_category_panel.py), [`AliexpressCategoryCrawler`](file:///home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/app/core/aliexpress_category_crawler.py), [`MainWindow`](file:///home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/app/ui/main_window.py)
- **참조 히스토리**:
  - 알리익스프레스 히스토리: [`CRAWL_HISTORY_AND_POC.md`](file:///home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/docs/aliexpress/CRAWL_HISTORY_AND_POC.md)
  - 쿠팡 카테고리 재개 규격: [`WORK_ORDER_CATEGORY_RESUME_20260915.md`](file:///home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/docs/coupang/WORK_ORDER_CATEGORY_RESUME_20260915.md)
  - 프로젝트 배포 가이드: [`RELEASE_READINESS.md`](file:///home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/docs/RELEASE_READINESS.md)

---

## 1. 개요 및 개선 목적

본 문서는 최근 개발된 **'Ali 카테고리' 탭**을 선행 검증된 **'Coupang 카테고리' 탭**의 기술 및 제품 운영 히스토리와 비교 대조하여 발견된 문제점을 해결하고, 최종 사용자에게 안전하고 신뢰도 높은 기능을 제공하기 위한 **상세 제품 개선 및 배포 준비 사양**을 정의합니다.

### 1.1 핵심 해결 과제
1. **배포 환경 리소스 누락 해소**: 최종 배포 실행 파일(EXE)에서 카테고리 트리가 비어버리는 치명적 결함을 사전에 방지.
2. **장시간 수집 복원력(이어하기) 확보**: 네트워크 불안정 또는 앱 종료 후 재실행 시 기존 진행 상태를 이어받는 쿠팡형 재개 기능 도입.
3. **프록시 정책 및 사용자 진입 장벽 일원화**: "0원 수집" 문서 안내와 "유료 프록시 필수" 잠금 간의 괴리를 해소하고 명확한 사용자 경험 제공.
4. **정식 릴리스 절차 완비**: 무결성 검증, 코드 서명, 사용자 설명서 갱신을 통한 안정적인 배포 기준 충족.

---

## 2. 세부 제품 개선 사양

### 과제 1. 배포 패키징 및 카테고리 트리 리소스 로딩 안정화 (배포 필수)

#### 제품 목적 및 사용자 가치
사용자가 개발 환경이 아닌 최종 패키징된 실행 파일(`SellerCollector.exe`)을 실행했을 때도, 준비된 대분류/소분류 카테고리 트리가 지연 없이 즉시 노출되어 직관적인 탐색과 수집이 가능해야 합니다.

#### 제품 동작 및 UX 사양
1. **패키징 리소스 등록**:
   - 빌드 정의 파일([`pyinstaller.spec`](file:///home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/pyinstaller.spec))의 번들 데이터 목록에 카테고리 리소스([`aliexpress_category_tree.json`](file:///home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/app/resources/aliexpress_category_tree.json))를 필수로 포함합니다.
2. **이중화된 리소스 탐색**:
   - 패널([`AliexpressCategoryPanel`](file:///home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/app/ui/aliexpress_category_panel.py)) 구동 시, 단일 실행 파일 내부의 번들 경로와 외부 개발 디렉터리 경로를 모두 순차 탐색하여 어떤 환경에서도 파일 누락이 발생하지 않도록 보장합니다.
3. **사용자 예외 대응 UX**:
   - 만일 리소스 파일이 손상되었거나 찾을 수 없는 예외 상황이 발생하더라도, 오류로 멈추지 않고 화면에 "기본 카테고리 트리를 불러올 수 없습니다. '직접 URL 입력' 탭을 이용해 주세요"라는 친절한 안내와 함께 직접 입력 화면으로 자동 전환합니다.

---

### 과제 2. 쿠팡형 중단 지점 저장 및 이어하기(Resume) 도입

#### 제품 목적 및 사용자 가치
수천 건 단위의 대형 카테고리를 수집하는 도중 사용자가 작업을 일시 취소하거나, 인터넷 연결이 불안정하여 중단되더라도, 이미 확보한 데이터와 진행 위치를 보존하여 **불필요한 중복 수집과 시간 낭비 없이 중단된 지점부터 즉시 이어받아 완주**할 수 있는 가치를 제공합니다.

#### 제품 동작 및 UX 사양
1. **진행 상태 보존 (내구성)**:
   - 1단계(목록 탐색)에서 페이지 처리가 완료될 때마다 마지막 확인 페이지와 상품 목록을 디스크에 안전하게 기록합니다.
   - 2단계(상세 수집)에서 판매자 사업자 정보가 확인될 때마다 즉시 저장소에 반영합니다.
2. **작업 재개 흐름**:
   - 사용자가 이전과 동일한 카테고리와 저장 폴더를 선택하고 '수집 시작'을 누르면, 시스템이 미완료 작업 기록을 감지합니다.
   - 화면 및 로그에 다음과 같은 명확한 이어하기 안내를 표시합니다:
     > `[작업 이어하기] 이전 진행 기록 발견: 1~18페이지 완료 (상품 820건 확보 / 판매자 145개사 수집 완료) → 19페이지부터 이어 수집합니다.`
   - 1단계 목록은 이미 완료된 페이지를 건너뛰고 19페이지부터 탐색하며, 2단계 상세 수집 역시 이미 확인된 판매자는 스마트 캐시로 통과하여 미확인 대상만 순차 처리합니다.
3. **'처음부터 다시 수집' 옵션 제공**:
   - 사용자가 기존 데이터를 덮어쓰지 않고 완전히 새로운 시점으로 수집을 시작하고 싶을 때를 대비하여, 확인 대화상자에서 [이어서 수집]과 [처음부터 새로 수집]을 명시적으로 선택할 수 있도록 지원합니다.

---

### 과제 3. 프록시 운영 정책 및 사용자 진입 장벽 개선

#### 제품 목적 및 사용자 가치
사용자가 보유한 환경(유료 프록시 보유 여부)에 맞춰 최적의 수집 방식을 유연하게 선택할 수 있도록 지원하고, 문서와 실제 기능 간의 차이로 인한 혼란을 원천 해소합니다.

#### 제품 동작 및 UX 사양
1. **수집 방식 선택 모드 제공**:
   - **안정적 대량 무인 모드 (권장)**:
     - 대규모 수집(10~30페이지 이상) 시 플랫폼의 차단 및 빈도 제한을 방지하기 위해 25건 단위 자동 회선 순환을 적용합니다. (설정 탭의 Decodo 주거용 프록시 계정 사용)
   - **알뜰 직접 수집 모드 (0원 수집)**:
     - 소규모 수집(1~5페이지 내외)이나 프록시 계정이 없는 사용자를 위해, 인간적 안전 딜레이(3~4초)를 적용하여 사용자의 PC 로컬 회선으로 무료 수집을 진행합니다.
2. **직관적인 안내 메시지**:
   - 설정 탭에 프록시 계정이 등록되어 있지 않은 상태에서 '수집 시작'을 누르면, 단순 차단 팝업 대신 다음과 같은 선택 안내 창을 띄웁니다:
     > "Decodo 프록시 계정이 설정되어 있지 않습니다.<br><br>
     > ① **로컬 회선으로 안전 수집**: 소량 수집에 적합하며 비용이 들지 않습니다.<br>
     > ② **설정 탭으로 이동**: 대량 무인 수집을 위한 프록시 계정을 등록합니다."
3. **제품 상단 배너 문구 현행화**:
   - 상단 안내 배너의 "0원 수집"과 "주거용 회선 기반" 설명을 정리하여, 사용자가 기대하는 비용과 동작 방식을 투명하게 인지할 수 있도록 개선합니다.

---

### 과제 4. 정식 배포 준비 및 무결성 승인 절차 (Release Gate)

#### 제품 목적 및 사용자 가치
프로젝트의 정식 배포 가이드([`RELEASE_READINESS.md`](file:///home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/docs/RELEASE_READINESS.md), [`DEPLOYMENT_APPROVAL.md`](file:///home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/docs/DEPLOYMENT_APPROVAL.md)) 기준을 완벽하게 충족하여, 배포된 바이너리의 보안과 무결성을 보증합니다.

#### 배포 절차 사양
1. **자동 검증 통과**:
   - 기존 쿠팡, 지마켓 관련 회귀 테스트 및 신규 알리익스프레스 패널/워커 테스트 100% 통과 확인.
2. **패키징 및 단일 실행 파일 생성**:
   - 수정된 [`pyinstaller.spec`](file:///home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/pyinstaller.spec)을 적용하여 Windows 클린 빌드 수행.
   - 단일 실행 파일 `SellerCollector.exe` 생성 확인.
3. **무결성 해시(SHA-256) 및 코드 서명 갱신**:
   - 새롭게 빌드된 실행 파일의 SHA-256 해시를 추출하여 `SHA256SUMS.txt` 갱신.
   - 내부 자체 서명 인증서를 통해 서명 검증(`Valid`) 완료.
4. **사용자 설명서 갱신**:
   - 사용자 안내 매뉴얼에 7번째 탭인 'Ali 카테고리'의 주요 화면, 카테고리 트리 선택 방법, 직접 URL 입력 팁, 수집 결과 확인 가이드를 최신화.

---

## 3. 우선순위 및 단계별 실행 로드맵

| 단계 | 구분 | 중점 개선 항목 | 목표 산출물 및 검증 |
| :---: | :--- | :--- | :--- |
| **Phase 1** | **배포 차단 결함 해결** *(최우선)* | • [`pyinstaller.spec`](file:///home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/pyinstaller.spec) 리소스 번들 추가<br>• [`AliexpressCategoryPanel`](file:///home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/app/ui/aliexpress_category_panel.py) 리소스 경로 탐색 보강 | 빌드된 실행 파일 환경에서 카테고리 트리가 100% 정상 로드되는지 검증 |
| **Phase 2** | **사용자 경험 및 진입 장벽 개선** | • 프록시 계정 미등록 시 로컬 회선 안전 수집 모드 지원<br>• 상단 배너 및 안내 대화상자 정비 | 프록시 계정이 없는 일반 환경에서도 경고 차단 없이 정상 수집 완주 확인 |
| **Phase 3** | **중단 지점 이어하기(Resume) 구축** | • 페이지별 탐색 및 판매자별 상세 수집 진행 상태 디스크 보존<br>• 재시작 시 이전 위치 감지 및 이어하기 대화상자 제공 | 수집 중간 강제 중단 후 재시작 시 앞선 페이지를 스킵하고 이어하는지 실측 |
| **Phase 4** | **최종 패키징 및 정식 배포** | • Windows 클린 빌드 및 단일 실행 파일 생성<br>• SHA-256 무결성 해시 추출 및 서명 갱신<br>• 릴리스 문서 승인 기록 반영 | [`DEPLOYMENT_APPROVAL.md`](file:///home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/docs/DEPLOYMENT_APPROVAL.md)에 정식 GO 판정 기록 및 배포본 인도 |

---

## 4. 최종 성공 판정 기준 (Acceptance Criteria)

- **AC-ALI-01 (리소스 정상화)**: 배포용 단일 실행 파일(`SellerCollector.exe`) 실행 시 'Ali 카테고리' 트리가 빈 화면 없이 완벽하게 표시되어야 한다.
- **AC-ALI-02 (유연한 수집)**: 프록시 계정 등록자는 25건 회선 순환으로, 미등록자는 안전 딜레이 모드로 정상적인 수집 흐름이 시작되어야 한다.
- **AC-ALI-03 (복원력)**: 대규모 수집 중 취소하거나 프로그램을 종료한 뒤 다시 동일 카테고리를 실행하면, 이전 진행 상태를 정확히 인지하고 이어서 수집을 완료해야 한다.
- **AC-ALI-04 (품질 무결성)**: 수집된 최종 결과물에서 공정위 7대 필수 항목(이메일, 대표자, 사업자번호 등)이 누락 없이 규격에 맞게 저장되어야 한다.
- **AC-ALI-05 (배포 승인)**: 전체 테스트 100% 통과, SHA-256 해시 갱신, 릴리스 문서 최신화가 완료되어 정식 배포 승인 상태가 되어야 한다.
