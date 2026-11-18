# 쿠팡 판매자 사업자정보 출처 변경과 복구 (2026-11-18)

## 증상

- 2026-11-16 첫 조회부터 고객 실행 3,266건이 전부 `no_public_info`로 저장됐다.
- 상품·판매자 연결은 정상 동작했고 스토어명도 채워졌지만, 상호·대표자·
  사업자번호·연락처·이메일·주소가 비어 있었다.
- 각 샤드는 판매자 0명인데도 `coupang_판매자_0명.csv`를 만들고 완료로
  표시됐다. 한 번 `no_public_info`로 기록된 판매자는 재실행해도 다시
  조회하지 않았다.

## 확인된 원인

`https://shop.coupang.com/api/v1/store/getStoreReview`의 HTTP 200 응답에서
`name`, `repPersonName`, `businessNumber`, `repPhoneNum`, `repEmail`,
`repAddr1`, `repAddr2`, `eCommerceReportNumber` 8개 키가 빠졌다. 같은
판매자·같은 URL의 과거 응답에는 `name`이 있었고, `storeId`/`urlName` 인자를
맞춰도 응답은 바이트 단위로 같았다(storeId 누락 가설 기각).

대신 상품 상세 페이지(`/vp/products/...`)의 화면 "판매자 정보" 표와
Next.js RSC 데이터 `sellerDetailInfo`에 같은 정보가 남아 있다. 2026-11-18
직결 회선 실측에서 상호/대표자, 사업장 소재지, e-mail, 연락처,
통신판매업 신고번호, 사업자번호가 모두 표시됐다.

키 대응:

| 기존 getStoreReview | 상세 페이지 |
|---|---|
| `name` | `vendorName` (표에서는 "상호/대표자") |
| `repPersonName` | `repPersonName` |
| `repPhoneNum` | `repPhoneNum` |
| `repEmail` | `repEmail` |
| `repAddr1`/`repAddr2` | `repAddress1`/`repAddress2` |
| `businessNumber` | `bizNum` |
| `eCommerceReportNumber` | "통신판매업 신고번호" |

## 코드 변경

- `app/core/coupang/seller_detail.py` (신규)
  - 상품 상세 페이지 DOM의 "판매자 정보" 표(화면 6행 → 상호/대표자 분리로
    7개 레코드 필드)를 읽어 기존 응답 모양으로 변환.
  - 페이지에 영역이 있는데 값이 비면 `no_public_info`, 페이지를 열지
    못했거나 영역 자체가 없으면 `failed`로 구분한다.
  - `getStoreReview`는 배지·평점과 빈 사업자 필드를 보태는 보조 요청으로만
    쓴다. 보조 API가 차단되면 그 세션에서는 더 부르지 않는다.
- `app/core/coupang/patchright_top_sellers.py`
  - 판매자 수집을 API 단일 호출에서 상세 페이지 방문으로 교체.
  - `no_public_info`/`failed` 행에 출력 폴더당 1회 재확인 표시를 붙인다
    (`seller_detail_recovery.json` 표시). `Failed to fetch`로 멈춘
    halted 컨트롤도 이때 한 번 푼다. 차단·HTTP 503·터널 장애는 기존
    안전 장치가 관리하므로 건드리지 않는다.
  - 판매자 정보 추출이 20회 연속 실패하거나 50회 연속 비면 세션을 멈춘다.
  - 한 건도 저장하지 못한 채 끝나면 완료 대신 halted로 남긴다.
- `app/core/coupang/patchright_full_sellers.py`
  - 과일·냉장 전체 수집 판매자 단계도 같은 상세 페이지 경로와 중단
    가드를 쓴다.
- `app/core/coupang/parallel_pipeline.py`
  - 다음 작업을 고를 때 위 복구를 먼저 실행해, 완료된 샤드도 재조회
    대기열을 다시 계산한다.
- `app/core/coupang/patchright_canary.py`
  - 판매자 상세 세션에서 `image`·`media`·`font` 요청을 차단하는
    `block_heavy_resources()`를 추가했다. 판매자 정보 표는 HTML로
    렌더링되므로 이 리소스 없이 읽을 수 있고, Decodo 종량제 사용량을
    크게 줄인다. route를 지원하지 않는 가짜 컨텍스트에서는 아무 것도
    하지 않는다.
- `app/core/coupang/patchright_sample.py`
  - `FETCH_STORE_REVIEW_JS`는 보조 요청으로 남아 있고, 병렬 판매자
    단계는 더 이상 이 응답의 `name` 유무로 판정을 확정하지 않는다.

## 복구 절차

1. 수정본으로 같은 출력 폴더를 다시 연다.
2. 각 샤드의 `top_sellers.csv`에서 `no_public_info`/`failed` 행에
   "상세 페이지 재확인 대기(2026-11 이전 판정)" 표시가 1회 붙고, 남아 있는
   `top_product_seller.csv` 연결을 통해 상세 페이지 재조회 대기열에 오른다.
   행을 지우지 않으므로 기존 스토어명·평점 같은 메타데이터가 보존된다.
   표시한 vendor_id 목록은 `seller_detail_recovery.json`에 남는다. 완료(done)로
   굳은 인스턴스와 다른 가족으로 넘어가 배정이 끊긴 샤드도 재시작 때 이
   표시를 보고 다시 대기열에 들어간다. 새 코드로 처음 실행하는 폴더도 이
   표시를 남기므로, 새 코드가 상세 페이지에서 확인한 정상
   `no_public_info`를 다음 실행에서 다시 표시하지 않는다.
3. 세션이 끝나면 샤드별 `coupang_판매자_N명.csv`와
   `final_dataset_all.csv`가 다시 만들어진다.
4. 전 샤드가 정상 완주하면 루트에서 병합해 최종
   `final_dataset_all.csv`와 `coupang_판매자_N명.csv`를 만든다. 차단으로
   남은 샤드가 하나라도 있으면 루트 병합은 안전하게 보류된다 — 그 샤드의
   안전 장치를 통과한 뒤 다시 실행해야 한다.

차단(`blocked`)으로 멈춘 샤드는 자동으로 풀지 않는다. 안전 장치의 1시간
잠금과 복구 조건을 통과한 뒤 다시 실행해야 한다. Decodo 사용량 소진으로
멈춘 인스턴스도 대시보드에서 사용량을 충전해야 한다.

## 검증

- 단위 테스트: 상세 페이지 라벨→필드 변환, `saved`/`no_public_info`/`failed`
  구분, 차단 보고, 레거시 행 1회 재조회, 0명 완료 방지, 완료 샤드 재가동.
- 실제 Chromium에 합성 "판매자 정보" 표를 넣어 추출 JS가 화면 6개 항목을
  모두 읽는 것을 확인했다(네트워크 없음).
- 실제 Chromium + 로컬 HTTP 페이지로 `image` 요청이 차단되고 문서·CSS는
  정상 로드되는 것을 확인했다(Decodo 절감 경로).
- 2026-11-18 10:58 직결 회선 실측은 홈 페이지에서 HTTP 403을 받아 즉시
  중단했다. 상품 페이지 요청은 보내지 않았고, 코드는 차단을 기록하고
  세션을 멈췄다. 이 환경의 회선 차단이며 상세 페이지 구조 검증은
  2026-11-18 09:47 윈도우 앱 브라우저 실측(§13·14)을 근거로 한다.

## 남은 확인 사항

- 상세 페이지 대량 방문의 차단률·속도는 20명 정도의 소량 실행으로 먼저
  재는 것이 좋다. 판매자 1명당 상세 문서 1개(약 78만 바이트)와 보조 API
  1회를 쓰며, 이미지·폰트·미디어는 차단되어 문서 위주로 내려받는다.
- 로그인 여부에 따라 상세 페이지 판매자 정보가 달라지는지는 확인하지
  않았다. 이 수정은 비로그인 앱 브라우저에서 확인된 공개 화면을 기준으로
  한다.
- 메인 쿠팡 탭의 구형 `app/core/coupang/crawler.py` 경로는 이번 수정
  범위가 아니다. 병렬 수집(샤드·판매자 조각) 경로가 고객 사고 경로다.
