#!/usr/bin/env python3
"""HTML 매뉴얼을 A4 규격 고품질 PDF로 변환하는 스크립트.

- HTML 내부의 로컬 이미지 경로(manual_assets/...)를 자동으로 읽어 base64 데이터 URI로 치환하여
  QTextDocument가 어떤 경로 환경에서도 이미지를 완벽하게 임베드하도록 보장합니다.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import re
import sys
from pathlib import Path

from PyQt6.QtCore import QMarginsF
from PyQt6.QtGui import QPageLayout, QPageSize, QPdfWriter, QTextDocument
from PyQt6.QtWidgets import QApplication


def embed_images_as_base64(html: str, base_dir: Path) -> str:
    """<img> 태그의 상대 경로를 base64 data URI로 변환."""
    def replace_src(match: re.Match) -> str:
        src = match.group(1)
        if src.startswith("data:") or src.startswith("http://") or src.startswith("https://"):
            return match.group(0)

        img_file = (base_dir / src).resolve()
        if not img_file.exists():
            print(f"경고: 이미지를 찾을 수 없습니다: {img_file}")
            return match.group(0)

        mime_type, _ = mimetypes.guess_type(str(img_file))
        if not mime_type:
            mime_type = "image/jpeg"

        data = img_file.read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        return f'src="data:{mime_type};base64,{b64}"'

    return re.sub(r'src=["\']([^"\']+)["\']', replace_src, html)


def convert_html_to_pdf(html_path: Path, pdf_path: Path) -> None:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    if not html_path.exists():
        raise FileNotFoundError(f"HTML 파일이 존재하지 않습니다: {html_path}")

    raw_html = html_path.read_text(encoding="utf-8")
    processed_html = embed_images_as_base64(raw_html, html_path.parent)

    doc = QTextDocument()
    # A4 너비(210mm - 여백 20mm = 190mm)에 맞춘 문서 너비 설정
    # 72 DPI 기준 190mm ≈ 538pt
    doc.setDocumentMargin(0)
    doc.setHtml(processed_html)

    writer = QPdfWriter(str(pdf_path))
    layout = QPageLayout(
        QPageSize(QPageSize.PageSizeId.A4),
        QPageLayout.Orientation.Portrait,
        QMarginsF(10, 10, 10, 10),
        QPageLayout.Unit.Millimeter,
    )
    writer.setPageLayout(layout)
    writer.setResolution(150)

    doc.print(writer)
    print(f"PDF 생성 완료: {pdf_path} (크기: {pdf_path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    base_dir = Path(__file__).resolve().parent.parent
    if len(sys.argv) >= 3:
        convert_html_to_pdf(Path(sys.argv[1]), Path(sys.argv[2]))
    else:
        # 기본 2종 매뉴얼(외부 플랫폼 API 매뉴얼 및 UI 사용 매뉴얼) 모두 갱신
        api_html = base_dir / "docs" / "External_Platform_API_Manual_KO.html"
        api_pdf = base_dir / "docs" / "External_Platform_API_Manual_KO.pdf"
        if api_html.exists():
            convert_html_to_pdf(api_html, api_pdf)

        ui_html = base_dir / "docs" / "SellerCollector_UI_Manual_KO.html"
        ui_pdf = base_dir / "docs" / "SellerCollector_UI_Manual_KO.pdf"
        if ui_html.exists():
            convert_html_to_pdf(ui_html, ui_pdf)
