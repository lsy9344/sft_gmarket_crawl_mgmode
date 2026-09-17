# AliExpress Crawler - 문서 인덱스 및 아키텍처 가이드

## 1. Project Context

본 문서는 `sft_gmarket_crawl_mgmode` 프로젝트의 확장 일환으로, **알리익스프레스(AliExpress Korea) 입점 판매자 사업자 데이터셋을 쿠팡 카테고리 수집기와 100% 동일한 규격으로 전량 수집하기 위한 기술 개발 히스토리 및 운영 가이드**입니다.

- **대상 플랫폼**: AliExpress Korea (`ko.aliexpress.com`)
- **수집 대상 카테고리**: 모든 카테고리 → 식품과식료품 > 야채
- **비용 원칙**: **0원 (로컬 직접 회선 + 안티디텍트 브라우저 기술 조합, 유료 프록시 불필요)**
- **완전 무인화 원칙**: 최종 사용자의 브라우저 개입(캡차 수동 클릭 등) 없는 **100% 자율 Headless 프로세스**

---

## 2. Document Index

| 문서명 | 주요 내용 | 버전 |
| :--- | :--- | :--- |
| **[`README.md`](file:///home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/docs/aliexpress/README.md)** | 알리익스프레스 수집기 개요, 문서 인덱스, 핵심 원칙 및 산출물 규격 | v1.0 |
| **[`CRAWL_HISTORY_AND_POC.md`](file:///home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/docs/aliexpress/CRAWL_HISTORY_AND_POC.md)** | 초기 PoC 시도 과정, 기술 스택 검토, 10건 실측 검증, 레이트 리밋 발생 원인과 해결책, 완전 무인 파이프라인 아키텍처 | v1.0 |
| **[`DATA_FIELDS_MAPPING.md`](file:///home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/docs/aliexpress/DATA_FIELDS_MAPPING.md)** | 쿠팡 카테고리 탭(`CoupangRecord`) 대비 알리익스프레스 내부 API(`showedProps`) 1:1 필드 매핑 및 품질 검증 기준 | v1.0 |
| **[`IMPROVEMENT_AND_RELEASE_SPEC.md`](file:///home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/docs/aliexpress/IMPROVEMENT_AND_RELEASE_SPEC.md)** | 쿠팡 카테고리 대비 검토 반영 상세 제품 개선 및 정식 배포 준비 사양서 | v1.0 |

---

## 3. 핵심 발견 및 실측 결론 요약

1. **공정위 전자상거래 7대 필수 항목 100% 노출 확인**
   - 겉면 웹 화면(DOM)에는 기본 정보만 노출되나, 백그라운드 호출 엔드포인트인 `mtop.aliexpress.pdp.pc.query`의 `PRODUCT_PROP_PC.showedProps` 객체 내에 **대표자명, 사업자등록번호, 이메일 주소, 고객센터 전화번호, 회사 상호, 사업장 주소, 통신판매업신고번호**가 정형 텍스트로 보관되어 있음을 실측 규명했습니다.
   - 10건 정밀 수집 테스트 결과: **이메일 주소 10/10 (100%), 사업자등록번호 10/10 (100%), 대표자명 10/10 (100%)** 확보.

2. **비용 0원 수집 가능 (유료 프록시 불필요)**
   - 쿠팡과 마찬가지로, 한국 주거용/사무실 직접 회선에서 안티디텍트 브라우저 엔진(Camoufox/Patchright)을 적용하고 홈 화면 웜업을 거치면 유료 프록시 과금 없이 리스팅 및 상세 접근이 가능합니다.

3. **완전 무인화(Headless) 핵심 성공 방정식**
   - **영속 프로필(Persistent Profile)**: 일회용 세션 대신 디스크에 캐시/쿠키를 보존하여 플랫폼 신뢰도 축적.
   - **인간적 딜레이(2.5~3.5초)**: 1초 미만의 고빈도 기계적 호출 금지.
   - **스마트 판매자 중복 제거**: 1,300여 개 상품을 전부 방문하지 않고 고유 판매자 단위로 대표 상품을 방문하여 실 조회 수를 95% 이상 압축.
