$ErrorActionPreference = "Stop"

$repo = "\\wsl.localhost\Ubuntu-Restored\home\noah\Desktop\project\dev_busi\sft_gmarket_crawl_mgmode-patchright-canary"
$runner = Join-Path $repo "scripts\prototypes\run_patchright_fruit_pipeline.ps1"
$taskName = "PatchrightFruitPipeline-80Min"
$userId = "${env:USERDOMAIN}\${env:USERNAME}"
$firstRun = [datetime]"2026-09-08T08:47:00"
$runCount = 22

$triggers = @()
$registeredRunTimes = @()
foreach ($index in 0..($runCount - 1)) {
    $at = $firstRun.AddMinutes(80 * $index)
    $triggers += New-ScheduledTaskTrigger -Once -At $at
    $registeredRunTimes += $at.ToString("yyyy-MM-dd HH:mm:ss")
}

@(
    "PatchrightFruitPipeline-2Hour",
    "PatchrightFruitPipeline-90Min",
    "PatchrightFruitPipeline-61Min-5Runs",
    "PatchrightFruitPipeline-61Min-Continuation",
    $taskName
) | ForEach-Object {
    Get-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue |
        Unregister-ScheduledTask -Confirm:$false
}

$arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -TaskName "{1}"' -f `
    $runner, $taskName
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $arguments `
    -WorkingDirectory $repo
$principal = New-ScheduledTaskPrincipal `
    -UserId $userId `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 60) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $triggers `
    -Principal $principal `
    -Settings $settings `
    -Description "80-minute fruit category and seller-data pipeline; 22 remaining runs; stop on block or error" `
    -Force | Out-Null

$task = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
[pscustomobject]@{
    TaskName = $task.TaskName
    State = $task.State.ToString()
    NextRunTime = $info.NextRunTime.ToString("yyyy-MM-dd HH:mm:ss")
    TriggerCount = @($task.Triggers).Count
    IntervalMinutes = 80
    RunCount = $runCount
    RegisteredRunTimes = $registeredRunTimes
    SellerLimit = 130
    ListingPages = 8
} | ConvertTo-Json
