param(
    # 예약 작업명 = 인스턴스 식별자. 완성형 복사 폴더명도 여기서 파생한다.
    [string]$TaskName = "PatchrightTop1000-80Min",
    # 이 실행기가 쓸 워크트리(기본값 = 병렬 확장 워크트리).
    [string]$RepoRoot = "\\wsl.localhost\Ubuntu-Restored\home\noah\Desktop\project\dev_busi\sft_gmarket_crawl_mgmode-parallel-scaleout",
    [string]$OutputDir = "C:\Users\dltnd\Desktop\PatchrightTop1000",
    [string]$StateRoot = (Join-Path $env:LOCALAPPDATA "SellerCollectorPatchrightCanary"),
    # 인스턴스 B/C의 Decodo 스티키 세션 ID. 비어 있으면 A(집 회선 직접)와 같은 현행 동작.
    [string]$ProxySessionId = "",
    # 이 인스턴스가 수집할 카테고리 가족 정의 JSON 경로. 비어 있으면 기본 A 가족(현행 동작).
    [string]$CategoriesFile = ""
)

$ErrorActionPreference = "Stop"

$python = "C:\Users\dltnd\AppData\Local\Programs\Python\Python312\python.exe"
$script = Join-Path $RepoRoot "scripts\prototypes\coupang_patchright_top_pipeline.py"

# 프록시 세션 ID가 있을 때만 --proxy-session-id 를 붙인다(조건부 인자는 배열로 조립).
$pipelineArgs = @(
    "--seller-limit", "130",
    "--output-dir", $OutputDir,
    "--state-root", $StateRoot
)
if ($ProxySessionId -ne "") {
    $pipelineArgs += @("--proxy-session-id", $ProxySessionId)
}
# 카테고리 가족 파일이 있을 때만 --categories-file 을 붙인다(다른 인스턴스 가족).
if ($CategoriesFile -ne "") {
    $pipelineArgs += @("--categories-file", $CategoriesFile)
}

& $python $script @pipelineArgs
$code = $LASTEXITCODE

if ($code -eq 30) {
    # 완성형 복사 폴더명을 작업명에서 파생(예: PatchrightTop1000B-80Min →
    # PatchrightTop1000B_final_시각)해 인스턴스 간 충돌을 없앤다.
    $instanceName = $TaskName -replace '-\d+Min$', ''
    $stamp = Get-Date -Format "yyyyMMdd_HHmm"
    $dest = Join-Path (
        [Environment]::GetFolderPath("Desktop")
    ) "garbage\${instanceName}_final_$stamp"
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    # 판매자 완성형 복사. 한글 파일명 패턴은 PS 5.1이 BOM 없는 UTF-8 스크립트를
    # ANSI로 읽으면 깨지므로 ASCII 접두사로만 짚는다(출력 폴더에서 coupang_*은
    # 판매자 완성형뿐이다).
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
    # 0이 아닌 종료 코드(차단/오류)면 이 인스턴스의 예약만 비활성화한다.
    Disable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
    exit $code
}
exit 0
