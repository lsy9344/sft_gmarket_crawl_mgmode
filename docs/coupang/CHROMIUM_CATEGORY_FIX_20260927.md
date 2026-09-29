# 쿠팡 카테고리 접속 실패 조사 및 수정 (2026-09-27)

후속 운영 판단: [현재 엔진 배치·자동 전환·Patchright 워크트리 비교](research/BROWSER_ENGINE_DECISION_20260927.md). 현재 카테고리는 처음부터 Patchright + Chromium을 쓰며, Camoufox를 먼저 시도하는 자동 전환은 없다.

원인 확정(같은 날 심야): [Camoufox 쿠팡 차단 원인 확정](research/CAMOUFOX_BLOCK_ROOT_CAUSE_20260927.md). Access Denied는 Akamai 엣지의 네트워크 계층 판정이며, Camoufox 고정 베이스(152.0.4-beta)가 내는 구형 Firefox ClientHello 코호트가 차단 대상이다. UA·OS 지문·프로필·IP 평판 가설은 실측으로 기각됐다.

## 증상

Windows Sandbox에서 한국 Decodo 회선 확인은 성공했지만 Camoufox의 쿠팡 홈 진입이 Access Denied로 끝났다. 회선 3회 변경도 실패했다. 기존 로그의 'IP 평판 차단'은 원인 확정 자료가 아닌 추정 문구였다.

## 비교 실험

호스트 Windows와 Windows Sandbox의 결과를 구분한다. 로그인이나 결제 동작은 수행하지 않았다.

| 환경 | 조건 | 관찰 |
|---|---|---|
| 호스트 Windows / Edge | 직접 접속 | 홈 화면 정상, 일부 내부 문서는 403 |
| 호스트 Windows / Edge | Decodo | 홈 화면 정상 |
| 호스트 Windows / Edge | Bright Data ISP | HTTP 200이지만 본문은 Access Denied |
| 호스트 Windows / Camoufox | 이전에 실패한 동일 Decodo 세션, Windows UA, 새 프로필 | 홈 403, Access Denied |
| 호스트 Windows / Edge | 위와 같은 Decodo 세션 | 홈 화면 정상, 일부 내부 문서는 403 |
| 호스트 Windows / Patchright Chromium | 위와 같은 Decodo 세션, 실제 SearchCrawler, 카테고리 432481, 2페이지 | 상품 14개, 판매자 정보 14명, JSON 저장 성공 |
| Windows Sandbox / 기존 Camoufox 진단 EXE | 호스트 Camoufox에서 성공했던 동일 세션, 새 프로필 | 홈 403, Access Denied. Windows에서 생성된 UA/platform은 Linux |

Camoufox 0.5.4 / Playwright 1.60.0 / 고정 Camoufox 152.0.4-beta.28, Patchright 1.61.2 / Chromium 149로 확인했다. 호스트 전역 Python의 다른 패키지 버전 결과와 배포 가상환경 결과를 구분했다.

브라우저와 실행 환경에 따른 차이를 확인했으며, IP 하나만으로 실패를 설명할 수 없다. Camoufox의 무작위 OS 지문만을 단일 원인으로 확정하지도 않는다. HTTP 상태 코드만으로 성공을 판단하면 안 된다. 최초 407은 인증 교환 중에도 나타나며 최종 접속 결과를 함께 확인해야 한다.

## 변경

- Decodo 쿠팡 카테고리 실행만 기존 설치 패키지에 포함된 Patchright Chromium을 사용한다.
- 회선별로 새 Chromium 프로필을 만들고, 프록시 목록 수집과 직접 회선 판매자 수집의 프로필을 분리한다.
- 이번 실행이 만든 프로필만 종료 시 정리한다. 기존 Camoufox 프로필, 진행 DB, 결과 파일을 유지한다.
- 카테고리 시작 및 Decodo 저장 검사는 실제 사용하는 Chromium 설치 상태를 확인한다.
- 설정 검사는 연결·설치 확인 결과로 표시하고, 쿠팡 접속은 수집 시작 시 확인한다고 안내한다.
- Access Denied 로그에서 근거 없는 IP 원인 단정을 제거한다.
- 일반 Coupang 탭, 로그인, 카테고리 목록 갱신은 기존 Camoufox 경로를 유지한다.

## 자동 검증

- Linux: 변경 관련 테스트 171개 및 subtest 31개 통과.
- 프로필 분리·재사용·정리, 드라이버 시작 실패, 취소, 이어받기, UI 런타임 검사, 설정 결과 안내를 검증했다.
- Windows 전체 unittest: 702개 중 실패 5개, 오류 7개, 건너뜀 3개. 같은 Windows Python으로 별도 HEAD 소스를 시험해 동일한 12개 실패를 재현했다. 이번 수정으로 새로 생긴 실패는 없다.
- 기존 실패에는 손상된 진행 DB를 열다 실패할 때 연결을 닫지 않는 Windows 파일 잠금 문제가 포함된다. AliExpress 관련 기존 테스트 실패도 있다. 본 수정 범위에서는 변경하지 않았다.
- 이전 코드 비교 로그: `%LOCALAPPDATA%\Temp\seller-head-baseline-ZyCRl9\baseline.log`.
- Windows 변경 관련 테스트: 170개 통과, 플랫폼 조건 1개 건너뜀, subtest 31개 통과.
- Windows PyInstaller 빌드 성공. 기존 `browserforge.fingerprint` 숨은 import 경고는 남아 있으며 실제 패키지 `browserforge.fingerprints`도 포함된다. 런타임 실제 실행으로 추가 확인한다.
- SellerCollector.exe SHA-256: `2936f984539aba91c31b420ca6b1dfbcea4a6d1ff31dbc1f633ac6f1d57d5fe4`
- CoupangRuntimeSetup.exe SHA-256: `6b65dd361a29a673bcaa26c9449f716a11d538c144265c920a0cf01e285bb9b6`
- 이전 실행파일은 호스트 ShipTest의 `SellerCollector_before_chromium_20260927_2212.exe`로 보존했다.

## 증거 위치

호스트 임시 진단 폴더: `%LOCALAPPDATA%\Temp\codex-coupang-debug`

- `paired-results-t301511641.json`: 동일 회선 Camoufox/Edge 비교
- `chromium-collection/summary.json`: 호스트 실제 파이프라인 2페이지 결과
- `sandbox-camoufox-result.txt`: Sandbox 기존 브라우저 실패
- `/tmp/coupang-edge-comparison-20260927-2140/`: Edge 직접/Decodo/Bright Data 비교

쿠팡의 향후 접속 허용까지 보장하는 변경은 아니다. 현재 실패 사례를 재현하고 성공한 실행 경로를 적용한 수정이다.

## 실제 Windows Sandbox 검증

- 새 EXE의 Sandbox SHA-256이 위 빌드 해시와 일치함을 확인했다.
- 22:14:41~22:17:14: 실제 GUI에서 샐러드/닭가슴살(432481), 로켓 제외, 최대 2페이지로 실행했다.
- 상품 14개 → 판매자 14명 → 사업자정보 14명, 오류 0. 앱 로그에서 JSON 저장 및 결과 표 14명을 확인했다.
- 최대 페이지를 2로 제한했으므로 정상적으로 '일부 수집'으로 안내됐다. 전체 카테고리 완료라는 뜻은 아니다.
- 시험 결과는 Sandbox `Desktop\ShipTestRun\validation_chromium`에 별도로 저장했다.
- 이후 원래 `Desktop\ShipTestRun\output`, 반찬/간편식/대용식(432480), 로켓 제외, 카테고리당 최대 200페이지로 복원했다.
- '완료 1/13개' 이어받기 안내에서 **이어서 수집**을 선택했다. 기존 완료 카테고리를 유지하고 남은 하위 카테고리 수집을 시작했다.
- 재개 후 실제 앱 로그에서도 `이전 완료 결과 사용`, `카테고리 2/13`, Chromium 시작, 1페이지 5개와 2페이지 9개(누적 14개)를 확인했다. 이후 22:33에 아래 매핑 판정 오류로 중단되어 전체 13개 카테고리는 완주하지 못했다.
- 증거: 호스트 진단 폴더의 `sandbox-chromium-success.png`(2페이지 결과), `sandbox-resumed.png`(이어받기 화면), `sandbox-chromium-app.log`(재개 후 GUI 로그).

## 후속 중단 조사: 판매자 중복 제거로 인한 거짓 매핑 누락

### 실제 Sandbox 관찰

- 실행 구간: 22:19:39~22:33:31. 경고창과 GUI 로그를 직접 확인했다.
- 완료된 부모 카테고리 432480의 판매자 15명 결과를 재사용한 뒤, 두 번째 카테고리인 샐러드/닭가슴살(432481)을 진행했다.
- 1~19페이지를 조회했고, 18·19페이지의 빈 목록으로 목록 수집을 끝냈다. 최대 페이지 200에 도달한 중단이 아니다.
- 로켓 제외 상품 97개, 고유 판매자 69명, 사업자정보 저장 69명, 요청 오류 0건이었다.
- 저장 직전에 `매핑 누락 vendorItemId 28건`으로 판정하여 `mapping_pending`으로 끝났다. 카테고리 실행기는 일부 수집 결과를 받으면 멈추므로 남은 하위 카테고리는 진행되지 않았다.
- 화면 합계 84명은 부모 15명 + 이번 69명의 **카테고리별 합계**이며, 전체 카테고리 사이의 중복을 제거한 고유 판매자 수가 아니다.
- 부분 결과 JSON과 진행 기록이 저장됐다. 이번 중단 로그의 직접 원인은 접속 차단이 아닌 매핑 누락 판정이다.

### 코드 원인 및 재현

1. `app/core/coupang/crawler.py`의 `_get_vendors_for_items`가 API 응답을 `vendorId` 키로 묶는다. 같은 판매자의 여러 상품 중 대표 상품 하나만 남는다.
2. `_map_vendors`도 판매자별로 합친 뒤 대표 `vendorItemId`만 진행 DB에 저장한다. 한 요청 안의 중복뿐 아니라 서로 다른 요청 사이의 중복도 상품 연결을 잃는다.
3. `app/core/coupang/search_crawler.py`의 완료 검사는 수집한 **모든 상품**에 대해 진행 DB의 연결 정보를 요구한다. 앞 단계에서 없앤 연결을 실제 응답 누락과 구별할 수 없다.

성공 응답으로 상품 2개를 모두 반환하되 판매자는 같은 사람인 오프라인 재현을 실제 카테고리 파이프라인으로 실행했다. 요청 묶음 크기 1과 10 모두 다음 결과를 냈다.

| 입력 및 결과 | 관찰 |
|---|---|
| API가 정상 반환한 상품 | 2개 |
| 저장된 판매자 정보 | 1명 |
| 진행 DB의 상품 연결 | 1개만 남음 |
| 요청 오류 | 0건 |
| 종료 이유 | `mapping_pending` — 거짓 일부 수집 |

이 오류는 재현으로 확인했다. 실제 실행의 97 - 69 = 28도 같은 구조와 일치한다. 다만 실제 API 응답 원문은 보존되지 않았으므로, 실제 28개 각각이 모두 동일 판매자 중복 때문인지 또는 진짜 응답 누락도 섞였는지는 이 로그만으로 확정하지 않는다. 앞선 2페이지 검증에서는 상품 14개와 판매자 14명이 같아 이 조건이 드러나지 않았다.

### 필요한 수정과 재개 방법

- 모든 응답 상품의 `vendorItemId → 판매자 정보` 연결을 진행 DB에 보존한다.
- 판매자 정보 요청 및 최종 판매자 목록을 만들 때만 `vendorId`로 중복을 제거한다.
- 실제로 API가 반환하지 않은 상품은 지금처럼 미완료로 남긴다. 누락 검사 자체를 끄거나 임의로 완료 처리하지 않는다.
- 같은 판매자의 상품이 한 요청 및 여러 요청에 걸친 경우, 실제 응답 누락, 이전 진행 DB에서 재개하는 경우를 검증한다.
- 수정 버전에서 기존 출력 폴더를 그대로 지정하여 이어받는다. 완료한 부모 카테고리와 저장한 판매자 정보는 재사용하고, 연결이 없는 상품을 확인한 뒤 남은 하위 카테고리를 진행한다.
- 현재 코드로 재시작하면 같은 판정이 반복될 수 있다. IP 교체나 브라우저 교체는 이 오류의 해결책이 아니다.

최초 조사 시점에는 원인 확인과 수정 방법 기록까지 진행했다. 이후 사용자 요청에 따라 아래 수정을 적용했다.

증거: 호스트 진단 폴더의 `sandbox-mapping-warning-20260927.png`, `sandbox-mapping-warning.log`; Linux 임시 재현 폴더 `/tmp/coupang-mapping-diagnosis-20260927/`의 `repro.py`, `result.json`.

### 매핑 오류 수정 및 검증

- `_get_vendors_for_items`가 모든 상품을 문자열 `vendorItemId` 키로 반환하도록 수정했다.
- `_map_vendors`는 기존 연결과 새 상품별 연결을 합쳐 저장한 뒤, 판매자 정보 요청에 넘길 때만 `vendorId`로 중복을 제거한다. 진행 DB의 구조는 바꾸지 않았다.
- `tests/test_coupang_resume.py`에 같은 판매자가 한 배치 및 여러 배치에 걸친 경우, 이전 진행 DB의 누락 연결만 보충하는 경우, 실제 응답 누락을 미완료로 유지하는 경우를 추가했다.
- 실제 파이프라인을 실행하는 최초 재현도 다시 확인했다. 배치 크기 1과 10 모두 상품 연결 2개를 보존하고, 판매자 1명 저장 후 `success` / `finished`로 끝났다. 수정 전 두 경우 모두 연결 1개 / `mapping_pending`이었다.
- 별도 코드 검토에서 호출 호환성·실제 누락 감지·기존 기록 재개에 문제가 발견되지 않았다. 검토자의 독립 검사: 59개 테스트 및 하위 테스트 21개 통과.
- Windows 첫 검사에서 142개 통과, 1개 건너뜀, 2개 실패였다. 실패한 `ListingResumeTest.test_block_mid_listing_resumes_from_boundary`, `test_empty_end_reconfirmed_in_new_session`는 테스트가 DB를 닫지 않아 임시 폴더 정리 때 발생한 WinError 32이며, 이전 HEAD 비교 로그에도 동일하게 기록돼 있다. 기존 손상 DB 테스트 1개는 앞선 검증에서 확인한 문제로 처음부터 제외했다.
- 위 기존 실패 3개를 제외한 Windows 검사: 142개 통과, 하위 테스트 21개 통과, 1개 건너뜀. PyInstaller 빌드도 완료했다. 전체 테스트가 모두 통과했다는 의미는 아니다.
- 매핑 수정 SellerCollector.exe SHA-256: `451fe5dc79557ced7588f9b8ea15743e368a6d97f62a06d8d112afe52f64b212`. 빌드 로그: `/tmp/coupang-mapping-build-20260927-v2/job.log`.
- 호스트 `Desktop\ShipTest\SellerCollector.exe`를 교체했고 이전 파일은 `SellerCollector_before_mapping_20260927_2257.exe`로 보관했다.
- 수정 후 재현 증거: `/tmp/coupang-mapping-diagnosis-20260927/repro-fixed.py`, `result-fixed.json`.

### 수정 버전 Sandbox 재가동 및 실제 복구 확인

- 22:58:11: 기존 앱을 정상 종료한 뒤 새 EXE로 교체하고 다시 실행했다. Sandbox 파일 SHA-256도 위 매핑 수정 빌드와 일치했다.
- 기존 EXE 및 진행 DB 2개를 Sandbox `Desktop\ShipTestRun\mapping_fix_backup_20260927_225805`에 백업했다.
- 원래 출력 폴더 `Desktop\ShipTestRun\output`, 반찬/간편식/대용식(432480), 하위 포함 13개, 로켓배송 제외, 각 카테고리 최대 200페이지로 **이어서 수집**했다.
- 23:02:23: 부모 카테고리는 `이전 완료 결과 사용`으로 건너뛰었고, 샐러드/닭가슴살은 마지막 확인 페이지 19 / 상품 97개 / 확인된 판매자 69명의 기록을 복원했다.
- 23:02:54: 이미 완료된 목록 단계는 재요청하지 않았다.
- 23:03:19~33: 기존 연결 69건을 재사용하고 누락 연결 28건만 재조회했다. 3개 배치가 각각 10→10, 10→10, 8→8건으로 모두 반환됐다.
- 기존 판매자 69명의 사업자정보는 `저장됨 — API 호출 없이 복원`했다. 추가 판매자는 없었다. 이번 실제 28건은 기존 판매자들의 상품 연결이 빠져 있던 경우임을 복구 실행으로 확인했다.
- 23:03:35: **샐러드/닭가슴살 정상 완료 — 상품 97개, 판매자 69명**. 일반 결과 JSON으로 저장됐고 `mapping_pending` 경고 없이 다음 카테고리로 진행했다.
- 세 번째 카테고리 간편과일/후레쉬컷(432506)에서 새 회선 확인과 홈 접속이 성공했고, 23:04:59에 1페이지 상품 12개 수집, 23:05:19에 2페이지 로드를 확인했다. 앱은 수집 중으로 남겨뒀다. 전체 13개 카테고리 완료까지 확인한 것은 아니다.
- 증거: 호스트 진단 폴더의 `mapping-fix-deployment.json`, `mapping-fix-resumed.log`, `mapping-fix-category-complete.png`, `mapping-fix-third-category.png`.
