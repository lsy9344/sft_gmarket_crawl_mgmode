# Patchright 쿠팡 실험 보고서

작성일: 2026-09-01  
상태: **상품·판매자 분리 전량 구조 구현 및 사과/배 하위 카테고리 완료**

## 1. 결론

Patchright와 Windows에 설치된 실제 Chrome 조합은 이 개발 PC에서 쿠팡 홈,
카테고리 목록, 상점 세션 페이지에 처음 진입하는 데 성공했다. Patchright 적용 뒤
완료한 실접속 14회에서는 화면에 보이는 `Access Denied`, CAPTCHA, HTTP
403/418/429가 없었다.

다만 이것은 "탐지되지 않았다"거나 "과일 전량 수집이 검증됐다"는 뜻이 아니다.
확인한 범위는 과일 루트 첫 페이지의 상품 24개와 사과/배 1~19페이지의 위치
1,015까지다. 사과/배 18·19페이지가 연속으로 비어 종료 조건을 만족했다. 전량 상품
파일에는 중복 제거 뒤 고유 상품 833개가 있다.
사과/배 첫 8개는 기존 판매자 한 명으로 연결됐지만, 다음 12개에서 새 판매자 2명의
완전한 사업자정보를 저장했다. 차단보다 목록 앞부분에 같은 판매자가 반복되어 새
판매자 정보가 적게 나오는 현상은 계속 관찰됐다.

그래서 다음 단계는 한 페이지의 요청 수를 크게 올리는 방식이 아니다. 과일의 12개
하위 카테고리를 차례로 방문하고, 모든 카테고리에서 같은 판매자는 한 번만 조회한다.
한 실행은 상품 8개로 시작하고 정상 종료 뒤부터 12개로 늘린다. 화면에 보이는 차단
신호가 하나라도 생기면 공통 안전장치가 이후 실접속을 잠근다.

## 2. 시작 배경

기존 프로그램에서 `쿠팡 로그인`을 누르면 다음과 같은 Akamai/Edgesuite 오류가
반복됐다.

- `Reference #18.9da52b17.1788223878.33797fb2`
- `Reference #18.b23a6f3d.1788224454.65e40fbc`

같은 PC의 일반 브라우저에서는 쿠팡에 접속할 수 있었고, 약 12~24시간을 기다린 뒤
Windows에서 다시 실행해도 프로그램 경로는 같은 오류를 보였다. 따라서 Linux
아이콘 하나만의 문제로 볼 수 없었다. 현재 소스의 로그인 주소와 쿠팡 주소는 HTTPS를
사용하므로, 오류 화면에 `http://www.coupang.com/`이 보인 사실만으로 현재 코드의
HTTP 주소 오류라고 단정할 근거도 없었다.

이 관찰은 IP 전체가 항상 막힌 상황이라기보다 브라우저 종류, 자동화 연결 방식,
프로필 또는 세션 상태가 결과에 함께 영향을 주었을 가능성을 보여준다. 이번 실험만으로
그중 하나를 원인으로 분리하지는 못했다.

## 3. Patchright와 Camoufox의 차이

아주 간단히 비유하면 다음과 같다.

- **Patchright**: Chrome은 그대로 두고, 자동화 도구가 Chrome에 말을 거는 방법을
  고친다.
- **Camoufox**: Firefox 브라우저 자체를 고쳐서 브라우저가 보여주는 여러 특징을
  바꾼다.

| 항목 | Patchright | Camoufox |
|---|---|---|
| 기반 | Playwright를 수정한 드라이버/프레임워크 | Firefox를 수정한 별도 브라우저 |
| 이번 실험의 브라우저 | Windows에 설치된 Google Chrome | Camoufox가 제공하는 Firefox |
| 프로젝트가 설명하는 핵심 | 자동화 명령과 신호 누출을 줄이는 패치 | 브라우저 내부 수준의 지문 주입·변경 |
| 지원 범위 | 프로젝트 문서상 Chromium 중심 | Firefox와 자체 Playwright/Juggler 연결 |
| 주의점 | 프로젝트가 무탐지를 보증하지 않음 | 프로젝트도 개발 중이며 성능·지문 불일치를 경고 |

두 도구는 같은 이름만 바꾼 제품이 아니라 손보는 장소가 다르다. 그래서 Camoufox가
막힌 조건에서 Patchright + 실제 Chrome의 첫 진입 결과가 달라지는지 작은 독립
시험을 해볼 가치가 있었다. 프로젝트 문서의 "탐지 저항" 설명은 각 프로젝트의
주장이며, 이번 결과가 그 주장을 일반적으로 증명하지는 않는다.

공식 프로젝트:

- Patchright: https://github.com/Kaliiiiiiiiii-Vinyzu/patchright
- Camoufox: https://github.com/daijro/camoufox

## 4. 실험 범위와 안전 규칙

기존 Camoufox 수집 엔진은 바꾸지 않았다. Patchright 코드는 별도 워크트리와
`experiment/coupang-patchright-canary` 브랜치에 시험판으로 추가했다.

공통 규칙은 다음과 같다.

1. Windows에 설치된 Google Chrome을 화면이 보이는 상태로 실행한다.
2. 같은 Chrome 프로필과 안전 기록을 계속 사용한다.
3. 실접속 사이에 최소 30분을 강제한다.
4. 실패 요청을 자동으로 재시도하지 않는다.
5. `Access Denied`, Edgesuite 참조번호, CAPTCHA, HTTP 403/418/429 또는 한국어
   접근 제한 문구를 확인한다.
6. 차단 신호가 한 번 보이면 기록을 잠그고 이후 실접속을 거절한다.
7. 공개 목록과 공개 판매자 사업자정보만 읽는다. 로그인 자동화, 프록시, 지문 조작,
   차단 숨김 기능은 넣지 않는다.
8. 중간 결과와 위치 기록은 덮어쓰지 않거나 임시 파일에서 안전하게 교체한다.

"차단 없음"은 위 신호가 관찰되지 않았다는 뜻이다. 서버 내부 판단을 볼 수 없으므로
"감지 안 됨"을 뜻하지 않는다.

## 5. 구현과 변경 이력

| 커밋 | 내용 |
|---|---|
| `336c071` | Patchright 오프라인/실접속 canary 추가 |
| `999b202` | Windows 앱에 canary 버튼 연결 |
| `5e3110f` | Windows 빌드 실행 파일 검증 보완 |
| `817e7fd` | 앱 canary 결과 기록 |
| `1fe9d2b` | 판매자 최대 3건 표본 수집 추가 |
| `88386c3` | 일반 카테고리 3건 성공 기록 |
| `9cec53c` | 과일 루트 첫 묶음 기록 |
| `675b76b` | 상품 위치 저장과 이어하기 추가 |
| `be3aca6` | 과일 두 번째 묶음 기록 |
| `f171061` | 한 묶음 상한을 8개로 증가 |
| `007010b` | 앞선 묶음에서 본 판매자 재요청 방지 |
| `bc3e664` | 한 페이지 안전 확인 범위를 24개로 증가 |
| `fdb0453` | 과일 네 번째 묶음 기록 |
| `c57c285` | 한 Chrome에서 연속 3페이지를 저장하는 기능 추가 |

이번 후속 변경은 다음을 추가한다.

- 과일 하위 카테고리 12개를 고정된 순서로 처리한다.
- 카테고리별 1~50페이지, 페이지별 최대 60개 상품을 확인한다.
- 모든 카테고리·페이지의 판매자 기록을 합쳐 전역 중복을 제거한다.
- 같은 상품 링크가 화면에 여러 번 보여도 `vendorItemId` 기준 한 번만 센다.
- 첫 묶음은 8개, 정상 완료 뒤 다음 묶음부터 12개로 늘린다.
- 빈 페이지가 2번 연속이면 해당 하위 카테고리를 완료한다.
- `patchright_fruit_job.json`에 현재 카테고리, 페이지, 수량과 누계를 저장한다.
- 한 명령은 네트워크 묶음 하나만 실행한다. 중단 뒤 같은 명령을 다시 실행하면
  저장 위치부터 이어간다.

## 6. 실접속 결과

| 시각(KST) | 대상 | 처리 결과 | 직접 문서 이동 / 명시 API | 차단 |
|---|---|---|---|---|
| 11:25:08 | Windows Chrome canary | 홈 200, 카테고리 200, 상품 링크 5개 | 2 / 0 | 없음 |
| 11:55:12 | 빌드된 앱 canary | 홈 200, 카테고리 200, 상품 링크 5개 | 2 / 0 | 없음 |
| 12:25:18 | 일반 카테고리 표본 | 상품 3 → 판매자 3 → 사업자정보 3 | 3 / 4 | 없음 |
| 12:55:40 | 과일 루트, 위치 0 | 상품 3 → 판매자 2 → 저장 1 | 3 / 3 | 없음 |
| 13:25:46 | 과일 루트, 위치 3 | 새 상품 5 → 판매자 1 → 저장 1 | 3 / 2 | 없음 |
| 13:55:56 | 과일 루트, 위치 8 | 새 상품 8 → 판매자 2 → 저장 1 | 3 / 3 | 없음 |
| 15:17:10 | 과일 루트, 위치 16 | 새 상품 8 → 판매자 2 → 저장 0 | 3 / 2 | 없음 |
| 15:47:56 | 사과/배 1페이지, 위치 0 | 새 상품 8 → 기존 판매자 1 → 저장 0 | 3 / 1 | 없음 |
| 17:00:38 | 사과/배 1페이지, 위치 8 | 새 상품 12 → 판매자 3(기존 1) → 저장 2 | 3 / 3 | 없음 |
| 17:30:58 | 전량 목록 단계, 사과/배 위치 20 | 새 상품 24, 첫 44개 상품 파일 저장 | 2 / 0 | 없음 |
| 18:03:09 | 전량 목록 단계, 사과/배 위치 44 | 위치 60 완료, 2페이지 시작으로 이동 | 2 / 0 | 없음 |
| 18:40:18 | 전량 목록 단계, 사과/배 2~4페이지 | 60 + 57 + 60 = 177개, 5페이지 시작 | 4 / 0 | 없음 |
| 19:10:52 | 전량 목록 단계, 사과/배 5~14페이지 | 화면 598개 → 새 상품 561개, 15페이지 시작 | 11 / 0 | 없음 |
| 20:04:55 | 전량 목록 단계, 사과/배 15~19페이지 | 화면 180개 → 새 상품 34개, 연속 빈 페이지 2회로 완료 | 6 / 0 | 없음 |

마지막 묶음에서는 이미 본 판매자 1명을 사업자정보 요청 전에 제외했다. 새 판매자
1명은 표준 공개 사업자정보에 이름이 없어 결과 파일을 만들지 않았다. 재시도는 없었고
각 실행 뒤 Patchright 전용 Chrome이 닫힌 것을 확인했다.

### 누계

- 처리한 과일 상품 위치: 1,039개(루트 24개 + 사과/배 위치 1,015)
- 전량 상품 파일의 고유 상품: 833개
- 완료한 하위 카테고리: 1/12
- 과일에서 확인한 고유 판매자 ID: 4개
- 만들어진 JSON/CSV 결과 쌍: 4쌍
- 저장 행: 5행
- 저장 행의 실제 고유 판매자: 3명
- Patchright 적용 뒤 관찰된 차단/CAPTCHA: 0건

즉, 상품 수를 늘렸지만 판매자 수가 같은 비율로 늘지 않았다. 과일 하위 카테고리를
나누고 전역 판매자 중복을 없애는 이유다.

## 7. 저장 위치와 검증

Windows 결과 폴더:

```text
C:\Users\dltnd\Desktop\PatchrightFruit
```

주요 파일:

- `patchright_progress_<카테고리>[_page_<페이지>].json`: 상품과 판매자 위치
- `patchright_fruit_job.json`: 과일 전체 작업 위치와 누계
- `patchright_sample_*.json`, `patchright_sample_*.csv`: 새 판매자 결과
- `SHA256SUMS.txt`: 보관 파일 해시 목록
- `fruit_products.csv`: 전량 단계의 고유 상품 목록
- `fruit_sellers.csv`: 전량 단계의 판매자 처리 상태와 사업자정보
- `fruit_product_seller.csv`: 상품과 판매자 연결
- `fruit_collection_state.json`: 전량 단계의 정확한 재개 위치
- `fruit_failed_sellers.json`, `fruit_collection_summary.json`: 실패 대기와 검증 요약

네 번째 묶음 뒤 기존 과일 루트 위치 기록의 SHA-256은 다음과 같다.

```text
23af5e097aaa007f9bd3dacaae3f18b7a8e2693117d1615017d998b51a09193e
```

사과/배 첫 8개 묶음 뒤 작업 파일과 분류 위치 파일의 SHA-256은 다음과 같다.

```text
92af0d749c9306afe911a0e15e2575f4221fe5a43039bc39dacfe08c5fe382f7  patchright_fruit_job.json
f5fde33193f34619bb89e7bde005012bee855b4e962e010fe7dd5552bafc5749  patchright_progress_194284.json
```

사과/배 다음 12개 묶음 뒤 새 결과와 현재 위치 파일의 SHA-256은 다음과 같다.

```text
da423b8b2ca0afc6b74ba4e70f9c8c699b47b888f0d2e2b3d6d9c575ac9b35ed  patchright_sample_20260901_170051.json
691297443e669c445447b666927d565bc2d04cb822c3e867adab31ecf27c5770  patchright_sample_20260901_170051.csv
a3b452dbf7c55bc412ba48adfff4237798c5e984fad02b1bc007150d1ce52623  patchright_fruit_job.json
fa54915bb36f044c5a1ec02fc6df80f38921540098b86422836d18dc4ecec507  patchright_progress_194284.json
```

전량 상품 목록 24개 확대 뒤 새 파일의 주요 SHA-256은 다음과 같다.

```text
6f9eae2ff6f2d9738f8677c1572b9e44d47c2b26bb0e0683f388e4453a90a3ab  fruit_products.csv
919d01db09bd52af23c7959801522095f16c2f8d30ed615105eb8a6cc4806e57  fruit_collection_summary.json
f503ef2e7b7bd5c20d61e6c5f24041c062483e2d7210ecc6d1265eac1c9f843a  fruit_collection_state.json
```

사과/배 1페이지 위치 60 완료 뒤 현재 SHA-256은 다음과 같다.

```text
aba2293fe9679912461f5b76459aea86810706948984e2dc9c37b9109d02dd0f  fruit_products.csv
85ee4d14d724dbd3bc9ad3d08db117766b603b0d8d78eca35dba407f2e536547  fruit_collection_summary.json
69e62cbbb5c586d414fcf7a053b84e123978b1b98c7bcab22d1d2b3aabee5774  fruit_collection_state.json
```

사과/배 2~4페이지 연속 처리 뒤 현재 SHA-256은 다음과 같다.

```text
adb5832ee91b2edc0b6b054621a142e52dfbf3a400bcf0ad2b72a35e0370fb84  fruit_products.csv
4220ff86873f06a0d6228e570ed8f8209e2221b9633b437e110ec2be583909c9  fruit_collection_summary.json
59077e8661412d97bfaafe2db4677a25202e08892a4373b4141128f7daae1dcb  fruit_collection_state.json
```

사과/배 5~14페이지 연속 처리 뒤 현재 SHA-256은 다음과 같다.

```text
8f994b15ba904199dca631cfa10070c6f38532a08a1b61f23441e3ef13b14985  fruit_products.csv
1e6dc20aca70a565c3d136114a27b452522abe1db51fd5a1e64eabda59cae772  fruit_collection_summary.json
39b4a136e6b3a8b79935450bd5095388ba3eaa2029e6fa59051cd96de58a4c7c  fruit_collection_state.json
```

사과/배 카테고리 종료 확인 뒤 현재 SHA-256은 다음과 같다.

```text
08fd62a649006fb9c69de625fefbb0dd366b7ed1751dbc0af77e7f05aa8ae509  fruit_products.csv
b665823b35c6b4ec68f0a2615a532bd24f6cc5a9eec874afeaf2702861158204  fruit_collection_summary.json
2df83395dfd63ea1bafbb29ff9969bf4995427a892ff6dbf6a71f3d5d73d8c79  fruit_collection_state.json
```

백업:

```text
C:\Users\dltnd\Desktop\PatchrightFruit_backup_20260901_1517_batch4
C:\Users\dltnd\AppData\Local\SellerCollectorPatchrightCanary_backup_20260901_1517_fruit_batch4
C:\Users\dltnd\Desktop\PatchrightFruit_backup_20260901_1548_leaf1
C:\Users\dltnd\AppData\Local\SellerCollectorPatchrightCanary_backup_20260901_1548_leaf1
C:\Users\dltnd\Desktop\PatchrightFruit_backup_20260901_1701_batch12
C:\Users\dltnd\AppData\Local\SellerCollectorPatchrightCanary_backup_20260901_1701_batch12
C:\Users\dltnd\Desktop\PatchrightFruit_backup_20260901_1721_full24_ready
C:\Users\dltnd\AppData\Local\SellerCollectorPatchrightCanary_backup_20260901_1721_full24_ready
C:\Users\dltnd\Desktop\PatchrightFruit_backup_20260901_1731_full24
C:\Users\dltnd\AppData\Local\SellerCollectorPatchrightCanary_backup_20260901_1731_full24
C:\Users\dltnd\Desktop\PatchrightFruit_backup_20260901_1803_full60_page1
C:\Users\dltnd\AppData\Local\SellerCollectorPatchrightCanary_backup_20260901_1803_full60_page1
C:\Users\dltnd\Desktop\PatchrightFruit_backup_20260901_1841_3pages
C:\Users\dltnd\AppData\Local\SellerCollectorPatchrightCanary_backup_20260901_1841_3pages
C:\Users\dltnd\Desktop\PatchrightFruit_backup_20260901_1914_10pages
C:\Users\dltnd\AppData\Local\SellerCollectorPatchrightCanary_backup_20260901_1914_10pages
C:\Users\dltnd\Desktop\PatchrightFruit_backup_20260901_2006_category1
C:\Users\dltnd\AppData\Local\SellerCollectorPatchrightCanary_backup_20260901_2006_category1
```

백업 브랜치:

```text
backup/patchright-fruit-batch4-success-20260901-1517
backup/patchright-full-fruit-24-price-ready-20260901-1723
```

이전 변경까지 Windows 전체 테스트 352개가 통과했다. 과일 순차 작업과 상품 링크
중복 제거를 추가한 뒤 Windows의 실제 네트워크 없는 Patchright 관련 테스트 29개가
통과했고, 오프라인 Chrome canary도 상품 2개를 정상 추출했다. Windows 전체
테스트는 359개 중 350개 통과, 4개 건너뜀, 9개 실패였다. 실패 9개는 이 Windows
Python에 기존 Camoufox GeoIP 보조 패키지 `maxminddb`가 없는 환경 문제이며 이번
Patchright 변경 시험에는 포함되지 않는다.

전량 구조 추가 뒤 Windows의 실제 네트워크 없는 Patchright 관련 테스트 37개가
통과했고 새 전량 모듈의 코드 검사도 통과했다. 24개 실접속 결과는 고유
`vendorItemId` 44개, 기존 첫 20개 순서 일치, 새 24개 중복 0개, 필수 상품 열과
가격 누락 0개로 다시 읽어 검증했다.

위치 60 확대에서는 이전 고유 상품 44개가 모두 보존됐고 고유 상품 17개가 추가돼
파일은 61개가 됐다. 마지막 위치에서 고른 상품은 16개지만, 두 실행 사이 목록
앞부분 상품 1개가 바뀌어 다시 읽은 첫 60개 안에서 새 상품 1개를 추가로 발견했다.
중복은 0개이며 61개 모두 필수 상품 열과 가격이 채워졌다. 이는 목록이 시간에 따라
바뀔 수 있다는 한계의 실제 사례다.

연속 3페이지 단계는 한 Chrome에서 홈 1회와 목록 3페이지를 처리하도록 구현했다.
페이지 사이에는 15초를 기다리고 각 페이지 뒤 결과와 위치를 원자적으로 저장한다.
중간 페이지 차단 때 앞선 페이지 결과가 남고 차단 페이지에서 멈추는 경우까지 포함해
Windows Patchright 관련 테스트 39개와 새 코드 검사가 통과했다.

2026-09-01 18:40:18 KST 연속 3페이지 실접속은 한 Chrome에서 홈을 한 번 연 뒤
사과/배 2페이지 60개, 3페이지 57개, 4페이지 60개를 저장했다. 직접 문서 이동은
4회이고 판매자 API와 재시도는 0회였다. 고유 상품은 238개, CSV 중복과 모든 필수
열·가격 누락은 0개다. 상태와 요약은 모두 5페이지 위치 0의 `running`으로 일치했고,
실행 뒤 전용 Chrome 프로세스는 남지 않았다. 화면에서 확인 가능한 차단 신호도
관찰되지 않았다.

2026-09-01 19:10:52 KST 연속 10페이지 실접속은 사과/배 5~14페이지에서 화면상
상품 598개를 읽고, 이전 상품과 겹치는 37개를 제외한 새 상품 561개를 저장했다.
누적 파일은 고유 상품 799개이고 중복, 필수 열 누락, 가격 누락은 모두 0개다. 직접
문서 이동 11회, 판매자 API와 재시도 0회였으며 모든 페이지가 HTTP 200이었다.
상태와 요약은 15페이지 위치 0으로 일치하고, 화면에서 확인 가능한 차단 신호와 실행
뒤 전용 Chrome 잔여 프로세스는 없었다.

다음 검증용 `--category` 명령은 한 Chrome에서 현재 하위 카테고리의 종료 조건까지만
처리한다. 빈 페이지 2번이면 다음 카테고리 위치를 저장한 뒤 브라우저를 닫고, 50페이지
상한에서도 상품이 계속 나오면 완료가 아닌 `incomplete_limit_reached`로 멈춘다.
실제 네트워크 없는 Windows Patchright 관련 테스트 41개와 코드 검사가 통과했다.

2026-09-01 20:04:55 KST 사과/배 종료 검증은 15~17페이지의 화면상 상품 180개를
읽고 새 상품 34개를 저장했다. 18·19페이지는 연속으로 비어 사과/배를 완료 처리했고,
다음 위치는 귤/한라봉/감귤류 1페이지다. 누적 고유 상품 833개에 중복, 필수 열 누락,
가격 누락은 없다. 모든 페이지는 HTTP 200이고 직접 문서 이동 6회, 판매자 API와
재시도 0회였다. 화면에서 확인 가능한 차단 신호와 전용 Chrome 잔여 프로세스도
없었다.

## 8. 과일 전체 수집 방법

과일 루트 자체는 하위 카테고리 상품과 많이 겹치므로 새 전체 작업에서는 아래 12개
잎 카테고리를 처리한다.

| 순서 | ID | 이름 |
|---:|---|---|
| 1 | 194284 | 사과/배 |
| 2 | 194288 | 귤/한라봉/감귤류 |
| 3 | 194294 | 감/홍시/곶감 |
| 4 | 194300 | 키위/참다래 |
| 5 | 194306 | 토마토/자두/복숭아/포도 |
| 6 | 194315 | 수박/메론/참외 |
| 7 | 194320 | 딸기/블루베리/베리류 |
| 8 | 194326 | 바나나/오렌지/파인애플 |
| 9 | 194331 | 자몽/레몬/라임/석류 |
| 10 | 194337 | 망고/체리/아보카도/기타 |
| 11 | 194358 | 냉동과일/간편과일 |
| 12 | 194368 | 과일선물세트 |

Windows PowerShell에서 워크트리로 이동한 뒤 다음 명령을 한 번 실행한다.

```powershell
py scripts\prototypes\coupang_patchright_fruit.py `
  --output-dir "$env:USERPROFILE\Desktop\PatchrightFruit"
```

한 번 끝날 때마다 같은 명령을 다시 실행하면 이어진다. 30분이 지나지 않았으면
브라우저를 열지 않고 남은 시간을 알려준다. 차단 신호가 보이면 더 진행하지 않는다.

## 9. 한계와 완료 판정

현재 방식의 "전체"는 다음 경계 안의 전체를 뜻한다.

- 로컬 카테고리 트리에 기록된 과일 잎 카테고리 12개
- 카테고리당 최대 50페이지
- 페이지당 화면에서 읽힌 상품 링크 최대 60개
- 실행 시점에 공개 목록에 보이는 상품과 공개 사업자정보가 있는 판매자

목록은 시간, 품절, 광고, 개인화에 따라 바뀔 수 있다. 그러므로 어느 한 시점의 모든
쿠팡 과일 상품을 영구적으로 100% 증명할 수는 없다. 작업 파일의 `status`가
`completed`이고 12개 카테고리가 모두 완료됐을 때 위 경계의 순회가 끝난 것으로
판정한다. 마지막에는 모든 JSON을 판매자 ID로 다시 합쳐 고유 판매자 수, 필수 필드,
파일 해시를 검증해야 한다.

장시간 성공 여부는 아직 검증되지 않았다. 8개, 12개, 상품 목록 24개, 한 페이지
최대 60개, 연속 3페이지와 연속 10페이지 확대는 모두 정상 종료했다. 현재 전량
위치는 귤/한라봉/감귤류 1페이지 시작이고 사과/배는 완료됐다. 다음 단계는 계획서대로
나머지 11개 하위 카테고리의 상품 목록을 순서대로 처리하는 것이다. 마지막 실접속은
2026-09-01 20:04:55 KST이므로 현재 안전장치에서는 다음 실접속이 20:34:55 이후에만
허용된다.
