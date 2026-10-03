# 병렬 확장 인스턴스(A/B/C) 통합 점검 — 읽기 전용, 네트워크 요청 0회.
# 안전 장부(canary_guard.json), 파이프라인 실행 로그(top_pipeline_runs.jsonl),
# 예약 작업 상태를 한 번에 모아 JSON 하나로 출력한다.
# 파일/작업이 없는 인스턴스는 not_initialized 로 표시하고 계속 진행한다.

$ErrorActionPreference = "Stop"

# 인스턴스 정의(병렬 확장 설계 §3.1). 장부 폴더가 나뉘면 Chrome 프로필도
# 자동으로 나뉜다. StateRootSuffix: A="", B="_B", C="_C".
$canaryBase = Join-Path $env:LOCALAPPDATA "SellerCollectorPatchrightCanary"
$desktop = [Environment]::GetFolderPath("Desktop")
$instances = @(
    [pscustomobject]@{
        Key = "A"; TaskName = "PatchrightTop1000-80Min"
        StateRoot = $canaryBase
        OutputDir = Join-Path $desktop "PatchrightTop1000"
    },
    [pscustomobject]@{
        Key = "B"; TaskName = "PatchrightTop1000B-80Min"
        StateRoot = "$canaryBase`_B"
        OutputDir = Join-Path $desktop "PatchrightTop1000_B"
    },
    [pscustomobject]@{
        Key = "C"; TaskName = "PatchrightTop1000C-80Min"
        StateRoot = "$canaryBase`_C"
        OutputDir = Join-Path $desktop "PatchrightTop1000_C"
    }
)

function Get-GuardSummary {
    # canary_guard.json 요약 — 필드명은 app/core/coupang/patchright_canary.py 기준.
    param([string]$StateRoot)
    $path = Join-Path $StateRoot "canary_guard.json"
    if (-not (Test-Path $path)) {
        return [pscustomobject]@{ Status = "not_initialized" }
    }
    try {
        $guard = Get-Content $path -Raw -ErrorAction Stop | ConvertFrom-Json
        return [pscustomobject]@{
            Status = "ok"
            blocked = [bool]$guard.blocked
            recovery_hold = [bool]$guard.recovery_hold
            last_attempt_at = [string]$guard.last_attempt_at
            daily_sessions = [int]$guard.daily_sessions
            daily_items_reserved = [int]$guard.daily_items_reserved
        }
    } catch {
        return [pscustomobject]@{ Status = "unreadable"; error = "canary_guard.json 읽기 실패" }
    }
}

function Get-PipelineRunSummary {
    # top_pipeline_runs.jsonl — 마지막 줄(event/action/exit_code)과 가장 최근
    # exit_ip_check(회선 관측, 설계 §3.2-2)를 뽑는다. 하루 약 36줄이므로 최근
    # 400줄만 읽어도 최근 며칠 치는 충분히 커버된다.
    param([string]$OutputDir)
    $path = Join-Path $OutputDir "top_pipeline_runs.jsonl"
    if (-not (Test-Path $path)) {
        return [pscustomobject]@{
            Status = "not_initialized"
            last_run = $null
            last_exit_ip_check = $null
        }
    }
    $lastRun = $null
    $lastExitIp = $null
    try {
        $lines = @(
            Get-Content $path -Tail 400 -ErrorAction Stop |
                Where-Object { $_.Trim() -ne "" }
        )
    } catch {
        return [pscustomobject]@{
            Status = "unreadable"
            last_run = $null
            last_exit_ip_check = $null
        }
    }
    foreach ($line in $lines) {
        try {
            $entry = $line | ConvertFrom-Json
        } catch {
            continue
        }
        if ($null -eq $entry.event) { continue }
        $lastRun = [pscustomobject]@{
            event = [string]$entry.event
            action = [string]$entry.action
            exit_code = $entry.exit_code
        }
        if ($entry.event -eq "exit_ip_check") {
            $lastExitIp = [pscustomobject]@{
                proxy_session_id = [string]$entry.proxy_session_id
                ip = [string]$entry.ip
                country_code = [string]$entry.country_code
                ok = [bool]$entry.ok
                error_kind = [string]$entry.error_kind
            }
        }
    }
    if ($null -eq $lastRun) {
        return [pscustomobject]@{
            Status = "not_initialized"
            last_run = $null
            last_exit_ip_check = $null
        }
    }
    return [pscustomobject]@{
        Status = "ok"
        last_run = $lastRun
        last_exit_ip_check = $lastExitIp
    }
}

function Get-TaskSummary {
    # 예약 작업 상태 — 없으면 not_initialized(아직 등록 전 인스턴스).
    param([string]$TaskName)
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        return [pscustomobject]@{ Status = "not_initialized" }
    }
    $nextRun = ""
    $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $info -and $info.NextRunTime) {
        $nextRun = $info.NextRunTime.ToString("yyyy-MM-dd HH:mm:ss")
    }
    return [pscustomobject]@{
        Status = "ok"
        state = $task.State.ToString()
        next_run_time = $nextRun
    }
}

$results = foreach ($instance in $instances) {
    [pscustomobject]@{
        Key = $instance.Key
        TaskName = $instance.TaskName
        StateRoot = $instance.StateRoot
        OutputDir = $instance.OutputDir
        Guard = Get-GuardSummary -StateRoot $instance.StateRoot
        PipelineRuns = Get-PipelineRunSummary -OutputDir $instance.OutputDir
        ScheduledTask = Get-TaskSummary -TaskName $instance.TaskName
    }
}

[pscustomobject]@{
    GeneratedAt = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    Instances = @($results)
} | ConvertTo-Json -Depth 6
