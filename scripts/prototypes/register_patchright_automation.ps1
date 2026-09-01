$ErrorActionPreference = "Stop"

$repo = "\\wsl.localhost\Ubuntu-Restored\home\noah\Desktop\project\dev_busi\sft_gmarket_crawl_mgmode-patchright-canary"
$python = "C:\Users\dltnd\AppData\Local\Programs\Python\Python312\python.exe"
$script = Join-Path $repo "scripts\prototypes\coupang_patchright_automation.py"
$outputDir = "C:\Users\dltnd\Desktop\PatchrightFruit"
$userId = "${env:USERDOMAIN}\${env:USERNAME}"

$schedule = @(
    @{ Name = "PatchrightFruit-20260902-0055-recovery8"; At = "2026-09-02T00:55:00"; Stage = "recovery8" },
    @{ Name = "PatchrightFruit-20260902-0400-recovery24"; At = "2026-09-02T04:00:00"; Stage = "recovery24" },
    @{ Name = "PatchrightFruit-20260902-0705-recovery600"; At = "2026-09-02T07:05:00"; Stage = "recovery600" },
    @{ Name = "PatchrightFruit-20260902-0855-audit"; At = "2026-09-02T08:55:00"; Stage = "audit" }
)

Get-ScheduledTask | Where-Object TaskName -Like "PatchrightFruit-20260902-*" |
    Unregister-ScheduledTask -Confirm:$false

$principal = New-ScheduledTaskPrincipal `
    -UserId $userId `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 60) `
    -StartWhenAvailable `
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
        -Description "Patchright fruit guarded validation: $($item.Stage)" `
        -Force | Out-Null
}

Get-ScheduledTask | Where-Object TaskName -Like "PatchrightFruit-20260902-*" |
    Sort-Object TaskName |
    ForEach-Object {
        $info = Get-ScheduledTaskInfo -TaskName $_.TaskName
        [pscustomobject]@{
            TaskName = $_.TaskName
            State = $_.State.ToString()
            NextRunTime = $info.NextRunTime.ToString("yyyy-MM-dd HH:mm:ss")
        }
    } | ConvertTo-Json
