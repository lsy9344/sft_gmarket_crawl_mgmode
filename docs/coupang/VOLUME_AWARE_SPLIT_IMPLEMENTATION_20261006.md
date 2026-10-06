# 볼륨 인지 작업 분할 구현 기록 (2026-10-06)

- 상위 설계: `PARALLEL_VOLUME_AWARE_SPLIT_DESIGN_20261006.md`
- 구현 범위: §4(물량 조달 1·3순위) + §5 전부(가중 packing·조각 분할·
  혼합 큐·plan_version) + §6(UI) + §8(테스트) + §9 Phase 1·2 동시 적용
- 미구현(의도적): §4 2순위 '목록 1페이지 probe 폴백' — productCount 노드별
  부착 검증이 라이브로 안 됐고 설계 §10 도 폴백 우선순위를 미결정으로
  남겼다. productCount 유무의 2단 폴백(있으면 가중/없으면 라운드로빈)이
  열화 호환을 이미 보장하므로 probe는 검증 결과에 따라 추가한다.

## 변경 요약

| 모듈 | 변경 |
|---|---|
| `app/core/coupang/categories.py` | `CategoryNode.product_count` — category-list 응답의 `productCount` 를 파싱·캐시 보존. 옛 캐시(키 없음)는 0(미지)으로 읽힘 |
| `app/core/coupang/work_plan.py` (신규) | 작업 단위 스키마(`whole`/`pages`/`sellers`)·LPT bin-packing·페이지 조각 분할·crc32 판매자 슬라이스·직렬화 |
| `patchright_top_thousand.py` | 목록 조각 — 상태에 `page_range` 새겨 구간만 훑음(완주 판정 상한 = 구간 종료 페이지) |
| `patchright_full_sellers.py` | `seller_work_queue` 가 다중 상품 소스 + 해시 슬라이스 필터 지원 |
| `patchright_top_sellers.py` | `run_top_seller_batch` 로 조각 파라미터 전달 |
| `parallel_pipeline.py` | `choose_action` 이 단위 종류별로 판정 — §5.3 "단계 판단의 큐 배출 규칙 이관" |
| `parallel_manager.py` | 작업 단위 목록·배출 규칙(멱등 스윕)·`plan_version` 상태 영속·구버전 상태 이전 규약 재개 |
| `app/ui/coupang_parallel_panel.py` | 분할 모드 미리보기(총 물량·1단위 예상 물량·조각 분할 카테고리 수)·"작업 단위 수로 축소" 문구 |
| `split_family_into_shards` | 구현을 `work_plan.round_robin_shards` 로 이관(폴백과 같은 함수 공유, 시그니처 불변) |

## 계획 알고리즘 (§5 구현판)

물량을 알 때: S = 2N(스틸링 여유), G = 총물량/S. 물량 > 2G 인 카테고리는
페이지 조각(예상 페이지 = 물량/60, 조각 수 = 물량/G, 연속·서로소 구간),
나머지는 남은 예산(S − 조각 수)만큼의 bin 에 LPT packing. 설계 §5.1 의
`S = min(카테고리 수, 2N)` 은 whole 유닛에만 적용되는 phase-1 규칙이므로
물량 인지 경로에서는 카테고리 수 상한을 쓰지 않는다 — 그래야 §1 의
"카테고리 수 < 인스턴스 수여도 전 회선 활성"(§3.2 분할 천장 제거)이
성립한다. 물량 미지면 종전 라운드로빈 min(카테고리 수, 2N) 그대로.

판매자 조각 K: `clamp(ceil(상품 행 수 / seller_limit), 1, 인스턴스 수)`.
설계 §10 권장("인스턴스 수와 동일")에서 세션 1개 분량보다 작은 조각은
만들지 않게 변형 — 빈 조각은 즉시 완주하지만 폴더·큐가 부풀고 라운드
추적이 지저분해진다. 빈 조각도 브라우저 없이 즉시 완주하므로 안전.

## 배출 규칙 (§5.3)

완주 판정의 사실 근원은 디스크(조각 폴더의 `top_state.json`)다 — 실행 중
배출과 재시작 복원 배출이 같은 판정을 내고 멱등하다(이미 그 카테고리의
판매자 조각이 있으면 재배출 없음). 카테고리의 목록 전 조각 완료 → 상품
행 수 집계 → K개 판매자 조각을 대기 큐 끝에 추가 → 다음 도래한 아무
인스턴스나 승계. 복원 직후에도 스윕을 1회 돌아 죽기 직전 완주를 놓치지
않는다.

## 상태 호환 (§5.5)

`coupang_parallel_state.json` 에 `plan_version`(2)와 `units`(단위 사양,
배출된 판매자 조각 포함)이 추가됐다. 구버전 상태(version 1, plan_version
없음)를 새 계획으로 시작한 폴더에서 만나면 `root_family`(분할 모드 원본
가족)를 라운드로빈 재분할해 이전 규약(통짜 샤드)으로 재개한다 — 라운드로빈은
결정적이므로 같은 카테고리·샤드 수면 항상 같은 분할이 나온다. root_family
없이는 재구성 불가 → 새 배분 폴백(종전 동작). v1 상태 파일 형식 자체는
그대로라 구버전 코드도 읽을 수 있다.

## 검증 (§8)

- 단위(`tests/test_coupang_work_plan.py`): LPT 균등성·결정성(라운드로빈
  대비 편차 축소 대조 포함), 페이지 조각 연속·서로소·커버, 슬라이스
  분할·안정성(프로세스 재시작 이어하기), K 휴리스틱, 단위 직렬화 왕복.
- 통합(`tests/test_coupang_parallel_pieces.py`): 배출 규칙(미완료 미배출·
  완료 즉시 배출·멱등), 목록 조각 세션(범위 준수·끊긴 조각 이어하기),
  판매자 슬라이스 서로소 매핑 + 루트 병합, 빈 조각 무브라우저 완주,
  v2 상태 왕복, 구버전 상태 이전 규약 재개.
- 내구성(`tests/test_coupang_parallel_endurance_7.py`): 기존 7인스턴트
  시나리오를 파라미터화해 20인스턴스 × 30가족 변형 추가(세션 사망 주입·
  중간 재시작·공정성·무결성 동일 단정).
- 패널(`tests/test_coupang_parallel_shard.py`): 물량 미리보기 문구,
  unit_specs/root_family 전달, 단위 수 > 인스턴스 수 비축소, "작업 단위"
  축소 문구.
- 카테고리(`tests/test_coupang_categories.py`): productCount 파싱·직렬화·
  캐시 왕복·옛 캐시 호환·형식 오류 거부.

## 라이브 검증 순서 (§8-4, 코드와 별도 운영 작업)

8 → 14 → 20 단계 상한(스핀 값), 각 단계에서 차단 0·활성 회선 수 기록.
채소 7회선 실행(10-07 새벽 완주 예상)은 방해하지 않는다.
