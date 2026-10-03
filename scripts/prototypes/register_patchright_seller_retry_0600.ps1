$ErrorActionPreference = "Stop"

$repo = "\\wsl.localhost\Ubuntu-Restored\home\noah\Desktop\project\dev_busi\sft_gmarket_crawl_mgmode-patchright-canary"
$python = "C:\Users\dltnd\AppData\Local\Programs\Python\Python312\python.exe"
$script = Join-Path $repo "scripts\prototypes\coupang_patchright_full_sellers.py"
$outputDir = "C:\Users\dltnd\Desktop\PatchrightFruit"
$stateRoot = Join-Path $env:LOCALAPPDATA "SellerCollectorPatchrightSeller"
$profileRoot = Join-Path $env:LOCALAPPDATA "SellerCollectorPatchrightCanary"
$userId = "${env:USERDOMAIN}\${env:USERNAME}"

$schedule = @(
    @{
        Name = "PatchrightFruitSeller-20260903-0100-Retry100"
        At = [datetime]"2026-09-03T01:00:00"
        Resume = $true
    },
    @{
        Name = "PatchrightFruitSeller-20260903-0305-Next100"
        At = [datetime]"2026-09-03T03:05:00"
        Resume = $false
    },
    @{
        Name = "PatchrightFruitSeller-20260903-0600-Final100"
        At = [datetime]"2026-09-03T06:00:00"
        Resume = $false
    }
)

Get-ScheduledTask | Where-Object TaskName -Like "PatchrightFruitSeller-*" |
    Unregister-ScheduledTask -Confirm:$false

$principal = New-ScheduledTaskPrincipal `
    -UserId $userId `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

foreach ($item in $schedule) {
    $arguments = '"{0}" --limit 100 --output-dir "{1}" --state-root "{2}" --profile-root "{3}"' -f `
        $script, $outputDir, $stateRoot, $profileRoot
    if ($item.Resume) {
        $arguments += " --resume-http-503"
    }
    $action = New-ScheduledTaskAction `
        -Execute $python `
        -Argument $arguments `
        -WorkingDirectory $repo
    $trigger = New-ScheduledTaskTrigger -Once -At $item.At
    Register-ScheduledTask `
        -TaskName $item.Name `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description "Patchright seller dataset: 100 products once" `
        -Force | Out-Null
}

Get-ScheduledTask | Where-Object TaskName -Like "PatchrightFruitSeller-*" |
    Sort-Object TaskName |
    ForEach-Object {
        $info = Get-ScheduledTaskInfo -TaskName $_.TaskName
        [pscustomobject]@{
            TaskName = $_.TaskName
            State = $_.State.ToString()
            NextRunTime = $info.NextRunTime.ToString("yyyy-MM-dd HH:mm:ss")
            Arguments = $_.Actions.Arguments
        }
    } | ConvertTo-Json
