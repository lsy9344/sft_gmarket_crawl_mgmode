$ErrorActionPreference = "Stop"

$repo = "\\wsl.localhost\Ubuntu-Restored\home\noah\Desktop\project\dev_busi\sft_gmarket_crawl_mgmode-patchright-canary"
$runner = Join-Path $repo "scripts\prototypes\run_patchright_fruit_pipeline.ps1"
$taskName = "PatchrightFruitPipeline-61Min-Continuation"
$userId = "${env:USERDOMAIN}\${env:USERNAME}"
$firstRun = [datetime]"2026-09-07T22:37:00"
$scheduleEnd = [datetime]"2026-09-09T06:40:00"

$triggers = @()
$registeredRunTimes = @()
for ($at = $firstRun; $at -le $scheduleEnd; $at = $at.AddMinutes(61)) {
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
    -Description "61-minute continuation; enabled only after all five trial runs succeed" `
    -Force | Out-Null
Disable-ScheduledTask -TaskName $taskName | Out-Null

$task = Get-ScheduledTask -TaskName $taskName
[pscustomobject]@{
    TaskName = $task.TaskName
    State = $task.State.ToString()
    TriggerCount = @($task.Triggers).Count
    RegisteredRunTimes = $registeredRunTimes
} | ConvertTo-Json
