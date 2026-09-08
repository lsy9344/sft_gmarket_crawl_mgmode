# 크몽 포트폴리오 업로드 패키지

이 폴더는 `판매자 정보 수집기 v3.0` 한 프로젝트만 소개하는 크몽 포트폴리오 패키지다.

## 바로 사용할 파일

- `kmong_portfolio_upload.zip`: 최종 이미지 4장과 입력 문구·업로드 가이드 묶음
- `PORTFOLIO_CONTENT.md`: 크몽 입력란에 붙여 넣을 제목, 설명, 키워드, 기술, 기간
- `UPLOAD_GUIDE.md`: 2026-07-15 개편 기준 등록 순서와 최종 점검표
- `images/01_main_cover.png`: 메인 이미지, 1200×1200
- `images/02_project_overview.png`: Gmarket 실제 UI, 1200×1400
- `images/03_product_ui.png`: Coupang 실제 UI, 1200×1400
- `images/04_engineering_quality.png`: 핵심 기능 요약, 1200×1000
- `IMAGE_GENERATION_NOTES.md`: 생성 이미지 프롬프트와 합성 방식

## 이미지 재생성

```bash
./portfolio/kmong/render_images.sh
```

`assets/*_original.png`는 저장소의 실제 앱 화면이며, 공개본은 렌더링 과정에서 로컬 경로를 마스킹한다.
