# Task for reviewer

PyQt6 데스크톱 앱(판매자 정보 수집기)에 새로 추가된 Foodspring(식봄) 크롤링 탭을 독립 리뷰하세요. 버그, 실행 불가 문제, 엣지케이스, 회귀 위험을 찾는 것이 목적입니다. cwd: /home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode

최근 추가/수정된 파일:
- app/core/foodspring/engine.py (GraphQL API 크롤링 엔진, Scrapling 세션, 셀러 사업자정보 수집, ThreadPoolExecutor 병렬)
- app/core/foodspring/exporter.py (openpyxl Excel 원자적 저장)
- app/core/foodspring/preflight.py (런타임/네트워크 확인)
- app/core/foodspring/outcome.py (종료 판정)
- app/models/foodspring_records.py (dataclass)
- app/workers/foodspring_worker.py (QThread 브리지)
- app/ui/foodspring_panel.py (탭 UI)
- app/ui/main_window.py (3번째 탭 통합 - on_foodspring_start/pause/resume/cancel/open_result, _active_worker/_active_control, closeEvent, 크로스탭 busy)
- foodspring_crawl/foodspring_seller_crawler.py (기존 독립 CLI 크롤러)

검토 관점:
1. 엔진 run()의 취소/일시정지(Control) 처리 — ThreadPoolExecutor 배치, checkpoint 호출 위치
2. 세션 쿠키 만료·네트워크 실패 시 동작 (연속 실패 시 조기 종료 로직 포함)
3. 응답 파싱 실패, vendor 정보 누락 셀러 처리
4. exporter의 엑셀 저장 실패 경로와 outcome 매핑
5. main_window 통합: 시그널 연결, 상태 전이, 크로스탭 잠금(어느 탭 실행 중 다른 탭 비활성화), 닫기 시 안전 종료, exception 경로에서 UI 상태 복구
6. Python 스레드 안전성 (requests.Session 동시 사용, 카운터 동시 증가)
7. 데이터 무결성: 중복 제거, 상품-셀러 매핑, _product_count 산식

테스트 파일: tests/test_foodspring.py, tests/test_coupang_panel.py(탭 개수 검증 변경)

각 후보 문제에 대해: 심각도(치명/중요/경미), 발생 시나리오, 수정 제안을 명시하세요. 코드만 읽고 판단하며, 실행은 하지 마세요. 최종적으로 해결해야 할 상위 3개 문제를 우선순위로 정리해 주세요.

## Acceptance Contract
Acceptance level: attested
Completion is not accepted from prose alone. End with a structured acceptance report.

Criteria:
- criterion-1: Return concrete findings with file paths and severity when applicable

Required evidence: review-findings, residual-risks

Finish with a fenced JSON block tagged `acceptance-report` in this shape:
Use empty arrays when no items apply; array fields contain strings unless object entries are shown.
`criteriaSatisfied[].status` must be exactly one of: satisfied, not-satisfied, not-applicable.
`commandsRun[].result` must be exactly one of: passed, failed, not-run.
`manualNotes` and `notes` are optional strings; an empty string means no note and does not satisfy `manual-notes` evidence.
```acceptance-report
{
  "criteriaSatisfied": [
    {
      "id": "criterion-1",
      "status": "satisfied",
      "evidence": "specific proof"
    }
  ],
  "changedFiles": [
    "src/file.ts"
  ],
  "testsAddedOrUpdated": [
    "test/file.test.ts"
  ],
  "commandsRun": [
    {
      "command": "command",
      "result": "passed",
      "summary": "short result"
    }
  ],
  "validationOutput": [
    "validation output or concise summary"
  ],
  "residualRisks": [
    "none"
  ],
  "noStagedFiles": true,
  "diffSummary": "short description of the diff",
  "reviewFindings": [
    "blocker: file.ts:12 - issue found, or no blockers"
  ],
  "manualNotes": "anything else the parent should know"
}
```