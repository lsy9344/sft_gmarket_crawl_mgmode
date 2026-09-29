# Patchright의 쿠팡 대량 수집 성공 가능성 평가

> **2026-09-27 후속 확인:** 아래는 9월 1일 실측 전 평가다. 이후 실험 워크트리에 성공·403·503 기록이 쌓였고, 9월 27일에는 Sandbox의 Patchright + Chromium으로 상품 14개·판매자 정보 14명 저장을 확인했다. 현재 운영 판단은 [후속 조사](BROWSER_ENGINE_DECISION_20260927.md)를 따른다. 작은 작업의 성공과 장시간 전체 수집 성공률은 구분한다.

- 평가일: 2026-09-01 KST
- 범위: 기술적 성공 가능성과 지속성만 평가
- 조사 중 쿠팡에는 접속하지 않았으며, 차단 우회 설정이나 실행 절차는 다루지 않는다.

## 한 줄 결론

**Patchright는 `Camoufox에서 막힌 첫 화면`을 다시 비교해 볼 후보로는 가치가
있지만, Patchright만 바꿔서 쿠팡 대량 수집이 오래 성공할 가능성은 낮다.**

판단을 나누면 다음과 같다.

| 질문 | 판단 | 이유 |
|---|---|---|
| 첫 홈/로그인 진입이 Camoufox보다 나아질까? | **가능성 있음, 아직 미확인** | Firefox 계열인 Camoufox 대신 실제 Chrome을 쓰고, Playwright의 대표적인 자동화 흔적을 줄인다. |
| 한 PC·한 회선에서 많은 페이지를 오래 수집할까? | **Patchright 단독으로는 낮음** | 요청량, IP 평판, 기기 지문, 이동 흐름, 사람 행동은 해결하지 않는다. |
| 지금 바로 수집 엔진을 교체할 근거가 충분한가? | **아니오** | 쿠팡 대상 공개 실측과 장시간·대량 성공 자료가 없다. |
| 제한된 비교 실험 후보인가? | **예** | 현재 문제의 한 가설인 `Camoufox/Firefox 계열 차이`를 분리하는 데는 쓸 수 있다. 첫 접속 성공은 대량 성공 증거가 아니다. |

## Patchright가 실제로 바꾸는 것

Patchright는 새로운 브라우저가 아니라 Playwright의 **Chromium 제어부를 고친
포크**다. 공식 README와 패치 소스에서 확인되는 핵심 범위는 다음과 같다.

- 페이지가 알아차릴 수 있는 CDP `Runtime.enable` 사용을 피한다.
- 탐지 흔적이 되는 `Console.enable`을 끈다. 그 결과 콘솔 메시지와 페이지 오류
  기능 일부도 작동하지 않는다.
- `--enable-automation` 등 Playwright 기본 실행 플래그를 바꾸고
  `navigator.webdriver` 노출을 줄인다.
- Chromium 계열만 지원하며, 저장소는 실제 Google Chrome의 영속 프로필 사용을
  권한다.

근거: [Patchright README](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright#patches),
[Patchright Python의 실제 Chrome 권장 설정](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python#best-practice---use-chrome-without-fingerprint-injection),
[실제 패치 생성 소스](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright/blob/main/patchright_driver_patch.ts),
[현재 알려진 버그 #30](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright/issues/30).

이 범위는 **브라우저 자동 조작 표시를 줄이는 일**에는 맞는다. 반면 IP, 요청
속도, 계정 이력, 방문 순서 또는 사람 같은 행동을 자동으로 해결하는 제품은 아니다.

## Akamai가 보는 면과 Patchright의 범위

아래 Akamai 기능은 Akamai 제품군의 공식 기능이다. 쿠팡이 이 기능을 전부 어떤
강도로 켰는지는 공개되지 않았으므로, 표는 `가능한 탐지 면`이지 쿠팡 설정에 대한
확정 설명이 아니다.

| 탐지 면 | Akamai 공식 설명 | Patchright의 대응 범위 | 대량 수집 의미 |
|---|---|---|---|
| 자동화 도구·이상한 헤더·브라우저 버전 불일치 | 투명 탐지는 공통 자동화 프레임워크, 헤더 순서, 버전 불일치 등 수십 가지 이상을 점수화한다. | **일부 대응**: Playwright CDP·실행 플래그 흔적을 줄인다. | 첫 관문에는 도움이 될 수 있다. |
| 브라우저·기기 지문 | OS, 글꼴, 화면, 브라우저 속성, 요청 헤더 등을 함께 본다. | **범위 밖**: 유지보수자도 지문 문제는 Patchright의 목표가 아니라고 명시했다. 실제 Chrome의 진짜 값이 일관적일 수는 있지만, 지문 관리 기능은 아니다. | 서버·가상 환경·많은 세션에서 같은 모양이 반복되면 별도 위험이 남는다. |
| TLS/HTTP와 화면 속 정보의 일치 | 프로토콜 정보와 JavaScript가 본 브라우저 정보를 서로 맞춰 본다. | **직접 패치 주장 없음**: Chrome 자체 네트워크 스택을 쓰는 장점은 있지만, 중간 네트워크와 전체 일관성까지 보장하지 않는다. | 환경이 달라지면 결과도 달라질 수 있다. |
| IP·네트워크 평판, 요청량, 속도 제한 | Akamai는 IP 평판, 초당 요청 수, 속도 제한을 탐지 수단으로 설명한다. | **대응 없음** | 양이 커질수록 Patchright의 장점과 별개로 다시 막힐 수 있다. |
| 마우스·키 입력·방문 흐름·세션 행동 | 행동 탐지는 사람의 움직임과 상호작용을 보고, 제품은 페이지 사용과 이동 흐름도 분석한다. | **대응 없음** | 자동 수집의 반복 흐름은 그대로 보인다. |

근거: [Akamai Detection methods](https://techdocs.akamai.com/cloud-security/docs/detection-methods),
[Akamai Bot Manager](https://www.akamai.com/products/bot-manager),
[Akamai의 탐지 도구 설명](https://www.akamai.com/blog/security/how-bot-management-can-help),
[Akamai Content Protector 제품 설명](https://www.akamai.com/site/en/documents/brief/content-protector-product-brief.pdf).

## `Akamai 통과` 주장을 어떻게 봐야 하나

Patchright README는 `올바른 설정에서 탐지되지 않는다`며 Akamai에 체크 표시를
한다. 또 이슈 #108에서는 사용자가 실수로 일반 Playwright를 가져와 Akamai
거부를 받았고, 유지보수자는 Patchright로 바꿔 같은 Xfinity 사례를 재현하지
못했다고 답했다. 이는 **Playwright의 즉시 탐지 흔적을 줄일 수 있다는 긍정적인
증거**다.

그러나 이 자료에는 쿠팡, 성공률, 요청 수, 실행 기간, 계정 상태가 없다. 따라서
`한 번 들어감`의 근거이지 `쿠팡 대량 수집이 오래 됨`의 근거는 아니다.
저장소 자체의 탐지 스모크 시험도 `webdriver`, Playwright 전역 변수, 오류 스택
같은 로컬 흔적만 확인한다. Akamai나 장시간 트래픽을 재현하는 시험은 아니다.

근거: [Patchright의 Stealth 주장](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright#stealth),
[Akamai 관련 이슈 #108과 유지보수자 재현](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright/issues/108#issuecomment-3136161945),
[Patchright 탐지 스모크 시험 소스](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright/blob/main/utils/detection_smoke_test.mjs).

반대로 대량 지속성에 더 가까운 사례도 있다. 이슈 #96에서 약 50~100회 뒤의
차단이 논의되었고, 유지보수자는 이를 즉시 자동화 탐지와 다른 `soft block` 또는
지문 문제로 보면서 **지문은 Patchright 범위 밖**이라고 정리했다. 이 사례 자체는
쿠팡 실험도, 확정된 Patchright 결함도 아니지만, 프로젝트가 어디까지 책임지는지
명확히 보여 준다. 요청이 쌓인 뒤의 차단은 Patchright가 보장하는 영역이 아니다.

근거: [탐지 이슈 #96의 유지보수자 결론](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright/issues/96#issuecomment-3258001221),
[요청 누적 뒤 차단 이슈 #152의 범위 설명](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright/issues/152#issuecomment-3619760438).

## Camoufox와의 차이

두 도구는 방향이 다르다.

| Camoufox | Patchright |
|---|---|
| 패치된 Firefox이며 여러 기기 지문을 만들고 바꾸는 기능과 사람 같은 마우스 이동을 제공한다. | 실제 Chrome/Chromium을 쓰면서 Playwright 제어 흔적을 줄이는 데 집중한다. 지문 생성·회전은 목표가 아니다. |
| 넓게 위장하지만, 실제 OS·브라우저와 값이 어긋나면 새 위험이 될 수 있다. | 한 실제 PC의 자연스러운 Chrome 값은 더 단순하고 일관적일 수 있다. 대신 많은 독립 사용자처럼 보이게 해 주지는 않는다. |

근거: [Camoufox README의 지문·Playwright·마우스 설명](https://github.com/daijro/camoufox#highlights),
[Patchright 이슈에서 밝힌 지문 비지원 범위](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright/issues/37#issuecomment-2629580776).

프로젝트 내부 기록도 한쪽 엔진이 항상 성공하거나 항상 실패하는 단순한 상황이
아님을 보여 준다. 2026-08-25 Camoufox 작업은 중간 점검 시점까지 2,630페이지와
고유 상품 58,927개를 처리하며 차단/오류 0건이었다. 하지만 2026-08-31부터 서로
다른 두 PC에서 Camoufox 홈 진입 자체가 거부됐다.

근거: [과거 대량 실행 기록](../HANDOFF_20260825.md),
[최근 Access Denied 사고](../LOGIN_ACCESS_DENIED_INCIDENT_20260831.md).

따라서 현재 차단이 `Camoufox라는 이름` 하나 때문이라고 확정할 수 없다. 정책,
브라우저 버전, 지문, 요청 이력 또는 여러 신호의 조합이 바뀌었을 수 있다.

## 유지보수와 운영 위험

- 프로젝트는 현재 활동 중이다. 드라이버 v1.62.1은 2026-08-17 공개됐고,
  2026-08-29에도 코드 변경이 있었다. Python 패키지는 PyPI 기준 1.62.2가
  2026-08-29 올라왔다. 현재 프로젝트 고정값은 `patchright==1.61.2`다.
  근거: [드라이버 v1.62.1](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright/releases/tag/v1.62.1),
  [2026-08-29 커밋](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright/commit/5ee4c1aa178588b46d72d7787783620f30bb758f),
  [PyPI patchright](https://pypi.org/project/patchright/),
  [프로젝트 고정 버전](../../../requirements.txt).
- 이 평가와 함께 한 Windows 오프라인 점검에서는 영속 실제 Chrome 기동이
  통과했다. 이는 `프로그램이 켜진다`는 증거일 뿐, 쿠팡 접속이나 대량 지속성을
  검증한 결과는 아니다.
- README는 Playwright 새 버전에 맞춘 배포가 자동이지만, 상위 코드 변경으로 버그가
  생기고 수정에 며칠 걸릴 수 있다고 경고한다.
  근거: [Patchright Development](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright#development).
- 전체 Playwright 테스트를 모두 통과하지 않는다. 콘솔·페이지 오류 이벤트,
  WebSocket 라우팅, 초기 스크립트 시점 등에 알려진 제한이 있다. 장시간 작업에서
  오류 관찰과 복구가 더 어려워질 수 있다.
  근거: [현재 알려진 버그 #30](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright/issues/30).
- 2026-08-28 열린 이슈 #238은 현재 권장 조합인 실제 Chrome에서도
  `AutomationControlled` 플래그 경고가 생긴다고 보고한다. 유지보수자는 표시를
  숨기는 대안도 탐지될 수 있어 Chrome에서는 깔끔한 해결이 어려울 수 있다고
  답했다. 즉, Patchright 실행은 수동 Chrome과 완전히 같지 않다.
  근거: [이슈 #238](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright/issues/238),
  [유지보수자 답변](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright/issues/238#issuecomment-5462902134).

## 다른 지역 PC 시험의 정보 가치

다른 지역 PC에서는 IP, 운영체제, 기기 지문, 기존 기록이 한꺼번에 바뀐다. 성공해도
`Patchright가 좋아서인지`, `새 IP라서인지`, `새 기기라서인지` 알 수 없다. 또한
한 번 열린 결과는 요청이 쌓인 뒤에도 유지된다는 증거가 아니다.

따라서 **대량 성공만 놓고 봐도 새 지역 PC의 1회 성공은 낮은 품질의 증거**다.
엔진 채택 판단에는 최소한 다음 결과가 필요하다.

1. 첫 홈과 로그인뿐 아니라 실제 대표 수집 흐름이 완료될 것
2. 현실적인 작업량과 여러 날 동안 성공률이 유지될 것
3. 403·429·CAPTCHA·세션 만료·계정 이상을 따로 기록할 것
4. 버전 변경 뒤 같은 기준을 다시 통과할 것
5. 거부가 나오면 다른 엔진으로 연속 재시도하지 않는 중단 장치가 있을 것

이는 우회 방법이 아니라 `첫 화면 성공`을 `대량 성공`으로 잘못 판단하지 않기 위한
검증 기준이다.

## 최종 결정 제안

- **Patchright 전면 전환: 보류**
- **첫 관문 원인 분리용 제한된 비교 후보: 채택 가능**
- **Patchright 단독 대량 수집 해법으로의 기대: 낮춤**

현재 자료로 가장 정확한 표현은 다음과 같다.

> Patchright는 Camoufox와 다른 브라우저 길을 제공하므로 첫 접근 성공률을 바꿀
> 수 있다. 하지만 Akamai가 대량 수집에서 보는 핵심 신호 대부분은 그대로 남는다.
> 그러므로 `시험할 엔진`이지 `대량 성공이 예상되는 해법`은 아니다.
