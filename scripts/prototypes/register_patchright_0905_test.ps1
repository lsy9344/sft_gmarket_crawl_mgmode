$ErrorActionPreference = "Stop"

$repo = "\\wsl.localhost\Ubuntu-Restored\home\noah\Desktop\project\dev_busi\sft_gmarket_crawl_mgmode-patchright-canary"
$python = "C:\Users\dltnd\AppData\Local\Programs\Python\Python312\python.exe"
$script = Join-Path $repo "scripts\prototypes\coupang_patchright_full_fruit.py"
$outputDir = "C:\Users\dltnd\Desktop\PatchrightFruit"
$backupDir = "C:\Users\dltnd\Desktop\PatchrightFruit_backup_20260902_0905_pretest"
$taskName = "PatchrightFruit-20260902-0905-pages10"
$userId = "${env:USERDOMAIN}\${env:USERNAME}"

Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue |
    Unregister-ScheduledTask -Confirm:$false
Get-ScheduledTask | Where-Object TaskName -Like "PatchrightFruit-20260903-*" |
    Unregister-ScheduledTask -Confirm:$false

if (-not (Test-Path $backupDir)) {
    Copy-Item -Path $outputDir -Destination $backupDir -Recurse
}

$arguments = '"{0}" --pages 10 --output-dir "{1}"' -f $script, $outputDir
$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument $arguments `
    -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Once -At ([datetime]"2026-09-02T09:05:00")
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
    -Description "Patchright 2-hour interval, rolling-envelope review test" `
    -Force | Out-Null

$task = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
[pscustomobject]@{
    TaskName = $task.TaskName
    State = $task.State.ToString()
    NextRunTime = $info.NextRunTime.ToString("yyyy-MM-dd HH:mm:ss")
    StartWhenAvailable = $task.Settings.StartWhenAvailable
    BackupDir = $backupDir
} | ConvertTo-Json
