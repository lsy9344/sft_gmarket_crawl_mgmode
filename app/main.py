"""프로그램 엔트리포인트: Gmarket 판매자 수집기 v2.0 (WORK_ORDER §7).

실행:
    python -m app.main
(프로젝트 루트에서 실행하면 `app` 패키지가 import 경로에 잡힌다.)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _set_browsers_path() -> None:
    """PyInstaller 번들 환경에서 patchright가 시스템 브라우저를 찾도록 경로 설정."""
    if not getattr(sys, "frozen", False):
        return
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return
    local = os.environ.get("LOCALAPPDATA", "")
    if not local:
        return
    for candidate in (
        Path(local) / "ms-playwright",
        Path(local) / "patchright",
    ):
        if candidate.is_dir():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(candidate)
            return


def _ensure_project_root_on_path() -> None:
    """`python app/main.py` 로 직접 실행해도 `app` 패키지를 찾도록 루트를 추가."""
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def load_stylesheet() -> str:
    qss_path = Path(__file__).resolve().parent / "ui" / "styles" / "theme.qss"
    try:
        return qss_path.read_text(encoding="utf-8")
    except OSError:
        return ""


def main() -> int:
    _set_browsers_path()
    _ensure_project_root_on_path()

    from PyQt6.QtWidgets import QApplication

    from app.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Gmarket 판매자 수집기")
    qss = load_stylesheet()
    if qss:
        app.setStyleSheet(qss)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
