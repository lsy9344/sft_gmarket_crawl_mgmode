"""설정 탭 매뉴얼용 화면 캡처.

실제 계정 파일이나 외부 API를 읽지 않고, 빈 상태와 문서용 가짜 입력값만
화면에 넣어 ``docs/manual_assets/platform-setup`` 아래 PNG로 저장한다.

예:
    QT_QPA_PLATFORM=offscreen python scripts/capture_settings_screenshots.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs" / "manual_assets" / "platform-setup"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _capture(example: bool, output: Path) -> None:
    # Qt must be imported after the caller has selected the offscreen platform.
    from PyQt6.QtWidgets import QApplication

    from app.core import brightdata, decodo

    if example:
        brightdata_settings = brightdata.BrightDataSettings(
            api_token="bd_demo_token_for_manual_only",
            account_name="매뉴얼 예시 계정",
            unlocker_zone="gm_unlocker",
            country="kr",
        )
        decodo_settings = decodo.DecodoSettings(
            username="sp3demouser",
            password="manual_demo_password",
            country="kr",
        )
    else:
        brightdata_settings = brightdata.BrightDataSettings()
        decodo_settings = decodo.DecodoSettings()

    # These patches ensure no existing output/*.json credentials are touched.
    with patch.object(brightdata, "load_settings", return_value=brightdata_settings), \
            patch.object(decodo, "load_settings", return_value=decodo_settings):
        from app.ui.main_window import MainWindow

        app = QApplication.instance() or QApplication(sys.argv)
        window = MainWindow()
        window.resize(1100, 900)
        window.show()
        window.tab_widget.setCurrentWidget(window.brightdata_panel)

        panel = window.brightdata_panel
        if example:
            # Use clearly fake values so the screenshot can never expose a real key.
            panel.edit_token.setText(brightdata_settings.api_token)
            panel.edit_account_name.setText(brightdata_settings.account_name)
            panel.edit_unlocker_zone.setText(brightdata_settings.unlocker_zone)
            panel.edit_country.setText(brightdata_settings.country)
            panel.edit_decodo_user.setText(decodo_settings.username)
            panel.edit_decodo_password.setText(decodo_settings.password)
            panel._update_banner()
            panel.lbl_decodo_saved.setText(
                f"저장된 계정: {decodo_settings.username}"
            )
            panel._set_decodo_ready(
                "예시 화면 — 실제 저장·연결 검사는 실행하지 않았습니다."
            )
        else:
            panel.edit_token.clear()
            panel.edit_account_name.clear()
            panel.edit_unlocker_zone.clear()
            panel.edit_country.clear()
            panel.edit_decodo_user.clear()
            panel.edit_decodo_password.clear()
            panel._update_banner()
            panel.lbl_decodo_saved.setText("Decodo 계정이 저장되지 않았습니다.")
            panel._set_decodo_ready("사용 준비는 계정 저장 버튼을 눌러 확인하세요.")

        app.processEvents()
        window.grab().save(str(output), "PNG")
        if example:
            from PyQt6.QtWidgets import QGroupBox

            group_boxes = {
                box.title(): box
                for box in panel.findChildren(QGroupBox)
            }
            group_boxes[
                "계정 — Web Unlocker (Gmarket 카테고리 탭)"
            ].grab().save(str(output.parent / "settings-brightdata-fields.png"), "PNG")
            group_boxes[
                "Decodo — Coupang 카테고리 탭"
            ].grab().save(str(output.parent / "settings-decodo-fields.png"), "PNG")
        window.close()
        app.processEvents()


def main() -> int:
    parser = argparse.ArgumentParser(description="설정 탭 화면 캡처")
    parser.add_argument(
        "--example",
        action="store_true",
        help="문서용 가짜 입력값을 채운 화면을 캡처",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="저장할 PNG 경로 (기본값: platform-setup/settings-*.png)",
    )
    args = parser.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    output = args.output or OUT_DIR / (
        "settings-example.png" if args.example else "settings-empty.png"
    )
    _capture(args.example, output)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
