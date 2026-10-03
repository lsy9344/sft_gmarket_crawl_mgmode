param(
    [datetime]$FirstRun = (Get-Date).AddMinutes(85),
    [int]$RunCount = 18
)

$ErrorActionPreference = "Stop"

$repo = "\\wsl.localhost\Ubuntu-Restored\home\noah\Desktop\project\dev_busi\sft_gmarket_crawl_mgmode-patchright-canary"
$runner = Join-Path $repo "scripts\prototypes\run_patchright_top1000.ps1"
$taskName = "PatchrightTop1000-80Min"
$userId = "${env:USERDOMAIN}\${env:USERNAME}"

$triggers = @()
$registeredRunTimes = @()
foreach ($index in 0..($RunCount - 1)) {
    $at = $FirstRun.AddMinutes(80 * $index)
    $triggers += New-ScheduledTaskTrigger -Once -At $at
    $registeredRunTimes += $at.ToString("yyyy-MM-dd HH:mm:ss")
}

Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue |
    Unregister-ScheduledTask -Confirm:$false

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
    -Description "80-minute top-1000 3P category dataset runs; stop on block or error" `
    -Force | Out-Null

$task = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
[pscustomobject]@{
    TaskName = $task.TaskName
    State = $task.State.ToString()
    NextRunTime = $info.NextRunTime.ToString("yyyy-MM-dd HH:mm:ss")
    TriggerCount = @($task.Triggers).Count
    IntervalMinutes = 80
    RunCount = $RunCount
    RegisteredRunTimes = $registeredRunTimes
} | ConvertTo-Json
