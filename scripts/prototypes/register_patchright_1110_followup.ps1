$ErrorActionPreference = "Stop"

$repo = "\\wsl.localhost\Ubuntu-Restored\home\noah\Desktop\project\dev_busi\sft_gmarket_crawl_mgmode-patchright-canary"
$python = "C:\Users\dltnd\AppData\Local\Programs\Python\Python312\python.exe"
$script = Join-Path $repo "scripts\prototypes\coupang_patchright_full_fruit.py"
$outputDir = "C:\Users\dltnd\Desktop\PatchrightFruit"
$backupDir = "C:\Users\dltnd\Desktop\PatchrightFruit_backup_20260902_1110_pretest"
$guardPath = Join-Path $env:LOCALAPPDATA "SellerCollectorPatchrightCanary\canary_guard.json"
$taskName = "PatchrightFruit-20260902-1110-pages6"
$runAt = [datetime]"2026-09-02T11:10:00"
$userId = "${env:USERDOMAIN}\${env:USERNAME}"

$guard = Get-Content -Raw -Path $guardPath | ConvertFrom-Json
$cutoff = [DateTimeOffset]::new($runAt).ToUnixTimeSeconds() - (24 * 60 * 60)
$recent = @($guard.attempt_history | Where-Object { $_.attempt_ts -gt $cutoff })
$rollingPages = ($recent | Measure-Object pages -Sum).Sum
$rollingItems = ($recent | Measure-Object items -Sum).Sum
$projectedPages = $rollingPages + 6
$projectedItems = $rollingItems + 360
$lastAttempt = [DateTimeOffset]::FromUnixTimeSeconds(
    [long][Math]::Floor($guard.last_attempt_ts)
).LocalDateTime

if ($guard.blocked) {
    throw "Patchright guard is blocked; the follow-up task was not registered."
}
if (($runAt - $lastAttempt).TotalSeconds -lt (2 * 60 * 60)) {
    throw "The follow-up is less than two hours after the last live attempt."
}
if ($guard.daily_sessions -ge 5) {
    throw "The daily session limit is already exhausted."
}
if (($guard.daily_items_reserved + 360) -gt 1500) {
    throw "The daily item limit would be exceeded."
}
if ($projectedPages -gt 25 -or $projectedItems -gt 1500) {
    throw "The rolling 24-hour limit would be exceeded."
}

New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
Get-ChildItem -Path $outputDir -File | Copy-Item -Destination $backupDir -Force
Copy-Item -Path $guardPath -Destination (Join-Path $backupDir "canary_guard.json") -Force

Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue |
    Unregister-ScheduledTask -Confirm:$false

$arguments = '"{0}" --pages 6 --output-dir "{1}"' -f $script, $outputDir
$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument $arguments `
    -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Once -At $runAt
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
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Patchright rolling 24-hour envelope: up to 6 pages" `
    -Force | Out-Null

$task = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
[pscustomobject]@{
    TaskName = $task.TaskName
    State = $task.State.ToString()
    NextRunTime = $info.NextRunTime.ToString("yyyy-MM-dd HH:mm:ss")
    StartWhenAvailable = $task.Settings.StartWhenAvailable
    ProjectedRollingPages = $projectedPages
    ProjectedRollingItems = $projectedItems
    ProjectedDailySessions = $guard.daily_sessions + 1
    ProjectedDailyItems = $guard.daily_items_reserved + 360
    BackupDir = $backupDir
} | ConvertTo-Json
