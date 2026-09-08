# Task for reviewer

[Read from: /home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/plan.md, /home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/progress.md]

Independently validate release quality and user-facing usability for /home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/dist/SellerCollector.exe and coupang_crawl/output/coupang_category_헬스_건강식품_20260824_153133.{csv,json}. Check Windows PE/single-EXE claim, hash/signature evidence where available, row/schema consistency, likely runtime/setup behavior, and whether any packaging omissions would stop normal use. Do not edit files and do not run live crawling. Return concise evidence-backed findings with severity and final verdict.

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