$ErrorActionPreference = "Stop"

$repo = "\\wsl.localhost\Ubuntu-Restored\home\noah\Desktop\project\dev_busi\sft_gmarket_crawl_mgmode-patchright-canary"
$python = "C:\Users\dltnd\AppData\Local\Programs\Python\Python312\python.exe"
$script = Join-Path $repo "scripts\prototypes\coupang_patchright_full_sellers.py"
$outputDir = "C:\Users\dltnd\Desktop\PatchrightFruit"
$stateRoot = Join-Path $env:LOCALAPPDATA "SellerCollectorPatchrightSeller"
$profileRoot = Join-Path $env:LOCALAPPDATA "SellerCollectorPatchrightCanary"
$taskName = "PatchrightFruitSeller-20260903-0805-Scaled130"
$runAt = [datetime]"2026-09-03T08:05:00"
$userId = "${env:USERDOMAIN}\${env:USERNAME}"

Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue |
    Unregister-ScheduledTask -Confirm:$false

$arguments = '"{0}" --limit 130 --output-dir "{1}" --state-root "{2}" --profile-root "{3}"' -f `
    $script, $outputDir, $stateRoot, $profileRoot
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
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Patchright seller scaled trial: 130 products once" `
    -Force | Out-Null

$task = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
[pscustomobject]@{
    TaskName = $task.TaskName
    State = $task.State.ToString()
    NextRunTime = $info.NextRunTime.ToString("yyyy-MM-dd HH:mm:ss")
    Arguments = $task.Actions.Arguments
} | ConvertTo-Json
