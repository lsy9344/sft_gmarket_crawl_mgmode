#!/usr/bin/env bash
# v2 포트폴리오 이미지 재생성 스크립트
# HTML(source/)을 헤드리스 크로미움으로 열어 2배 해상도 PNG(images/)로 저장한다.
# 주의: 창 높이를 콘텐츠 높이와 똑같이 잡으면 맨 아래 요소가 잘리는 문제가 있어
#       200px 크게 렌더링한 뒤 ffmpeg으로 정확한 크기로 잘라낸다.
set -euo pipefail

dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
chrome="$(ls -d "$HOME"/.cache/ms-playwright/chromium-*/chrome-linux/chrome | sort -V | tail -1)"
mkdir -p "$dir/images"

render() { # name height
  local name="$1" h="$2"
  "$chrome" --headless=new --no-sandbox --disable-gpu --hide-scrollbars \
    --allow-file-access-from-files --force-device-scale-factor=2 \
    --window-size=1200,$((h + 200)) --virtual-time-budget=8000 \
    --screenshot="$dir/images/tmp_$name.png" "file://$dir/source/$name.html" 2>/dev/null
  ffmpeg -hide_banner -loglevel error -y -i "$dir/images/tmp_$name.png" \
    -vf "crop=2400:$((h * 2)):0:0" "$dir/images/$name.png"
  rm -f "$dir/images/tmp_$name.png"
}

render 01_main_cover 1200
render 02_workflow 1370
render 03_product_ui 1330
render 04_reliability 1200

file "$dir/images/"*.png
