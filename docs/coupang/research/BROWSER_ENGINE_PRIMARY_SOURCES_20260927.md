# 브라우저 엔진 조사: 공식 출처 요약

- 조사일: 2026-09-27
- 목적: Coupang 수집에 사용할 브라우저 엔진과 Patchright 실험의 의미를 확인

## 확인된 사실

### Patchright

Patchright는 Playwright의 수정판이며, 별도의 브라우저 엔진이 아니다. 공식 Python 저장소는 Playwright와 같은 방식으로 사용할 수 있다고 설명하고 `patchright install chromium` 설치 명령을 제공한다. 또한 Patchright의 수정 대상은 **Chromium 계열뿐**이며 Firefox와 WebKit은 지원하지 않는다고 명시한다.

공식 저장소의 권장 예시는 설치된 Chrome 채널, 영속 프로필, `headless=False`, `no_viewport=True`, 사용자 지정 User-Agent와 헤더를 추가하지 않는 설정이다. README에는 무탐지 주장도 있지만, 쿠팡 대상의 비교 실험이나 성공률 증거로 해석할 수 없다. 또한 Playwright 변경에 따라 Patchright 버그가 생길 수 있다고 설명한다.

출처: [Patchright Python 공식 저장소](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python), [Patchright 드라이버 공식 저장소](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright)

### Playwright와 Chromium

Playwright 공식 문서는 Chromium, Firefox, WebKit과 Chrome·Edge 같은 브랜드 브라우저 채널을 구분한다. 기본 Chromium은 Playwright가 내려받은 오픈소스 Chromium이고, Chrome이나 Edge를 쓰려면 해당 채널을 별도로 선택한다. Playwright 버전마다 호환되는 브라우저 버전이 있으므로 앱에 포함하거나 설치하는 브라우저 revision도 함께 관리해야 한다.

출처: [Playwright 공식 브라우저 문서](https://playwright.dev/docs/browsers), [Playwright 공식 라이브러리 문서](https://playwright.dev/docs/library)

### Camoufox

Camoufox는 Firefox를 기반으로 하는 Playwright 호환 브라우저와 Python 인터페이스다. 공식 문서는 BrowserForge를 사용해 장치 특성을 만들며, 운영체제를 지정하지 않으면 Windows·macOS·Linux 중에서 무작위로 고를 수 있다고 설명한다. Windows 고정이 필요하면 `os="windows"`처럼 지정할 수 있다.

Camoufox 공식 문서는 Firefox와 Chromium의 지문 특성이 서로 다르며 Chromium 지문을 Camoufox에 주입할 수 없다고 설명한다. 따라서 Camoufox와 Chromium은 같은 브라우저의 설정 차이가 아니라, 서로 다른 브라우저 계열을 사용하는 경로다.

출처: [Camoufox Python 사용법](https://camoufox.com/python/usage/), [Camoufox BrowserForge 문서](https://camoufox.com/python/browserforge/), [Camoufox 지문 주입 문서](https://camoufox.com/fingerprint/), [Camoufox 공식 저장소](https://github.com/daijro/camoufox)

## 앱 설계에 적용할 때의 해석

1. 현재 앱은 이미 Patchright로 Chromium을 실행한다. 별도 실험 후보는 공식 권장인 Google Chrome 채널이며, 이것이 현재 앱보다 더 잘되는지는 앱 자체의 비교 실험이 필요하다.
2. Camoufox를 완전히 삭제할 근거는 공식 문서만으로는 없다. 앱의 일반 Coupang 흐름과 로그인·트리 경로는 Camoufox를 유지할 수 있고, 현재 카테고리 수집 경로는 실제 시험 결과에 따라 Patchright Chromium을 사용하도록 분리할 수 있다.
3. “Camoufox 실패 후 Chromium 자동 전환”은 구현 가능한 정책이지만, 현재 앱에는 없고 효과도 검증되지 않았다. 실패 원인을 먼저 구분해야 한다. 인증·잔액·설치·저장 오류를 브라우저 전환 대상으로 취급하지 않는다. 사이트 접근 확인도 HTTP 상태만 보지 않고 정상 본문과 실제 상품·판매자 저장 결과를 함께 확인해야 한다.
4. 공식 문서에는 Coupang 또는 Akamai의 차단을 엔진별로 비교한 성공률 자료가 없다. 그러므로 Patchright가 Camoufox보다 항상 높은 확률로 성공한다고 결론 내릴 수 없다. 이 프로젝트의 결론은 공식 문서가 아니라 이 앱에서 수행한 동일 회선 비교 실험으로 별도 기록해야 한다.

## 조사 범위의 한계

공식 문서는 브라우저 구조·설치·지원 범위를 설명할 뿐, 특정 사이트의 접근 허용이나 회선 품질을 보장하지 않는다. 향후 엔진 변경은 먼저 작은 수집 시험에서 홈 접근, 목록 상품 수, 판매자 정보 저장을 각각 확인하고 결정한다.
