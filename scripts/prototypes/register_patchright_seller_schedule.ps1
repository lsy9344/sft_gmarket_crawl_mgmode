$ErrorActionPreference = "Stop"

$repo = "\\wsl.localhost\Ubuntu-Restored\home\noah\Desktop\project\dev_busi\sft_gmarket_crawl_mgmode-patchright-canary"
$python = "C:\Users\dltnd\AppData\Local\Programs\Python\Python312\python.exe"
$script = Join-Path $repo "scripts\prototypes\coupang_patchright_full_sellers.py"
$outputDir = "C:\Users\dltnd\Desktop\PatchrightFruit"
$stateRoot = Join-Path $env:LOCALAPPDATA "SellerCollectorPatchrightSeller"
$profileRoot = Join-Path $env:LOCALAPPDATA "SellerCollectorPatchrightCanary"
$userId = "${env:USERDOMAIN}\${env:USERNAME}"
$taskName = "PatchrightFruitSeller-Every2Hours"
$startAt = (Get-Date).AddHours(2).AddMinutes(5)

Get-ScheduledTask | Where-Object TaskName -Like "PatchrightFruitSeller-*" |
    Unregister-ScheduledTask -Confirm:$false

$principal = New-ScheduledTaskPrincipal `
    -UserId $userId `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 60) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$arguments = '"{0}" --limit 600 --output-dir "{1}" --state-root "{2}" --profile-root "{3}"' -f `
    $script, $outputDir, $stateRoot, $profileRoot
$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument $arguments `
    -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At $startAt `
    -RepetitionInterval (New-TimeSpan -Hours 2 -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 4)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Patchright fruit seller dataset every two hours, at most 600 products" `
    -Force | Out-Null

$task = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
[pscustomobject]@{
    TaskName = $task.TaskName
    State = $task.State.ToString()
    NextRunTime = $info.NextRunTime.ToString("yyyy-MM-dd HH:mm:ss")
    RepetitionInterval = $task.Triggers.Repetition.Interval
    RepetitionDuration = $task.Triggers.Repetition.Duration
} | ConvertTo-Json
