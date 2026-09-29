$ErrorActionPreference = 'Stop'

# Run inside the already-open sandbox. Keep the original collector and its
# Gmarket job running; only the old Ali job must be stopped before resuming.
try {
    $runDir = Join-Path $env:USERPROFILE 'Desktop\ShipTestRun'
    if (-not (Test-Path -LiteralPath 'C:\ShipTest\run-in-sandbox.ps1') -or
        -not (Test-Path -LiteralPath (Join-Path $runDir 'SellerCollector.exe'))) {
        throw 'Open this launcher INSIDE the existing Windows Sandbox, from C:\ShipTest\AliPhoneFix_20260928. Do not open a new sandbox.'
    }

    $name = 'AliCollector_PhoneFix_20260928.exe'
    $oldAli = @(Get-CimInstance Win32_Process |
        Where-Object { $_.ExecutablePath -in @((Join-Path $runDir 'AliCollector_20260928.exe'), (Join-Path $runDir 'AliCollector_SaveData_20260928.exe')) })
    if ($oldAli.Count -gt 0) {
        throw 'Stop collection and close the old Ali-only window first, then open this launcher again. Keep the Gmarket window open.'
    }
    $source = Join-Path $PSScriptRoot $name
    $destination = Join-Path $runDir $name
    $running = @(Get-CimInstance Win32_Process -Filter "Name = '$name'" |
        Where-Object { $_.ExecutablePath -eq $destination })
    if ($running.Count -gt 0) {
        throw 'The updated Ali collector is already open. Use that window.'
    }

    $manifest = Join-Path $PSScriptRoot 'SHA256SUMS.txt'
    $expected = ((Get-Content -LiteralPath $manifest | Where-Object {
        $_ -match ('  ' + [regex]::Escape($name) + '$')
    }) -split '\s+')[0]
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $source).Hash
    if (-not $expected -or $actual -ne $expected) {
        throw 'The Ali executable checksum does not match. Copy the complete update folder again.'
    }

    Copy-Item -LiteralPath $source -Destination $destination -Force
    Start-Process -FilePath $destination -ArgumentList '--ali-only' -WorkingDirectory $runDir
    Write-Host 'Ali collector opened. The existing Gmarket collector was not stopped or replaced.'
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
