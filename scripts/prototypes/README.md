# Patchright Coupang canary — PROTOTYPE

이 코드는 **Patchright가 Camoufox와 다른 첫 진입 결과를 내는지**만 확인하는
버려도 되는 시험판이다. 앱의 `Coupang 카테고리` 탭에는 같은 시험을 실행하는
`Patchright 접속 시험` 버튼만 연결했다. 기존 로그인·수집 엔진은 바꾸지 않았다.

## 범위

- 기본 실행은 쿠팡을 포함한 외부 페이지로 이동하지 않는다.
- 실제 모드는 홈 1회와 지정 카테고리 1페이지만 연다.
- 화면에 보이는 상품 링크를 최대 10개까지 읽지만 상품 상세는 열지 않는다.
- 자동 반복, 로그인, 프록시, 지문 조작, 상세정보 수집은 없다.
- 실접속 사이에는 최소 30분을 강제한다.
- `Access Denied`, 403, 418, 429 또는 CAPTCHA를 한 번 감지하면 이후 실접속을
  잠근다. 차단 직후 다른 엔진으로 이어서 재시도하지 않는다.

## 실행

먼저 외부 페이지를 열지 않는 점검을 실행한다.

```bash
python scripts/prototypes/coupang_patchright_canary.py
```

정상이라면 마지막 JSON 줄에 `"event": "offline_completed"`와 상품 2개가
표시된다.

앱에서는 `Coupang 카테고리` 탭의 `Patchright 접속 시험`을 누른다. 이 버튼도
아래 명령과 같은 프로필 및 안전 기록을 사용하며, 비교 조건을 고정하기 위해
카테고리 `176573`만 연다. Windows 상태는
`%LOCALAPPDATA%\SellerCollectorPatchrightCanary`에 저장된다.

실제 비교가 허용되고 기존 차단 상태가 완전히 정리된 뒤에만 카테고리 ID 하나를
명시한다.

```bash
python scripts/prototypes/coupang_patchright_canary.py --live-category 176573
```

정상 판정은 `home_status=200`, `category_status=200`, `products`가 1개 이상인
경우다. 한 번 성공한 것은 대량 성공 증거가 아니다. 같은 조건의 여러 독립 실행이
계속 성공해야 다음 단계 검토가 가능하다.

## 이 시험이 답하는 질문

> 실제 Chrome을 쓰는 Patchright가 현재 Camoufox의 첫 화면 차단과 다른 결과를
> 내는가?

이 시험은 장시간 수집, 로그인 계정, 전체 판매자정보 수집 성공 여부를 답하지 않는다.

## 확인된 결과

- 2026-09-01 11:25:08, Windows 실제 Chrome
- 홈 HTTP 200, 카테고리 HTTP 200
- 상품 링크 5개 확인, 차단 없음
- 2026-09-01 11:55:12, 빌드된 `SellerCollector.exe`의 앱 버튼
- 홈 HTTP 200, 카테고리 HTTP 200, 상품 링크 5개 확인, 차단 없음
- 시험이 끝난 뒤 Chrome이 닫히고 앱 버튼이 다시 활성화됨

이 결과는 첫 관문 성공만 뜻한다. 앱 버튼과 CLI를 연달아 누르는 것은 독립 시험이
아니며, 공통 안전장치가 최소 30분 간격을 강제한다.
