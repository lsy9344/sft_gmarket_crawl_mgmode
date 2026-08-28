"""프로그램 엔트리포인트: 판매자 정보 수집기 v3.0 (Gmarket + Coupang).

실행:
    python -m app.main
(프로젝트 루트에서 실행하면 `app` 패키지가 import 경로에 잡힌다.)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

RUNTIME_SETUP_FLAG = "--setup-runtime"
RUNTIME_VERIFY_FLAG = "--verify-runtime"


def _run_runtime_mode(argv: list[str] | None = None) -> int | None:
    """단일 EXE의 런타임 설치/검증 모드를 실행한다.

    반환값이 ``None``이면 일반 GUI 모드다. 설치 스크립트는 ``sys.argv``를 직접
    읽으므로, 외부 인자를 내부 계약인 ``--verify-only``로 정규화한 뒤 원래 객체를
    반드시 복원한다.
    """
    args = list(sys.argv if argv is None else argv)
    setup_requested = RUNTIME_SETUP_FLAG in args
    verify_requested = RUNTIME_VERIFY_FLAG in args
    if not setup_requested and not verify_requested:
        return None
    if setup_requested and verify_requested:
        print(
            f"{RUNTIME_SETUP_FLAG}와 {RUNTIME_VERIFY_FLAG}는 동시에 사용할 수 없습니다.",
            file=sys.stderr,
        )
        return 2

    from scripts.setup_coupang_runtime import main as setup_main

    original_argv = sys.argv
    executable = args[0] if args else "SellerCollector.exe"
    sys.argv = [executable] + (["--verify-only"] if verify_requested else [])
    try:
        return setup_main()
    finally:
        sys.argv = original_argv


def _hide_console_window() -> None:
    """Windows frozen GUI 모드에서 설치용 콘솔 창을 숨긴다."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    try:
        import ctypes

        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return
        console = windll.kernel32.GetConsoleWindow()
        if console:
            windll.user32.ShowWindow(console, 0)
    except (AttributeError, OSError):
        pass


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


def _install_crash_hooks() -> None:
    """처리되지 않은 예외를 파일 로그에 남긴다 — windowed 빌드(console=False)에서는
    이 훅이 없으면 앱이 아무 흔적 없이 종료된다."""
    import threading
    import traceback

    from app.core.applog import get_logger

    logger = get_logger()

    def _show_crash_dialog(text: str) -> None:
        try:
            from PyQt6.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is not None:
                QMessageBox.critical(
                    None, "예기치 않은 오류",
                    "프로그램에서 처리되지 않은 오류가 발생했습니다.\n"
                    "아래 내용이 진단 로그 파일에도 기록되었습니다.\n\n"
                    + text[-2000:],
                )
        except Exception:
            # 크래시 처리 중 UI 가 이미 깨져 있을 수 있다 — 로그 기록이 우선.
            pass

    def _excepthook(exc_type, exc, tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        logger.error("처리되지 않은 예외:\n%s", text)
        sys.__excepthook__(exc_type, exc, tb)
        _show_crash_dialog(text)

    sys.excepthook = _excepthook

    def _thread_excepthook(args) -> None:
        text = "".join(
            traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
        )
        name = args.thread.name if args.thread else "?"
        logger.error("스레드(%s) 처리되지 않은 예외:\n%s", name, text)
        threading.__excepthook__(args)

    threading.excepthook = _thread_excepthook


def main() -> int:
    runtime_result = _run_runtime_mode()
    if runtime_result is not None:
        return runtime_result

    _hide_console_window()
    _set_browsers_path()
    _ensure_project_root_on_path()

    from app.core.applog import log_line, setup_file_logging

    log_path = setup_file_logging()
    _install_crash_hooks()
    log_line("=== 판매자 정보 수집기 시작 ===")

    from PyQt6.QtWidgets import QApplication

    from app.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("판매자 정보 수집기")
    qss = load_stylesheet()
    if qss:
        app.setStyleSheet(qss)

    window = MainWindow()
    window.show()
    if log_path is not None:
        window.log.append_log(f"진단 로그 파일: {log_path}")
    else:
        window.log.append_log("진단 로그 파일을 생성하지 못했습니다 — 파일 로그 없이 실행합니다.")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
