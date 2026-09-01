# Patchright Coupang canary — PROTOTYPE

이 코드는 **Patchright가 Camoufox와 다른 첫 진입 결과를 내는지**만 확인하는
버려도 되는 시험판이다. 본 프로그램의 Coupang 엔진에는 연결하지 않았다.

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
