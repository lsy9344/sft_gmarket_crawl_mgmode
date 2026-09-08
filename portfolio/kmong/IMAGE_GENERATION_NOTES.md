# 이미지 생성 및 합성 기록

## 생성 모드

- 생성 도구: Codex 내장 `image_gen`
- 분류: `ads-marketing`
- 용도: 실제 프로그램 화면 뒤에 사용하는 최소 장식 배경
- 생성 결과: `assets/generated_minimal_ui_bg_v2.png`
- 최종 합성본: `images/01_main_cover.png`

## 사용한 최종 프롬프트

```text
Use case: ads-marketing
Asset type: square background plate for a Korean software portfolio cover
Primary request: Create an extremely minimal premium backdrop that will sit behind a real Windows desktop application screenshot. The real UI will be composited later, so do not invent or draw any interface.
Scene/backdrop: clean soft off-white to pale blue studio gradient with one restrained deep-navy curved field along the lower edge and a very subtle cyan light accent.
Style/medium: polished editorial technology art direction, flat-to-soft dimensional, sophisticated and quiet.
Composition/framing: 1:1 square; large uninterrupted negative space across the top 38%; calm central area for a large real screenshot; minimal visual weight at the edges.
Lighting/mood: bright, trustworthy, precise, modern.
Color palette: #F6F8FA, #EAF2F7, #0A2035, #21CDB1, #5AA7FF.
Text: no text, no letters, no numbers.
Constraints: background only; no windows, no dashboards, no devices, no icons, no logos, no people, no charts, no documents, no watermark.
Avoid: 3D computer mockups, fake UI, neon cyberpunk, decorative clutter, dramatic reflections, stock-photo style.
```

## 합성 방식

- 생성 배경 위에 저장소의 실제 Gmarket 프로그램 화면을 크게 배치했다.
- 상세 이미지 1과 2는 실제 Gmarket/Coupang 화면을 각각 한 장씩 사용했다.
- 상세 문구는 각 이미지당 제목 1개, 설명 1개, 기능 3개 이내로 줄였다.
- Gmarket 화면의 로컬 저장 경로는 `Output folder selected`로 마스킹했다.
- 외부 연락처, 실제 수집 결과, 개인정보, 브랜드 로고는 포함하지 않았다.

