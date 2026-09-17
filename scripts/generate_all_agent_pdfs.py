#!/usr/bin/env python3
"""3인의 herdr 에이전트 산출물(Markdown)을 각각 독립된 고품질 A4 PDF로 변환하고
윈도우 바탕화면으로 일괄 복사하는 스크립트.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import re
import shutil
import sys
from pathlib import Path

from markdown_it import MarkdownIt
from PyQt6.QtCore import QMarginsF
from PyQt6.QtGui import QPageLayout, QPageSize, QPdfWriter, QTextDocument
from PyQt6.QtWidgets import QApplication


def markdown_to_html(md_content: str, title: str = "매뉴얼") -> str:
    """마크다운을 고품질 A4 인쇄용 HTML로 변환."""
    # 1. GitHub Callouts 변환 (> [!NOTE] 등)
    def callout_repl(match: re.Match) -> str:
        ctype = match.group(1).lower()
        body = match.group(2).strip()
        body_lines = [re.sub(r"^>\s?", "", line) for line in body.splitlines()]
        clean_body = "<br>".join(body_lines)
        titles = {
            "note": "📌 참고 (NOTE)",
            "warning": "⚠️ 주의 (WARNING)",
            "important": "❗ 중요 (IMPORTANT)",
            "caution": "🛑 경고 (CAUTION)",
            "tip": "💡 팁 (TIP)",
        }
        ctitle = titles.get(ctype, ctype.upper())
        return f'<div class="callout callout-{ctype}"><b class="callout-title">{ctitle}</b><br>{clean_body}</div>'

    md_proc = re.sub(
        r"> \[!(NOTE|WARNING|IMPORTANT|CAUTION|TIP)\]\n((?:> .*\n?)+)",
        callout_repl,
        md_content,
    )

    # 2. MarkdownIt GFM Table 렌더러
    md = MarkdownIt().enable("table")
    body_html = md.render(md_proc)

    html = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <style>
    @page {{ size: A4; margin: 12mm; }}
    body {{
      font-family: "Noto Sans CJK KR", "Malgun Gothic", sans-serif;
      color: #1e293b;
      background: white;
      font-size: 8.5pt;
      line-height: 1.45;
      margin: 0;
      padding: 0;
    }}
    h1 {{ color: #0f172a; font-size: 18pt; font-weight: 800; border-bottom: 2px solid #2563eb; padding-bottom: 6px; margin: 14px 0 10px; }}
    h2 {{ color: #1e3a8a; font-size: 13pt; font-weight: 700; border-bottom: 1px solid #93c5fd; padding-bottom: 4px; margin: 14px 0 8px; }}
    h3 {{ color: #1e40af; font-size: 10.5pt; font-weight: 700; margin: 10px 0 4px; }}
    h4 {{ color: #334155; font-size: 9pt; font-weight: 700; margin: 8px 0 3px; }}
    p {{ margin: 4px 0 6px; }}
    ol, ul {{ margin: 4px 0 8px 20px; padding: 0; }}
    li {{ margin: 2px 0; }}
    code {{ font-family: monospace; background: #f1f5f9; padding: 1px 4px; color: #0369a1; font-size: 8pt; }}
    pre {{ background: #0f172a; color: #f8fafc; padding: 8px 12px; font-size: 7.8pt; line-height: 1.35; }}
    table {{ width: 100%; border-collapse: collapse; margin: 8px 0; font-size: 8pt; }}
    th {{ background: #1e3a8a; color: white; padding: 5px 6px; text-align: left; font-weight: bold; border: 1px solid #cbd5e1; }}
    td {{ border: 1px solid #cbd5e1; padding: 5px 6px; vertical-align: top; }}
    tr:nth-child(even) {{ background: #f8fafc; }}
    img {{ max-width: 95%; height: auto; border: 1px solid #cbd5e1; margin: 8px auto; display: block; }}
    .callout {{ padding: 8px 12px; margin: 8px 0; border-left: 4px solid #2563eb; font-size: 8pt; }}
    .callout-note {{ background: #eff6ff; border-left-color: #3b82f6; color: #1e40af; }}
    .callout-tip {{ background: #f0fdf4; border-left-color: #22c55e; color: #15803d; }}
    .callout-important {{ background: #fef2f2; border-left-color: #ef4444; color: #b91c1c; }}
    .callout-warning {{ background: #fffbeb; border-left-color: #f59e0b; color: #b45309; }}
    .callout-caution {{ background: #fff1f2; border-left-color: #e11d48; color: #be123c; }}
    .callout-title {{ font-size: 8.5pt; margin-bottom: 2px; display: inline-block; }}
    hr {{ border: 0; border-top: 1px solid #e2e8f0; margin: 14px 0; }}
  </style>
</head>
<body>
  {body_html}
</body>
</html>"""
    return html


def embed_images_as_base64(html: str, base_dir: Path) -> str:
    """<img> 태그의 로컬 이미지 경로를 base64 data URI로 변환."""
    def replace_src(match: re.Match) -> str:
        src = match.group(1)
        if src.startswith("data:") or src.startswith("http://") or src.startswith("https://"):
            return match.group(0)

        # file:// 처리
        if src.startswith("file://"):
            src_clean = src.replace("file://", "")
            img_file = Path(src_clean)
        else:
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


def convert_content_to_pdf(html_content: str, base_dir: Path, pdf_path: Path) -> None:
    """HTML 문자열을 A4 PDF 파일로 변환."""
    processed_html = embed_images_as_base64(html_content, base_dir)

    doc = QTextDocument()
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
    print(f"PDF 생성 완료: {pdf_path.name} ({pdf_path.stat().st_size:,} bytes)")


def main() -> None:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    base_dir = Path(__file__).resolve().parent.parent
    docs_dir = base_dir / "docs"

    # 3개 에이전트 산출물 매핑
    agent_tasks = [
        (
            docs_dir / "MANUAL_DIATAXIS.md",
            docs_dir / "1_MANUAL_DIATAXIS (doc_writer).pdf",
            "외부 플랫폼 연동 매뉴얼 (Diátaxis 규격)",
        ),
        (
            docs_dir / "MANUAL_APP_USER.md",
            docs_dir / "2_MANUAL_APP_USER (app_manual).pdf",
            "판매자 정보 수집기 사용자 설명서",
        ),
        (
            docs_dir / "MANUAL_PLATFORM_INTEGRATION.md",
            docs_dir / "3_MANUAL_PLATFORM_INTEGRATION (platform_guide).pdf",
            "외부 플랫폼 연동 및 자격 증명 관리 가이드",
        ),
    ]

    generated_pdfs: list[Path] = []

    # 1. 3개 에이전트의 마크다운 -> 개별 PDF 생성
    for md_file, pdf_file, title in agent_tasks:
        if md_file.exists():
            md_text = md_file.read_text(encoding="utf-8")
            html_text = markdown_to_html(md_text, title)
            convert_content_to_pdf(html_text, docs_dir, pdf_file)
            generated_pdfs.append(pdf_file)
        else:
            print(f"경고: {md_file} 파일이 없습니다.")

    # 2. 종합본 HTML -> PDF 생성
    combo_html = docs_dir / "External_Platform_API_Manual_KO.html"
    combo_pdf = docs_dir / "종합_External_Platform_API_Manual_KO.pdf"
    if combo_html.exists():
        convert_content_to_pdf(combo_html.read_text(encoding="utf-8"), docs_dir, combo_pdf)
        generated_pdfs.append(combo_pdf)

    # 3. 윈도우 바탕화면 복사
    win_desktop = Path("/mnt/c/Users/dltnd/Desktop/SellerCollector_Manuals_PDF")
    win_desktop.mkdir(parents=True, exist_ok=True)

    print("\n--- 윈도우 바탕화면 복사 시작 ---")
    for pdf in generated_pdfs:
        target = win_desktop / pdf.name
        shutil.copy2(pdf, target)
        print(f"복사 완료: {target}")


if __name__ == "__main__":
    main()
