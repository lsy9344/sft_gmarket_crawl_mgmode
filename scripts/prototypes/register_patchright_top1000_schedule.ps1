param(
    # 인스턴스별 예약 작업명. A=PatchrightTop1000-80Min, B=PatchrightTop1000B-80Min, ...
    [string]$TaskName = "PatchrightTop1000-80Min",
    [datetime]$FirstRun = (Get-Date).AddMinutes(85),
    [int]$RunCount = 18,
    [int]$IntervalMinutes = 80,
    [string]$RepoRoot = "\\wsl.localhost\Ubuntu-Restored\home\noah\Desktop\project\dev_busi\sft_gmarket_crawl_mgmode-parallel-scaleout",
    [string]$OutputDir = "C:\Users\dltnd\Desktop\PatchrightTop1000",
    [string]$StateRoot = (Join-Path $env:LOCALAPPDATA "SellerCollectorPatchrightCanary"),
    # 인스턴스 B/C의 Decodo 스티키 세션 ID. 비어 있으면 A(집 회선 직접).
    [string]$ProxySessionId = "",
    # 이 인스턴스가 수집할 카테고리 가족 정의 JSON 경로. 비어 있으면 기본 A 가족.
    [string]$CategoriesFile = ""
)

$ErrorActionPreference = "Stop"

$runner = Join-Path $RepoRoot "scripts\prototypes\run_patchright_top1000.ps1"
$userId = "${env:USERDOMAIN}\${env:USERNAME}"

# 참고: 작업당 일회성 트리거 상한은 48개(실측). 80분 간격 일 18회라 여유가
# 있고, 상한에 닿으면 기존 방식(이어서 재등록)으로 연장한다.

$triggers = @()
$registeredRunTimes = @()
foreach ($index in 0..($RunCount - 1)) {
    $at = $FirstRun.AddMinutes($IntervalMinutes * $index)
    $triggers += New-ScheduledTaskTrigger -Once -At $at
    $registeredRunTimes += $at.ToString("yyyy-MM-dd HH:mm:ss")
}

Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue |
    Unregister-ScheduledTask -Confirm:$false

# 실행기에 인스턴스 값 전부를 넘긴다. 프록시 세션 ID는 있을 때만 붙인다.
$arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -TaskName "{1}" -RepoRoot "{2}" -OutputDir "{3}" -StateRoot "{4}"' -f `
    $runner, $TaskName, $RepoRoot, $OutputDir, $StateRoot
if ($ProxySessionId -ne "") {
    $arguments += ' -ProxySessionId "{0}"' -f $ProxySessionId
}
# 카테고리 가족 파일도 있을 때만 실행기로 넘긴다(다른 인스턴스 가족).
if ($CategoriesFile -ne "") {
    $arguments += ' -CategoriesFile "{0}"' -f $CategoriesFile
}
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $arguments `
    -WorkingDirectory $RepoRoot
$principal = New-ScheduledTaskPrincipal `
    -UserId $userId `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 60) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

$description = "{0}-minute top-1000 3P category dataset runs; stop on block or error" -f $IntervalMinutes
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $triggers `
    -Principal $principal `
    -Settings $settings `
    -Description $description | Out-Null

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
[pscustomobject]@{
    TaskName = $task.TaskName
    State = $task.State.ToString()
    NextRunTime = $info.NextRunTime.ToString("yyyy-MM-dd HH:mm:ss")
    TriggerCount = @($task.Triggers).Count
    IntervalMinutes = $IntervalMinutes
    RunCount = $RunCount
    RegisteredRunTimes = $registeredRunTimes
} | ConvertTo-Json
