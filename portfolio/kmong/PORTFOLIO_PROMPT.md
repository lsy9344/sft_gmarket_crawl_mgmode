# 크몽 포트폴리오 이미지 제작 프롬프트 (재사용용)

2026-08-31 `sft_gmarket_crawl_mgmode` 프로젝트에서 검증한 제작 방법을 문서화한 것.
다른 프로젝트에서도 아래 프롬프트를 그대로 붙여 넣으면 같은 품질의 포트폴리오 이미지 세트가 나온다.

---

## 1. 붙여넣을 프롬프트

```text
ui-ux-pro-max 스킬 사용하세요.

크몽에 '전문가'로 나의 포트폴리오를 올릴겁니다.
이 프로젝트를 어필할 수 있는 포트폴리오 이미지를 만들어주세요.

조건:
- 메인 1장(1:1) + 상세 3장, 총 4장 세트로 만들 것
- 스크린샷을 붙이지 말고, 제품 화면을 HTML로 깔끔하게 재현할 것
- 화면 속 데이터는 전부 가짜 예시(마스킹)로 채울 것 — 실제 고객/사업자 정보 금지
- 결과물은 PNG(2배 해상도)로 저장하고, 수정할 수 있게 HTML 원본과 재생성 스크립트도 남길 것
- 완성되면 [산출물 폴더 경로]에 넣을 것
```

> 마지막 줄의 경로만 프로젝트에 맞게 바꾼다. 기존 포트폴리오 자료(등록 문구, 업로드 가이드)가
> 있으면 "portfolio 폴더의 기존 문구를 참고해"라고 한 줄 추가한다.

---

## 2. AI가 수행해야 하는 절차 (검증된 워크플로)

### 1단계 — 프로젝트 파악
- README·기획 문서를 읽고 **제품이 해결하는 문제, 대상 사용자, 자랑할 수치**를 뽑는다.
- 수치는 실제 문서에 근거한 것만 사용한다 (예: "22개 카테고리", "240개 테스트 통과", "단일 EXE").
  근거 없는 수치·"수익 보장" 같은 과장 문구는 금지.

### 2단계 — 디자인 방향 결정
- `ui-ux-pro-max` 스킬의 디자인 시스템 검색을 실행한다:
  ```bash
  python3 ~/.claude/skills/ui-ux-pro-max/scripts/search.py \
    "<제품 유형> <업종> professional trust" --design-system -p "<프로젝트명>"
  ```
- B2B·자동화·데이터 도구라면 검증된 조합을 그대로 써도 된다:
  - 배경 네이비 `#0F172A`/`#0B1220`, 포인트 블루 `#0369A1`(밝은 강조 `#38BDF8`)
  - 밝은 면 `#F8FAFC`, 카드 `#FFFFFF`, 테두리 `#E2E8F0`, 성공 `#059669`
  - 서체: **Pretendard** (jsdelivr에서 woff2 내려받아 로컬 포함)
    `https://cdn.jsdelivr.net/npm/pretendard@1.3.9/dist/web/variable/woff2/PretendardVariable.woff2`
  - 아이콘: 이모지 금지, 인라인 SVG(lucide 스타일 stroke 2px)만 사용

### 3단계 — 4장 구성 (스토리 순서)
| 순서 | 내용 | 캔버스(CSS px) |
| --- | --- | --- |
| 01 메인 | 어두운 히어로: 배지 + 큰 한 줄 카피 + 서브카피 + 수치 칩 4개 + 제품 목업(하단에 걸쳐서) + 떠 있는 기능 카드 2~3개 | 1200×1200 (1:1) |
| 02 흐름 | "이렇게 진행됩니다" — 단계 카드 3~4개, 각 카드 오른쪽에 작은 UI 조각(표·진행바·버튼·파일칩) | 1200×1300~1400 |
| 03 화면 | 제품 화면 전체를 HTML로 재현 + ①②③④ 번호 마커 + 하단 번호 설명 | 1200×1300~1400 |
| 04 신뢰 | "왜 믿고 맡길 수 있나" — 안전장치/품질 카드 4개 + 어두운 납품 범위 밴드 + 정책 문구 | 1200×1200 |

- 상세 이미지 하단에 정책 준수 문구 고정:
  "실제 수집(작업)은 대상 사이트 정책과 데이터 이용 권한을 확인한 범위에서 진행합니다."

### 4단계 — 렌더링 파이프라인
- HTML 한 장 = 이미지 한 장. `html,body{width:1200px;height:<H>px;overflow:hidden}` 고정.
- 헤드리스 크로미움(플레이라이트 캐시의 chrome 바이너리)으로 스크린샷:
  ```bash
  CHROME=$(ls -d ~/.cache/ms-playwright/chromium-*/chrome-linux/chrome | sort -V | tail -1)
  "$CHROME" --headless=new --no-sandbox --disable-gpu --hide-scrollbars \
    --allow-file-access-from-files --force-device-scale-factor=2 \
    --window-size=1200,$((H+200)) --virtual-time-budget=8000 \
    --screenshot=out.png "file://<html 경로>"
  ffmpeg -y -i out.png -vf "crop=2400:$((H*2)):0:0" final.png
  ```
- **중요 (실제로 겪은 버그)**: 창 높이를 콘텐츠 높이와 똑같이 잡으면 `position:absolute; bottom:0`
  요소가 렌더링되지 않는다. 반드시 **+200px 크게 렌더링한 뒤 ffmpeg으로 잘라낸다.**
- 렌더링 후 각 PNG를 Read 도구로 직접 눈으로 확인하고, 줄바꿈 고아·과한 여백·겹침을 고쳐 재렌더링한다.

### 5단계 — 산출물 정리
```
portfolio/kmong/v2/          # (프로젝트에 맞게 경로 조정)
├── images/  01~04 PNG       # 업로드용 최종본 (2400px 폭)
├── source/  01~04 HTML + fonts/PretendardVariable.woff2
├── render_images.sh          # 재생성 스크립트 (위 파이프라인)
└── README.md                 # 업로드 순서·규격·디자인 메모
```

---

## 3. 크몽 규격·심사 체크리스트 (제출 전 확인)

- [ ] 메인 이미지 1:1, 가로 600px 이상 / 상세 이미지 세로 3000px 이하 (2배 렌더 시 1500px 초과 금지)
- [ ] 실제 사업자 이메일·전화·사업자번호·주소 등 결과 데이터가 보이지 않는다 (전부 가짜+마스킹)
- [ ] 전화번호·이메일·SNS·외부 링크·QR코드를 넣지 않았다
- [ ] 로컬 파일 경로, 인증서, 내부 해시가 보이지 않는다
- [ ] "무단 크롤링", "차단 우회", "수익 보장" 등 정책 위반·오인 표현이 없다
- [ ] 수치는 전부 프로젝트 문서에 근거가 있다
- [ ] 이미지 하단에 정책 준수 범위 문구가 있다

---

## 4. 원본 참고

- 완성 예시: `portfolio/kmong/v2/` (sft_gmarket_crawl_mgmode 저장소)
- 등록 문구·키워드 작성 예시: `portfolio/kmong/PORTFOLIO_CONTENT.md`
- 크몽 등록 절차·공식 규격: `portfolio/kmong/UPLOAD_GUIDE.md`
