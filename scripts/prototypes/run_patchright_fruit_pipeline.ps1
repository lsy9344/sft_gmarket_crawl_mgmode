param(
    [string]$TaskName = "PatchrightFruitPipeline-90Min"
)

$ErrorActionPreference = "Stop"

$repo = "\\wsl.localhost\Ubuntu-Restored\home\noah\Desktop\project\dev_busi\sft_gmarket_crawl_mgmode-patchright-canary"
$python = "C:\Users\dltnd\AppData\Local\Programs\Python\Python312\python.exe"
$script = Join-Path $repo "scripts\prototypes\coupang_patchright_fruit_pipeline.py"
$outputDir = "C:\Users\dltnd\Desktop\PatchrightFruit"
$stateRoot = Join-Path $env:LOCALAPPDATA "SellerCollectorPatchrightCanary"
$profileRoot = $stateRoot
$trialTaskName = "PatchrightFruitPipeline-61Min-5Runs"
$continuationTaskName = "PatchrightFruitPipeline-61Min-Continuation"

& $python $script `
    --seller-limit 130 `
    --output-dir $outputDir `
    --state-root $stateRoot `
    --profile-root $profileRoot
$code = $LASTEXITCODE

if ($code -eq 30) {
    @($TaskName, $continuationTaskName) | Select-Object -Unique | ForEach-Object {
        Disable-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue | Out-Null
    }
    exit 0
}
if ($code -ne 0) {
    @($TaskName, $continuationTaskName) | Select-Object -Unique | ForEach-Object {
        Disable-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue | Out-Null
    }
    exit $code
}

if ($TaskName -eq $trialTaskName) {
    $trialStartedAt = [datetime]"2026-09-07T17:32:00"
    $trialRuns = @(
        Get-Content (Join-Path $outputDir "fruit_pipeline_runs.jsonl") |
            ForEach-Object { $_ | ConvertFrom-Json } |
            Where-Object { [datetime]$_.started_at -ge $trialStartedAt }
    )
    $successfulTrialRuns = @(
        $trialRuns |
            Where-Object {
                $_.event -eq "pipeline_slot_finished" -and
                $_.exit_code -eq 0
            }
    )
    if ($trialRuns.Count -eq 5 -and $successfulTrialRuns.Count -eq 5) {
        Enable-ScheduledTask `
            -TaskName $continuationTaskName `
            -ErrorAction Stop | Out-Null
    }
}
exit 0
