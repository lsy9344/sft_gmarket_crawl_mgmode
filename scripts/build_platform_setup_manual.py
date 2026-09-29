"""Build the portable setup manual and PDF from its editable HTML source.

Run from the project root: python scripts/build_platform_setup_manual.py
Requires Playwright and its Chromium browser (documentation build only).
"""
from pathlib import Path
import base64
import re

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs/manual_assets/platform-setup"
HTML = ROOT / "docs/SETTINGS_PLATFORM_GUIDE_KO.html"
PDF = HTML.with_suffix(".pdf")


def main() -> None:
    source = (ASSETS / "guide-source.html").read_text(encoding="utf-8")
    for name in set(re.findall(r'(?:src|href)="([^"/]+\.png)"', source)):
        data = base64.b64encode((ASSETS / name).read_bytes()).decode("ascii")
        source = source.replace(f'"{name}"', f'"data:image/png;base64,{data}"')
    HTML.write_text(source, encoding="utf-8")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width":1200, "height":950})
        page.goto(HTML.as_uri())
        page.evaluate("document.fonts.ready")
        page.wait_for_function('Array.from(document.querySelectorAll("img[src]")).every(i => i.complete)')
        assert page.evaluate('Array.from(document.querySelectorAll("img[src]")).every(i => i.naturalWidth > 0)')
        page.emulate_media(media="print")
        overflow = page.evaluate("""Array.from(document.querySelectorAll('.page')).filter(p => {
            const bottom = Math.max(...Array.from(p.children)
                .filter(x => x.tagName !== 'FOOTER').map(x => x.getBoundingClientRect().bottom));
            return bottom > p.querySelector('footer').getBoundingClientRect().top - 5;
        }).map(p => p.id)""")
        assert not overflow, f"Content overlaps page footer: {overflow}"
        page.pdf(path=str(PDF), prefer_css_page_size=True, print_background=True)
        browser.close()
    print(HTML)
    print(PDF)


if __name__ == "__main__":
    main()
