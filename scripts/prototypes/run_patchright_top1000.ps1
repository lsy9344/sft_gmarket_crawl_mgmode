param(
    [string]$TaskName = "PatchrightTop1000-80Min"
)

$ErrorActionPreference = "Stop"

$repo = "\\wsl.localhost\Ubuntu-Restored\home\noah\Desktop\project\dev_busi\sft_gmarket_crawl_mgmode-patchright-canary"
$python = "C:\Users\dltnd\AppData\Local\Programs\Python\Python312\python.exe"
$script = Join-Path $repo "scripts\prototypes\coupang_patchright_top_pipeline.py"
$outputDir = "C:\Users\dltnd\Desktop\PatchrightTop1000"
$stateRoot = Join-Path $env:LOCALAPPDATA "SellerCollectorPatchrightCanary"

& $python $script `
    --seller-limit 130 `
    --output-dir $outputDir `
    --state-root $stateRoot
$code = $LASTEXITCODE

if ($code -eq 30) {
    $stamp = Get-Date -Format "yyyyMMdd_HHmm"
    $dest = Join-Path (
        [Environment]::GetFolderPath("Desktop")
    ) "garbage\PatchrightTop1000_final_$stamp"
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    # 한글 파일명 패턴은 PS 5.1이 BOM 없는 UTF-8 스크립트를 ANSI로 읽으면 깨지므로
    # ASCII 접두사로만 짚는다(출력 폴더에서 coupang_*은 판매자 완성형뿐이다).
    Copy-Item (Join-Path $outputDir "coupang_*.csv") $dest -ErrorAction SilentlyContinue
    Copy-Item (Join-Path $outputDir "top_sellers.csv") $dest -ErrorAction SilentlyContinue
    Copy-Item (Join-Path $outputDir "top_product_seller.csv") $dest -ErrorAction SilentlyContinue
    Copy-Item (Join-Path $outputDir "final_dataset_*.csv") $dest -ErrorAction SilentlyContinue
    Copy-Item (Join-Path $outputDir "top_1000_*.csv") $dest -ErrorAction SilentlyContinue
    Copy-Item (Join-Path $outputDir "top_products.csv") $dest -ErrorAction SilentlyContinue
    Copy-Item (Join-Path $outputDir "top_state.json") $dest -ErrorAction SilentlyContinue
    Copy-Item (Join-Path $outputDir "top_seller_control.json") $dest -ErrorAction SilentlyContinue
    Disable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
    exit 0
}
if ($code -ne 0) {
    Disable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
    exit $code
}
exit 0
