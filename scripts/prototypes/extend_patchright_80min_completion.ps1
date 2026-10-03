$ErrorActionPreference = "Stop"

$taskName = "PatchrightFruitPipeline-80Min"
$finalRun = [datetime]"2026-09-10T12:47:00"
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
$now = Get-Date
$futureStarts = @(
    $task.Triggers |
        ForEach-Object { [datetime]$_.StartBoundary } |
        Where-Object { $_ -gt $now } |
        Sort-Object
)
if ($futureStarts.Count -eq 0) {
    throw "이어 붙일 기존 80분 예약이 없습니다."
}

$lastRun = $futureStarts[-1]
$added = @()
$nextRun = $lastRun.AddMinutes(80)
while ($nextRun -le $finalRun) {
    $added += $nextRun
    $nextRun = $nextRun.AddMinutes(80)
}
if ($added.Count -ne 18) {
    throw "추가 예약 수가 예상과 다릅니다: $($added.Count)"
}

$allStarts = @($futureStarts + $added | Sort-Object -Unique)
$triggers = @(
    $allStarts | ForEach-Object { New-ScheduledTaskTrigger -Once -At $_ }
)
Set-ScheduledTask -TaskName $taskName -Trigger $triggers | Out-Null

$updated = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
$registeredStarts = @(
    $updated.Triggers |
        ForEach-Object { [datetime]$_.StartBoundary } |
        Sort-Object
)
$gaps = @()
for ($index = 1; $index -lt $registeredStarts.Count; $index++) {
    $gaps += ($registeredStarts[$index] - $registeredStarts[$index - 1]).TotalMinutes
}

[pscustomobject]@{
    TaskName = $updated.TaskName
    State = $updated.State.ToString()
    NextRunTime = $info.NextRunTime.ToString("yyyy-MM-dd HH:mm:ss")
    TriggerCount = $registeredStarts.Count
    AddedCount = $added.Count
    IntervalMinutes = @($gaps | Select-Object -Unique)
    FirstRun = $registeredStarts[0].ToString("yyyy-MM-dd HH:mm:ss")
    LastRun = $registeredStarts[-1].ToString("yyyy-MM-dd HH:mm:ss")
    RegisteredRunTimes = @(
        $registeredStarts | ForEach-Object { $_.ToString("yyyy-MM-dd HH:mm:ss") }
    )
} | ConvertTo-Json -Depth 4
