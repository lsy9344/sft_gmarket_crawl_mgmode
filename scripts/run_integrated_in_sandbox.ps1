$ErrorActionPreference = 'Stop'
try {
    $runDir = Join-Path $env:USERPROFILE 'Desktop\ShipTestRun'
    if (-not (Test-Path -LiteralPath 'C:\ShipTest\run-in-sandbox.ps1') -or
        -not (Test-Path -LiteralPath (Join-Path $runDir 'SellerCollector.exe'))) {
        throw 'Open this launcher inside the existing Windows Sandbox.'
    }
    $name = 'SellerCollector_Integrated_20260928.exe'
    $destination = Join-Path $runDir $name
    $running = @(Get-CimInstance Win32_Process | Where-Object {
        $_.ExecutablePath -eq $destination -or
        ($_.Name -like 'AliCollector*.exe' -and
         $_.ExecutablePath -like "$runDir\*")
    })
    if ($running.Count -gt 0) {
        throw 'Close the old Ali-only window first. If the integrated collector is already open, use that window. Keep the running Gmarket collector open.'
    }
    $source = Join-Path $PSScriptRoot 'SellerCollector.exe'
    $manifest = Join-Path $PSScriptRoot 'SHA256SUMS.txt'
    $expected = ((Get-Content -LiteralPath $manifest | Where-Object {
        $_ -match '  SellerCollector\.exe$'
    }) -split '\s+')[0]
    if (-not $expected -or (Get-FileHash -Algorithm SHA256 -LiteralPath $source).Hash -ne $expected) {
        throw 'Executable checksum mismatch. Copy the complete release again.'
    }
    Copy-Item -LiteralPath $source -Destination $destination -Force
    # No --ali-only: show every collection tab in the standard application.
    Start-Process -FilePath $destination -WorkingDirectory $runDir
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
