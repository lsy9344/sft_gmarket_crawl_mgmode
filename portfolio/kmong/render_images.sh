#!/usr/bin/env bash
set -euo pipefail

portfolio_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_dir="$portfolio_dir/source"
image_dir="$portfolio_dir/images"

mkdir -p "$image_dir"

ffmpeg -hide_banner -loglevel error -y \
  -i "$portfolio_dir/assets/gmarket_ui_original.png" \
  -vf "drawbox=x=82:y=348:w=910:h=25:color=white:t=fill,drawbox=x=82:y=348:w=910:h=25:color=0x9b9b9b:t=1,drawtext=fontfile=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc:text='Output folder selected':x=91:y=349:fontsize=14:fontcolor=0x222222" \
  -frames:v 1 \
  "$portfolio_dir/assets/gmarket_ui_sanitized.png"

ffmpeg -hide_banner -loglevel error -y \
  -i "$source_dir/01_main_cover.svg" -frames:v 1 \
  "$portfolio_dir/assets/01_main_cover_foreground.png"

ffmpeg -hide_banner -loglevel error -y \
  -i "$portfolio_dir/assets/generated_minimal_ui_bg_v2.png" \
  -i "$portfolio_dir/assets/01_main_cover_foreground.png" \
  -i "$portfolio_dir/assets/gmarket_ui_sanitized.png" \
  -filter_complex "[0:v]scale=1200:1200[bg];[2:v]scale=1040:851[gm];[bg][1:v]overlay=0:0:format=auto[cover];[cover][gm]overlay=80:330" \
  -frames:v 1 "$image_dir/01_main_cover.png"

for name in 02_project_overview 03_product_ui 04_engineering_quality; do
  ffmpeg -hide_banner -loglevel error -y \
    -i "$source_dir/$name.svg" -frames:v 1 \
    "$image_dir/$name.png"
done

ffmpeg -hide_banner -loglevel error -y \
  -i "$image_dir/02_project_overview.png" \
  -i "$portfolio_dir/assets/gmarket_ui_sanitized.png" \
  -filter_complex "[1:v]scale=1080:884[gm];[0:v][gm]overlay=60:270" \
  -frames:v 1 "$portfolio_dir/assets/02_project_overview_composited.png"
mv "$portfolio_dir/assets/02_project_overview_composited.png" "$image_dir/02_project_overview.png"

ffmpeg -hide_banner -loglevel error -y \
  -i "$image_dir/03_product_ui.png" \
  -i "$portfolio_dir/assets/coupang_ui_original.png" \
  -filter_complex "[1:v]scale=1080:884[cp];[0:v][cp]overlay=60:270" \
  -frames:v 1 "$portfolio_dir/assets/03_product_ui_composited.png"
mv "$portfolio_dir/assets/03_product_ui_composited.png" "$image_dir/03_product_ui.png"

file "$image_dir"/*.png
