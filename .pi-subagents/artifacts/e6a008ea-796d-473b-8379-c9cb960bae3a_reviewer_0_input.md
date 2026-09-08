# Task for reviewer

[Read from: /home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/plan.md, /home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode/progress.md]

Review the completed Windows release packaging in /home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode. Scope: dist/SellerCollector.exe, dist/SHA256SUMS.txt, the external CSV/JSON in coupang_crawl/output, and build staging at /mnt/c/Users/dltnd/Desktop/SellerCollector_build_20260826. Confirm the evidence supports a usable single Windows EXE containing the latest Coupang category tab while data remains external. Inspect files/commands/artifacts directly. Do not modify project/source files. Report only concrete blockers or fixes worth doing now, plus a pass/fail verdict and residual risks.

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