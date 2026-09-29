# 설정 매뉴얼 원본과 화면 출처

이용자용 파일:

- [PDF 매뉴얼](../../SETTINGS_PLATFORM_GUIDE_KO.pdf)
- [사진 확대가 가능한 HTML 매뉴얼](../../SETTINGS_PLATFORM_GUIDE_KO.html)

2026-09-28 기준. 15쪽, 사용 이미지 14장. HTML에는 사진이 포함되어 파일 하나로 공유할 수 있다.

## 수정과 재생성

`guide-source.html`을 수정한 뒤 프로젝트 루트에서 다음 명령을 실행한다.

```bash
.venv/bin/python scripts/build_platform_setup_manual.py
```

문서 생성에는 Playwright와 Chromium이 필요하다. 생성 스크립트는 이미지 누락과 인쇄 영역 넘침을 검사한다.

현재 앱 화면은 실제 위젯을 가짜 계정 정보로 열어 캡처했다. 다시 찍으려면:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/capture_settings_screenshots.py
QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/capture_settings_screenshots.py --example
```

## 출처와 확인 범위

- 파일별 출처는 `sources.json`에 기록했다.
- `decodo-public-start.png`, `bright-signup.png`: 공식 공개 페이지 직접 캡처.
- 다른 플랫폼 사진: 공식 도움말이 제공하는 화면 예시를 내려받았다. 이미지의 표시나 가격을 수정하지 않았다. 본문에 예시 화면임을 표시했다.
- `settings-*.png`: 현재 프로젝트의 실제 설정 화면. 가짜 정보만 사용했으며 계정 파일 읽기와 외부 계정 호출을 하지 않았다.
- Decodo 대시보드는 보안 확인 화면, Bright Data 대시보드는 로그인 화면까지만 접근 가능했다. 로그인 뒤 실제 계정 화면·최종 결제 창은 직접 캡처하지 않았다.
- 가입, 결제, 실제 토큰 발급 및 유료 연결 검사는 실행하지 않았다. 메뉴·권한·충전 동작은 링크된 공식 문서로 확인했다.
- HTML 이미지 표시, 확대/닫기, 내부 이동, 390px 화면 너비, 인쇄 영역 넘침 없음, PDF 15쪽을 확인했다.
