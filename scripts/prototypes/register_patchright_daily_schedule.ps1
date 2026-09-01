$ErrorActionPreference = "Stop"

$repo = "\\wsl.localhost\Ubuntu-Restored\home\noah\Desktop\project\dev_busi\sft_gmarket_crawl_mgmode-patchright-canary"
$python = "C:\Users\dltnd\AppData\Local\Programs\Python\Python312\python.exe"
$script = Join-Path $repo "scripts\prototypes\coupang_patchright_daily_schedule.py"
$outputDir = "C:\Users\dltnd\Desktop\PatchrightFruit"
$userId = "${env:USERDOMAIN}\${env:USERNAME}"

$schedule = @(
    @{ Name = "PatchrightFruit-20260903-0010-pages10a"; At = "2026-09-03T00:10:00"; Stage = "pages10_a" },
    @{ Name = "PatchrightFruit-20260903-0215-pages10b"; At = "2026-09-03T02:15:00"; Stage = "pages10_b" },
    @{ Name = "PatchrightFruit-20260903-0420-remainder"; At = "2026-09-03T04:20:00"; Stage = "daily_remainder" },
    @{ Name = "PatchrightFruit-20260903-0630-audit"; At = "2026-09-03T06:30:00"; Stage = "audit" }
)

Get-ScheduledTask | Where-Object TaskName -Like "PatchrightFruit-20260903-*" |
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

foreach ($item in $schedule) {
    $arguments = '"{0}" --stage {1} --output-dir "{2}"' -f `
        $script, $item.Stage, $outputDir
    $action = New-ScheduledTaskAction `
        -Execute $python `
        -Argument $arguments `
        -WorkingDirectory $repo
    $trigger = New-ScheduledTaskTrigger -Once -At ([datetime]$item.At)
    Register-ScheduledTask `
        -TaskName $item.Name `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description "Patchright fruit daily envelope: $($item.Stage)" `
        -Force | Out-Null
}

Get-ScheduledTask | Where-Object TaskName -Like "PatchrightFruit-20260903-*" |
    Sort-Object TaskName |
    ForEach-Object {
        $info = Get-ScheduledTaskInfo -TaskName $_.TaskName
        [pscustomobject]@{
            TaskName = $_.TaskName
            State = $_.State.ToString()
            NextRunTime = $info.NextRunTime.ToString("yyyy-MM-dd HH:mm:ss")
        }
    } | ConvertTo-Json
