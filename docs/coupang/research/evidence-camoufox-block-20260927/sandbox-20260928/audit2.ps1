$ErrorActionPreference='SilentlyContinue'
$out = [ordered]@{ marker='SANDBOX_FULL_AUDIT'; observed_at=(Get-Date).ToString('o') }
$logDir = "$env:LOCALAPPDATA\SellerCollector\logs"
$out.log_dir = $logDir
$out.log_files = @(Get-ChildItem $logDir | ForEach-Object { "$($_.Name)|$($_.Length)|$($_.LastWriteTime.ToString('s'))" })
$mainLog = Join-Path $logDir 'seller_collector.log'
if (Test-Path $mainLog) {
  $lines = Get-Content $mainLog -Encoding UTF8
  $out.log_total_lines = $lines.Count
  $out.log_first_line = $lines[0]
  $out.log_tail400 = ($lines | Select-Object -Last 400) -join "`n"
}
$root = "$env:USERPROFILE\Desktop\ShipTestRun\output"
$out.categories = @(Get-ChildItem $root -Directory | ForEach-Object {
  $d = $_.FullName
  $results = @(Get-ChildItem $d -Filter '*.json' | Where-Object {$_.Name -ne 'coupang_block_state.json'} | ForEach-Object {$_.Name})
  $bs = Join-Path $d 'coupang_block_state.json'
  $blockedAt = $null
  if (Test-Path $bs) { $blockedAt = (Get-Content $bs -Raw | ConvertFrom-Json).blocked_at }
  [ordered]@{ name=$_.Name; result_files=($results -join ','); has_resume=(Test-Path (Join-Path $d 'resume.sqlite3')); blocked_at=$blockedAt }
})
($out | ConvertTo-Json -Depth 4 -Compress) | Set-Clipboard
