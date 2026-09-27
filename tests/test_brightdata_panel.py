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
    from PyQt6.QtWidgets import QGroupBox, QMessageBox

    from app.core import brightdata, decodo
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
        self.decodo_path = os.path.join(self._tmp.name, "decodo_settings.json")
        self._decodo_path_patch = mock.patch.object(
            decodo, "default_settings_path", lambda: self.decodo_path)
        self._decodo_path_patch.start()
        self.panel = BrightDataPanel()

    def tearDown(self):
        self._decodo_path_patch.stop()
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
        self.panel.edit_account_name.setText("본사 계정")
        self.panel.edit_unlocker_zone.setText("my_zone")
        self.panel.edit_country.setText("kr")
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
        self.assertEqual(raw["account_name"], "본사 계정")
        self.assertEqual(raw["unlocker_zone"], "my_zone")
        self.assertFalse(raw["isp_enabled"])
        self.assertIn("검증 완료", self.panel.lbl_status.text())
        self.assertIn("본사 계정", self.panel.lbl_status.text())

    def test_save_decodo_writes_settings_file(self):
        ready = panel_module._DecodoReadiness(True, "한국 회선 확인 (1.2.3.4)", ())
        with mock.patch.object(panel_module, "_check_decodo_readiness",
                               return_value=ready) as check:
            self.panel.edit_decodo_user.setText("user-sp3id")
            self.panel.edit_decodo_password.setText("secretpw")
            self.panel.on_save_decodo()
            self._settle()
        self.assertEqual(check.call_args.args[0].username, "sp3id")
        self.assertEqual(check.call_args.args[0].password, "secretpw")
        with open(self.decodo_path, encoding="utf-8") as f:
            raw = json.load(f)
        self.assertEqual(raw["username"], "sp3id")
        self.assertEqual(raw["password"], "secretpw")
        self.assertIn("계정 저장 완료", self.panel.lbl_decodo_saved.text())
        self.assertIn("사용 준비 완료", self.panel.lbl_decodo_ready.text())
        self.assertNotIn("secretpw", self.panel.lbl_decodo_ready.text())

    def test_save_decodo_checks_in_worker_without_freezing_ui(self):
        from threading import Event

        started = Event()
        release = Event()

        def slow_check(_settings):
            started.set()
            release.wait(5)
            return panel_module._DecodoReadiness(True, "한국 회선 확인 (1.2.3.4)", ())

        self.panel.edit_decodo_user.setText("user-sp3id")
        self.panel.edit_decodo_password.setText("secretpw")
        with mock.patch.object(panel_module, "_check_decodo_readiness", slow_check):
            try:
                self.panel.on_save_decodo()
                self.assertTrue(started.wait(2))
                QApplication.processEvents()
                self.assertIn("계정 저장 완료", self.panel.lbl_decodo_saved.text())
                self.assertIn("확인 중", self.panel.lbl_decodo_ready.text())
                self.assertFalse(self.panel.btn_save_decodo.isEnabled())
                self.panel.set_external_busy(True)
            finally:
                release.set()
            self._settle()
        self.assertIn("사용 준비 완료", self.panel.lbl_decodo_ready.text())
        self.assertFalse(self.panel.btn_save_decodo.isEnabled())
        self.panel.set_external_busy(False)
        self.assertTrue(self.panel.btn_save_decodo.isEnabled())

    def test_save_decodo_empty_credentials_stays_unready(self):
        with mock.patch.object(panel_module.QMessageBox, "warning"):
            self.panel.on_save_decodo()
        self.assertFalse(os.path.exists(self.decodo_path))
        self.assertIn("수집 준비 안 됨", self.panel.lbl_decodo_ready.text())

    def test_save_decodo_permission_error_shows_writable_folder_hint(self):
        self.panel.edit_decodo_user.setText("sp3id")
        self.panel.edit_decodo_password.setText("secretpw")
        with mock.patch.object(decodo, "save_settings",
                               side_effect=PermissionError("access denied")), \
             mock.patch.object(panel_module.QMessageBox, "critical") as dialog:
            self.panel.on_save_decodo()
        self.assertIn("쓰기 권한", dialog.call_args.args[2])
        self.assertIn("저장 실패", self.panel.lbl_decodo_saved.text())
        self.assertIn("수집 준비 안 됨", self.panel.lbl_decodo_ready.text())

    def test_decodo_check_failure_keeps_save_result_separate(self):
        failed = panel_module._DecodoReadiness(
            False, "Decodo 데이터 사용량이 소진됐습니다(HTTP 407).", ())
        self.panel.edit_decodo_user.setText("sp3id")
        self.panel.edit_decodo_password.setText("secretpw")
        with mock.patch.object(panel_module, "_check_decodo_readiness",
                               return_value=failed):
            self.panel.on_save_decodo()
            self._settle()
        self.assertIn("계정 저장 완료", self.panel.lbl_decodo_saved.text())
        self.assertIn("수집 준비 안 됨", self.panel.lbl_decodo_ready.text())
        self.assertIn("사용량", self.panel.lbl_decodo_ready.text())

    def test_decodo_readiness_requires_korean_exit(self):
        from app.core.coupang.preflight import PreflightResult, PreflightStatus
        from app.core.gmarket_preflight import GmarketPreflightResult

        settings = decodo.DecodoSettings(username="sp3id", password="secretpw")
        with mock.patch.object(decodo, "fetch_exit_ip", return_value=decodo.ExitIpInfo(
                ip="14.0.0.1", country_code="VN")), \
             mock.patch("app.core.coupang.preflight.check_runtime",
                        return_value=PreflightResult(PreflightStatus.OK, "준비 완료")), \
             mock.patch("app.core.gmarket_preflight.check_gmarket_runtime",
                        return_value=GmarketPreflightResult(True, "준비 완료")):
            result = panel_module._check_decodo_readiness(settings)
        self.assertFalse(result.connection_ok)
        self.assertIn("한국 회선이 아닙니다", result.connection_message)
        self.assertEqual(result.runtime_errors, ())

    def test_decodo_readiness_checks_camoufox_geoip_and_patchright(self):
        from app.core.coupang.preflight import PreflightResult, PreflightStatus
        from app.core.gmarket_preflight import GmarketPreflightResult

        settings = decodo.DecodoSettings(username="sp3id", password="secretpw")
        with mock.patch.object(decodo, "fetch_exit_ip", return_value=decodo.ExitIpInfo(
                ip="1.2.3.4", country_code="KR")), \
             mock.patch("app.core.coupang.preflight.check_runtime",
                        return_value=PreflightResult(
                            PreflightStatus.GEOIP_MISSING, "GeoIP 파일이 없습니다.")), \
             mock.patch("app.core.gmarket_preflight.check_gmarket_runtime",
                        return_value=GmarketPreflightResult(
                            False, "Patchright 브라우저가 없습니다.")):
            result = panel_module._check_decodo_readiness(settings)
        self.assertTrue(result.connection_ok)
        self.assertEqual(len(result.runtime_errors), 2)
        self.assertIn("GeoIP", result.runtime_errors[0])
        self.assertIn("Patchright", result.runtime_errors[1])
        self.panel._on_decodo_readiness(result)
        self.assertIn("수집 준비 안 됨", self.panel.lbl_decodo_ready.text())
        self.assertIn("SellerCollector.exe --setup-runtime",
                      self.panel.lbl_decodo_ready.text())

    def test_banner_warns_without_token(self):
        # 토큰이 없으면 눈에 띄는 경고 배너 — 수집 시작이 막힌다는 사실을 명시
        panel = BrightDataPanel()
        self.assertIn("필요합니다", panel.banner.text())
        self.assertIn("시작되지", panel.banner.text())

    def test_banner_shows_saved_account_by_name(self):
        # 토큰 저장 후에는 계정 이름으로 구분되는 확인 배너로 바뀐다
        brightdata.save_settings(
            brightdata.BrightDataSettings(api_token="tok123",
                                          account_name="본사 계정"),
            self.path)
        panel = BrightDataPanel()
        self.assertIn("설정됨", panel.banner.text())
        self.assertIn("본사 계정", panel.banner.text())
        self.assertIn("tok123"[-4:], panel.banner.text())

    def test_banner_updates_after_token_typed(self):
        # 저장 전이라도 토큰을 입력하면 배너가 경고 → 확인 상태로 바뀐다
        self.panel.edit_token.setText("newtok")
        self.panel.edit_token.editingFinished.emit()
        self.assertIn("설정됨", self.panel.banner.text())

    def test_blank_token_preserves_stored_value_on_save(self):
        brightdata.save_settings(
            brightdata.BrightDataSettings(api_token="stored-tok",
                                          isp_enabled=True,
                                          isp_customer_id="hl_x",
                                          isp_zone="gm_isp",
                                          isp_password="stored-pw"),
            self.path)
        self.panel.load_from_settings()
        # 화면에서 제거한 ISP 값과 마스크된 토큰은 다른 설정을 저장해도 유지된다.
        self.assertEqual(self.panel.edit_token.text(), "")
        with mock.patch.object(brightdata, "fetch_balance") as fake_verify:
            self.panel.on_save()
        fake_verify.assert_not_called()
        stored = brightdata.load_settings(self.path)
        self.assertEqual(stored.api_token, "stored-tok")
        self.assertTrue(stored.isp_enabled)
        self.assertEqual(stored.isp_customer_id, "hl_x")
        self.assertEqual(stored.isp_zone, "gm_isp")
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

    def test_load_from_settings_fills_widgets(self):
        brightdata.save_settings(
            brightdata.BrightDataSettings(
                api_token="tok123", account_name="부산 지점", unlocker_zone="z1",
                country="kr"),
            self.path)
        panel = BrightDataPanel()
        self.assertEqual(panel.edit_token.text(), "")  # 토큰은 마스크 복원
        self.assertIn("tok123"[-4:], panel.edit_token.placeholderText())
        self.assertEqual(panel.edit_account_name.text(), "부산 지점")
        self.assertEqual(panel.edit_unlocker_zone.text(), "z1")
        self.assertEqual(panel.edit_country.text(), "kr")
        self.assertFalse(any("ISP 프록시" in group.title()
                             for group in panel.findChildren(QGroupBox)))

    # ── 잔액 조회 ─────────────────────────────────────────────────────
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

    # ── busy 잠금 ─────────────────────────────────────────────────────
    def test_set_external_busy_locks_widgets(self):
        self.panel.set_external_busy(True)
        self.assertFalse(self.panel.btn_save.isEnabled())
        self.assertFalse(self.panel.btn_open_token_page.isEnabled())
        self.assertFalse(self.panel.edit_account_name.isEnabled())
        self.assertFalse(self.panel.edit_token.isEnabled())
        self.panel.set_external_busy(False)
        self.assertTrue(self.panel.btn_save.isEnabled())
        self.assertTrue(self.panel.btn_open_token_page.isEnabled())
        self.assertTrue(self.panel.edit_token.isEnabled())

    # ── collect_settings ──────────────────────────────────────────────
    def test_collect_settings_maps_widgets(self):
        self.panel.edit_token.setText("tok")
        self.panel.edit_account_name.setText("별칭")
        self.panel.edit_unlocker_zone.setText("z")
        s = self.panel.collect_settings()
        self.assertEqual(s.api_token, "tok")
        self.assertEqual(s.account_name, "별칭")
        self.assertEqual(s.unlocker_zone, "z")
        self.assertFalse(s.isp_enabled)


if __name__ == "__main__":
    unittest.main()
