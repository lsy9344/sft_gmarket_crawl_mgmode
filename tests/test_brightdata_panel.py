"""Bright Data 설정 탭 패널 UI 테스트 — 위젯 연동·저장·검증 버튼 (offscreen).

API 호출은 QThread 워커로 실행되므로 검증 버튼 테스트는 _settle() 로 워커
종료를 기다린 뒤 상태 라벨을 단정한다. 네트워크는 전부 mock 으로 대체한다.

PyQt6 가 없으면 전체 skip(다른 PyQt6 테스트 모듈과 동일 가드).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtWidgets import QApplication

    _QT_OK = True
except Exception:  # noqa: BLE001 - PyQt6 미설치 환경
    _QT_OK = False

if _QT_OK:
    from PyQt6.QtWidgets import QMessageBox

    from app.core import brightdata
    from app.ui.widgets import brightdata_panel as panel_module
    from app.ui.widgets.brightdata_panel import BrightDataPanel

_app = None


def setUpModule():
    global _app
    if _QT_OK:
        _app = QApplication.instance() or QApplication(sys.argv)


@unittest.skipUnless(_QT_OK, "PyQt6 필요")
class BrightDataPanelTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "brightdata_settings.json")
        # 패널은 기본 경로를 쓰므로 테스트에서 임시 경로로 격리한다.
        self._path_patch = mock.patch.object(
            brightdata, "default_settings_path", lambda: self.path)
        self._path_patch.start()
        self.panel = BrightDataPanel()

    def tearDown(self):
        self._path_patch.stop()
        self._tmp.cleanup()

    def _settle(self):
        """실행 중인 API 워커가 끝나고 시그널이 전달될까지 기다린다."""
        worker = self.panel._worker
        if worker is not None:
            self.assertTrue(worker.wait(10000))
        QApplication.processEvents()

    # ── 저장·로드 ─────────────────────────────────────────────────────
    def test_save_writes_settings_file(self):
        self.panel.edit_token.setText("tok123")
        self.panel.edit_unlocker_zone.setText("my_zone")
        self.panel.edit_country.setText("kr")
        self.panel.chk_isp_enabled.setChecked(True)
        self.panel.edit_customer_id.setText("hl_x")
        self.panel.edit_isp_zone.setText("gm_isp")
        self.panel.edit_isp_password.setText("pw")
        # 새 토큰이 입력된 저장은 먼저 검증한다 — 성공 응답으로 스텁.
        with mock.patch.object(brightdata, "fetch_balance",
                               lambda token, timeout=15, session=None:
                               {"balance": 9.0, "pending_balance": 0.0}):
            self.panel.on_save()
            self._settle()

        self.assertTrue(os.path.exists(self.path))
        with open(self.path, encoding="utf-8") as f:
            raw = json.load(f)
        self.assertEqual(raw["api_token"], "tok123")
        self.assertEqual(raw["unlocker_zone"], "my_zone")
        self.assertTrue(raw["isp_enabled"])
        self.assertIn("검증 완료", self.panel.lbl_status.text())

    def test_blank_token_preserves_stored_value_on_save(self):
        brightdata.save_settings(
            brightdata.BrightDataSettings(api_token="stored-tok",
                                          isp_password="stored-pw"),
            self.path)
        self.panel.load_from_settings()
        # 토큰·비밀번호 위젯은 마스크만 보여주므로 비어 있다 — 새 입력 없이 저장하면
        # 기존 값이 유지돼야 한다. 빈 토큰 저장은 검증 없이 동기 실행된다.
        self.assertEqual(self.panel.edit_token.text(), "")
        with mock.patch.object(brightdata, "fetch_balance") as fake_verify:
            self.panel.on_save()
        fake_verify.assert_not_called()
        stored = brightdata.load_settings(self.path)
        self.assertEqual(stored.api_token, "stored-tok")
        self.assertEqual(stored.isp_password, "stored-pw")

    def test_save_rejected_token_confirm_no_cancels(self):
        self.panel.edit_token.setText("bad-tok")

        def _reject(token, timeout=15, session=None):
            raise brightdata.BrightDataAPIError(
                "토큰이 거부됐습니다 (HTTP 401)", status=401)

        with mock.patch.object(brightdata, "fetch_balance", _reject), \
                mock.patch.object(panel_module.QMessageBox, "question",
                                  return_value=QMessageBox.StandardButton.No):
            self.panel.on_save()
            self._settle()
        self.assertFalse(os.path.exists(self.path))  # 거부 + 아니오 → 저장 안 됨
        self.assertIn("취소", self.panel.lbl_status.text())

    def test_save_rejected_token_confirm_yes_saves(self):
        self.panel.edit_token.setText("bad-tok")

        def _reject(token, timeout=15, session=None):
            raise brightdata.BrightDataAPIError(
                "토큰이 거부됐습니다 (HTTP 401)", status=401)

        with mock.patch.object(brightdata, "fetch_balance", _reject), \
                mock.patch.object(panel_module.QMessageBox, "question",
                                  return_value=QMessageBox.StandardButton.Yes):
            self.panel.on_save()
            self._settle()
        self.assertTrue(os.path.exists(self.path))  # 사용자 확인으로 저장
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["api_token"], "bad-tok")

    def test_save_network_error_saves_as_unverified(self):
        self.panel.edit_token.setText("tok123")

        def _network_down(token, timeout=15, session=None):
            raise brightdata.BrightDataAPIError("Bright Data API 연결 실패")

        with mock.patch.object(brightdata, "fetch_balance", _network_down):
            self.panel.on_save()
            self._settle()
        # 판정 불가 오류 — 저장은 진행되되 미검증임을 명시한다
        self.assertTrue(os.path.exists(self.path))
        self.assertIn("미검증", self.panel.lbl_status.text())

    def test_save_warns_incomplete_isp_credentials(self):
        self.panel.edit_token.setText("tok123")
        self.panel.chk_isp_enabled.setChecked(True)
        # 계정 ID/존/비밀번호 비움 → 저장은 되지만 경고가 표시된다
        with mock.patch.object(brightdata, "fetch_balance",
                               lambda token, timeout=15, session=None:
                               {"balance": 9.0, "pending_balance": 0.0}):
            self.panel.on_save()
            self._settle()
        self.assertTrue(os.path.exists(self.path))
        self.assertIn("비어", self.panel.lbl_status.text())
        self.assertIn("프록시 없이", self.panel.lbl_status.text())

    def test_load_from_settings_fills_widgets(self):
        brightdata.save_settings(
            brightdata.BrightDataSettings(
                api_token="tok123", unlocker_zone="z1", country="kr",
                isp_enabled=True, isp_customer_id="hl_x", isp_zone="z2",
                isp_password="pw123456"),
            self.path)
        panel = BrightDataPanel()
        self.assertEqual(panel.edit_token.text(), "")  # 토큰은 마스크 복원
        self.assertIn("tok123"[-4:], panel.edit_token.placeholderText())
        self.assertEqual(panel.edit_unlocker_zone.text(), "z1")
        self.assertEqual(panel.edit_country.text(), "kr")
        self.assertTrue(panel.chk_isp_enabled.isChecked())
        self.assertEqual(panel.edit_customer_id.text(), "hl_x")
        self.assertEqual(panel.edit_isp_zone.text(), "z2")
        self.assertEqual(panel.edit_isp_password.text(), "")
        self.assertIn("pw123456"[-4:], panel.edit_isp_password.placeholderText())

    # ── 잔액 조회·존 비밀번호 가져오기 ─────────────────────────────────
    def test_balance_button_success(self):
        self.panel.edit_token.setText("tok123")
        with mock.patch.object(brightdata, "fetch_balance",
                               lambda token, timeout=15, session=None:
                               {"balance": 5.84, "pending_balance": 0.0}):
            self.panel.on_check_balance()
            self._settle()
        self.assertIn("$5.84", self.panel.lbl_status.text())
        self.assertIn("tok123"[-4:], self.panel.lbl_status.text())
        # 완료 후 버튼 잠금 해제
        self.assertTrue(self.panel.btn_balance.isEnabled())

    def test_balance_button_rejected_token(self):
        self.panel.edit_token.setText("bad")

        def _raise(token, timeout=15, session=None):
            raise brightdata.BrightDataAPIError("토큰이 거부됐습니다 (HTTP 401)",
                                                status=401)

        with mock.patch.object(brightdata, "fetch_balance", _raise):
            self.panel.on_check_balance()
            self._settle()
        self.assertIn("실패", self.panel.lbl_status.text())
        self.assertIn("401", self.panel.lbl_status.text())

    def test_balance_button_empty_token_no_network(self):
        self.panel.edit_token.setText("  ")
        with mock.patch.object(brightdata, "fetch_balance") as fake:
            self.panel.on_check_balance()
        fake.assert_not_called()
        self.assertIn("입력", self.panel.lbl_status.text())

    def test_fetch_zone_password_fills_field(self):
        self.panel.edit_token.setText("tok123")
        self.panel.edit_isp_zone.setText("gm_isp")
        with mock.patch.object(brightdata, "fetch_zone_password",
                               lambda token, zone, timeout=15, session=None: "pw9"):
            self.panel.on_fetch_zone_password()
            self._settle()
        self.assertEqual(self.panel.edit_isp_password.text(), "pw9")

    def test_fetch_zone_password_requires_zone(self):
        self.panel.edit_token.setText("tok123")
        self.panel.edit_isp_zone.setText("")
        with mock.patch.object(brightdata, "fetch_zone_password") as fake:
            self.panel.on_fetch_zone_password()
        fake.assert_not_called()

    # ── ISP 프록시 테스트 ──────────────────────────────────────────────
    def test_proxy_test_success_shows_exit_ip(self):
        self.panel.chk_isp_enabled.setChecked(True)
        self.panel.edit_customer_id.setText("hl_x")
        self.panel.edit_isp_zone.setText("gm_isp")
        self.panel.edit_isp_password.setText("pw")
        with mock.patch.object(brightdata, "test_isp_proxy",
                               lambda settings, timeout=20, session=None:
                               {"ip": "31.40.194.114", "country": "kr"}):
            self.panel.on_test_isp_proxy()
            self._settle()
        self.assertIn("31.40.194.114", self.panel.lbl_status.text())
        self.assertIn("KR", self.panel.lbl_status.text())

    def test_proxy_test_requires_enabled_and_complete(self):
        with mock.patch.object(brightdata, "test_isp_proxy") as fake:
            self.panel.chk_isp_enabled.setChecked(False)
            self.panel.on_test_isp_proxy()
            self.assertIn("체크", self.panel.lbl_status.text())
            self.panel.chk_isp_enabled.setChecked(True)
            self.panel.on_test_isp_proxy()
            self.assertIn("모두 입력", self.panel.lbl_status.text())
        fake.assert_not_called()

    def test_proxy_test_auth_failure_shown(self):
        self.panel.chk_isp_enabled.setChecked(True)
        self.panel.edit_customer_id.setText("hl_x")
        self.panel.edit_isp_zone.setText("gm_isp")
        self.panel.edit_isp_password.setText("wrong")

        def _auth_fail(settings, timeout=20, session=None):
            raise brightdata.BrightDataAPIError(
                "프록시 인증 실패(HTTP 407)", status=407)

        with mock.patch.object(brightdata, "test_isp_proxy", _auth_fail):
            self.panel.on_test_isp_proxy()
            self._settle()
        self.assertIn("실패", self.panel.lbl_status.text())
        self.assertIn("407", self.panel.lbl_status.text())

    # ── busy 잠금 ─────────────────────────────────────────────────────
    def test_set_external_busy_locks_widgets(self):
        self.panel.set_external_busy(True)
        self.assertFalse(self.panel.btn_save.isEnabled())
        self.assertFalse(self.panel.btn_test_proxy.isEnabled())
        self.assertFalse(self.panel.edit_token.isEnabled())
        self.panel.set_external_busy(False)
        self.assertTrue(self.panel.btn_save.isEnabled())
        self.assertTrue(self.panel.btn_test_proxy.isEnabled())
        self.assertTrue(self.panel.edit_token.isEnabled())

    # ── collect_settings ──────────────────────────────────────────────
    def test_collect_settings_maps_widgets(self):
        self.panel.edit_token.setText("tok")
        self.panel.edit_unlocker_zone.setText("z")
        self.panel.chk_isp_enabled.setChecked(True)
        s = self.panel.collect_settings()
        self.assertEqual(s.api_token, "tok")
        self.assertEqual(s.unlocker_zone, "z")
        self.assertTrue(s.isp_enabled)


if __name__ == "__main__":
    unittest.main()
