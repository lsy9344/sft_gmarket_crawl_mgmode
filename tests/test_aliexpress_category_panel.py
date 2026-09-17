"""AliExpress 카테고리 패널 UI 및 워커 테스트.

UI UX Pro Max 원칙을 반영한 AliExpress 카테고리 탭의 위젯 구성,
트리 탐색, 상태 전이, 메트릭 대시보드 갱신, cross-tab 잠금 등을 검증한다.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtWidgets import QApplication

    _QT_OK = True
except Exception:  # noqa: BLE001
    _QT_OK = False

if _QT_OK:
    from PyQt6.QtWidgets import QMessageBox

    from app.core import decodo
    from app.core.aliexpress_category_crawler import (
        AliexpressCategoryCrawler,
        AliexpressCategoryRunConfig,
        AliexpressCrawlSummary,
    )
    from app.core.base import CancelledError, Control
    from app.ui.aliexpress_category_panel import AliexpressCategoryPanel
    from app.workers.aliexpress_category_crawl_worker import AliexpressCategoryCrawlWorker

_app = None


def setUpModule():
    global _app
    if _QT_OK:
        _app = QApplication.instance() or QApplication(sys.argv)


@unittest.skipUnless(_QT_OK, "PyQt6 필요")
class AliexpressCategoryPanelTest(unittest.TestCase):
    def setUp(self):
        self.panel = AliexpressCategoryPanel()

    def tearDown(self):
        self.panel.deleteLater()

    def test_initial_state(self):
        """초기 상태(idle)에서 UI 위젯들이 올바르게 활성화되어 있어야 한다."""
        self.assertEqual(self.panel._state, "idle")
        self.assertTrue(self.panel.btn_start.isEnabled())
        self.assertFalse(self.panel.btn_pause.isEnabled())
        self.assertFalse(self.panel.btn_resume.isEnabled())
        self.assertFalse(self.panel.btn_cancel.isEnabled())
        self.assertTrue(self.panel.spin_max_pages.isEnabled())
        self.assertTrue(self.panel.spin_rotation_batch.isEnabled())
        self.assertTrue(self.panel.category_tree.topLevelItemCount() > 0)

    def test_state_transitions(self):
        """수집 상태 전이에 따른 버튼 제어 상태 검증."""
        # 1. running
        self.panel.set_state("running")
        self.assertFalse(self.panel.btn_start.isEnabled())
        self.assertTrue(self.panel.btn_pause.isEnabled())
        self.assertFalse(self.panel.btn_resume.isEnabled())
        self.assertTrue(self.panel.btn_cancel.isEnabled())
        self.assertFalse(self.panel.spin_max_pages.isEnabled())

        # 2. paused
        self.panel.set_state("paused")
        self.assertFalse(self.panel.btn_start.isEnabled())
        self.assertFalse(self.panel.btn_pause.isEnabled())
        self.assertTrue(self.panel.btn_resume.isEnabled())
        self.assertTrue(self.panel.btn_cancel.isEnabled())

        # 3. finished (다시 idle로 복구 가능)
        self.panel.set_state("idle")
        self.assertTrue(self.panel.btn_start.isEnabled())
        self.assertFalse(self.panel.btn_pause.isEnabled())

    def test_external_busy(self):
        """다른 탭 수집 실행 시 외부 잠금 및 해제 동작 검증."""
        self.panel.set_external_busy(True)
        self.assertFalse(self.panel.btn_start.isEnabled())
        self.assertFalse(self.panel.spin_max_pages.isEnabled())
        self.assertFalse(self.panel.category_tree.isEnabled())

        self.panel.set_external_busy(False)
        self.assertTrue(self.panel.btn_start.isEnabled())
        self.assertTrue(self.panel.spin_max_pages.isEnabled())
        self.assertTrue(self.panel.category_tree.isEnabled())

    def test_add_record_and_update_stats(self):
        """실시간 상품 수집 레코드 테이블 추가 및 메트릭 카드 갱신 검증."""
        sample_record = {
            "crawl_timestamp": "2026-09-17 19:50:00",
            "item_id": "1005001234567890",
            "item_title": "신선 국내산 양파 3kg",
            "company_name": "(주)신선푸드",
            "ceo_name": "홍길동",
            "business_number": "123-45-67890",
            "phone_number": "02-1234-5678",
            "email": "fresh@food.co.kr",
            "address": "서울시 강남구 테헤란로 123",
            "item_url": "https://ko.aliexpress.com/item/1005001234567890.html",
        }
        self.panel.add_record(sample_record)
        self.assertEqual(self.panel.result_table.rowCount(), 1)
        self.assertEqual(self.panel.result_table.item(0, 0).text(), "(주)신선푸드")
        self.assertEqual(self.panel.result_table.item(0, 3).text(), "fresh@food.co.kr")

        summary = AliexpressCrawlSummary(
            total_items=120,
            collected_items=120,
            unique_vendors=45,
            has_email=110,
            has_business_number=118,
            has_ceo_name=115,
        )
        self.panel.update_stats(summary)
        self.assertEqual(self.panel.card_total.findChild(type(self.panel.label_progress), "value_label").text(), "120건")
        self.assertEqual(self.panel.card_vendors.findChild(type(self.panel.label_progress), "value_label").text(), "45개사")

    def test_start_signal_emission(self):
        """시작 버튼 클릭 시 유효한 AliexpressCategoryRunConfig가 방출되는지 검증."""
        captured_cfg: list[AliexpressCategoryRunConfig] = []
        self.panel.start_requested.connect(lambda cfg: captured_cfg.append(cfg))

        # 직접 URL 탭을 통해 수집 대상 설정
        self.panel.input_tabs.setCurrentIndex(1)
        self.panel.input_direct_name.setText("직접입력 카테고리")
        self.panel.input_direct_url.setText("https://ko.aliexpress.com/w/wholesale-test.html")

        self.panel._on_start_clicked()
        self.assertEqual(len(captured_cfg), 1)
        cfg = captured_cfg[0]
        self.assertEqual(cfg.category_name, "직접입력 카테고리")
        self.assertEqual(cfg.category_url, "https://ko.aliexpress.com/w/wholesale-test.html")
        self.assertEqual(cfg.max_pages, 30)
        self.assertEqual(cfg.rotation_batch_size, 25)

    def test_dynamic_label_updates(self):
        """탭 전환 및 직접 입력 시 라벨 텍스트가 실시간 갱신되는지 검증."""
        # 1. 트리 탭: 초기 로드 시 첫 번째 소분류 반영
        self.panel.input_tabs.setCurrentIndex(0)
        self.assertIn("선택된 카테고리", self.panel.selected_target_label.text())

        # 2. 직접 입력 탭으로 전환
        self.panel.input_tabs.setCurrentIndex(1)
        self.assertIn("직접 입력 대상", self.panel.selected_target_label.text())

        # 3. 직접 입력 텍스트 변경
        self.panel.input_direct_name.setText("프리미엄 견과류")
        self.assertIn("프리미엄 견과류", self.panel.selected_target_label.text())


    def test_finished_and_failed_states_reenable_start(self):
        """완료·실패 종료 상태에서 시작 버튼과 설정 위젯이 다시 활성화되어야 한다.

        IP 차단으로 중단된 뒤에도 우회 회선을 골라 다시 시작할 수 있어야 하므로
        finished/failed 는 idle 과 같은 '다시 시작 가능' 상태다(쿠팡 탭 동일 규율).
        """
        for state in ("finished", "failed"):
            self.panel.set_state(state)
            self.assertTrue(
                self.panel.btn_start.isEnabled(),
                f"{state} 상태에서 시작 버튼이 다시 활성화되어야 합니다",
            )
            self.assertTrue(self.panel.spin_max_pages.isEnabled())
            self.assertTrue(self.panel.spin_rotation_batch.isEnabled())
            self.assertFalse(self.panel.btn_pause.isEnabled())
            self.assertFalse(self.panel.btn_resume.isEnabled())
            self.assertFalse(self.panel.btn_cancel.isEnabled())

        # cancelling 은 종료 상태가 아니므로 시작·폴더 열기 모두 잠긴다
        self.panel.set_state("cancelling")
        self.assertFalse(self.panel.btn_start.isEnabled())
        self.assertFalse(self.panel.btn_open_folder.isEnabled())
        self.panel.set_state("idle")

    def test_open_output_button_emits_typed_path(self):
        """📁 결과 폴더 열기 버튼이 입력된 경로 그대로 open_output_requested(str) 을 방출하는지 검증."""
        emitted: list[str] = []
        self.panel.open_output_requested.connect(lambda p: emitted.append(p))
        self.panel.edit_output_dir.setText("/tmp/ali_open_folder_test")
        self.panel.btn_open_folder.click()

        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0], "/tmp/ali_open_folder_test")

    def test_open_output_button_without_path_does_not_emit(self):
        """저장 폴더가 비어 있을 때는 시그널을 방출하지 않고 안내 로그만 남기는지 검증."""
        emitted: list[str] = []
        self.panel.open_output_requested.connect(lambda p: emitted.append(p))
        self.panel.edit_output_dir.setText("   ")
        self.panel.btn_open_folder.click()

        self.assertEqual(len(emitted), 0)
        self.assertIn("[폴더 열기 오류]", self.panel.log_text.toPlainText())

    def test_start_click_does_not_clear_results(self):
        """시작 클릭 시 패널이 결과 테이블을 비우지 않아야 한다.

        결과 화면 비우기는 시작이 수락된 뒤 main_window 가 담당하므로,
        시작 대화상자에서 취소하면 이전 결과가 보존된다.
        """
        self.panel.add_record({"company_name": "(주)보존", "email": "keep@x.com"})
        self.panel.input_tabs.setCurrentIndex(1)
        self.panel.input_direct_name.setText("보존테스트")
        self.panel.input_direct_url.setText("https://ko.aliexpress.com/w/keep.html")

        self.panel._on_start_clicked()
        self.assertEqual(
            self.panel.result_table.rowCount(), 1,
            "시작 클릭만으로 결과 테이블이 비워져서는 안 됩니다",
        )

    def test_category_tree_fallback_when_missing(self):
        """카테고리 트리 파일이 없을 때 안내 문구를 출력하고 직접 URL 입력 탭으로 자동 전환해야 한다."""
        from unittest.mock import patch
        with patch("pathlib.Path.exists", return_value=False):
            self.panel._load_category_tree()
            self.assertEqual(self.panel.input_tabs.currentIndex(), 1)
            log_text = self.panel.log_text.toPlainText()
            self.assertIn("[안내] 기본 카테고리 트리를 불러올 수 없습니다. '직접 URL 입력' 탭을 이용해 주세요.", log_text)

    def test_category_tree_fallback_when_corrupted(self):
        """카테고리 트리 파일이 손상되었거나 파싱 실패 시 안내 문구를 출력하고 직접 URL 입력 탭으로 전환해야 한다."""
        from unittest.mock import patch
        with patch("builtins.open", side_effect=ValueError("JSON 손상")), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.is_file", return_value=True):
            self.panel._load_category_tree()
            self.assertEqual(self.panel.input_tabs.currentIndex(), 1)
            log_text = self.panel.log_text.toPlainText()
            self.assertIn("[안내] 기본 카테고리 트리를 불러올 수 없습니다. '직접 URL 입력' 탭을 이용해 주세요.", log_text)


@unittest.skipUnless(_QT_OK, "PyQt6 필요")
class MainWindowAliExpressIntegrationTest(unittest.TestCase):
    def setUp(self):
        from app.ui.main_window import MainWindow
        self.win = MainWindow()

    def tearDown(self):
        self.win.close()

    def test_preflight_blocks_when_decodo_missing(self):
        """Decodo 자격 증명이 누락된 경우 3지선다 대화상자(취소, 설정 이동, 로컬 회선 안전 수집)를 검증한다."""
        from unittest.mock import patch
        from PyQt6.QtWidgets import QMessageBox
        from app.core.decodo import DecodoSettings

        cfg = AliexpressCategoryRunConfig(
            output_dir=Path("/tmp/ali_test_out"),
            category_name="테스트",
            category_url="https://ko.aliexpress.com/w/wholesale-test.html",
        )

        def make_simulator(choice_text: str):
            def fake_exec(box_self):
                self.assertEqual(box_self.windowTitle(), "수집 방식 선택")
                self.assertIn("Decodo 프록시 계정이 설정되어 있지 않습니다", box_self.text())
                for btn in box_self.buttons():
                    if choice_text in btn.text():
                        box_self._mock_choice = btn
                        return
                box_self._mock_choice = None
            return fake_exec

        # 1. '취소' 선택 시 -> 워커 미시작
        with patch("app.core.decodo.load_settings", return_value=DecodoSettings(username="", password="")), \
             patch.object(QMessageBox, "exec", make_simulator("취소")), \
             patch.object(QMessageBox, "clickedButton", lambda b: getattr(b, "_mock_choice", None)):
            self.win.on_alicat_start(cfg)
            self.assertIsNone(self.win.alicat_worker)

        # 2. '설정 탭으로 이동' 선택 시 -> 워커 미시작 & 설정 탭으로 전환
        with patch("app.core.decodo.load_settings", return_value=DecodoSettings(username="", password="")), \
             patch.object(QMessageBox, "exec", make_simulator("설정 탭으로 이동")), \
             patch.object(QMessageBox, "clickedButton", lambda b: getattr(b, "_mock_choice", None)):
            self.win.on_alicat_start(cfg)
            self.assertIsNone(self.win.alicat_worker)
            self.assertEqual(self.win.tab_widget.currentWidget(), self.win.brightdata_panel)

        # 3. '로컬 회선으로 안전 수집' 선택 시 -> use_proxy=False, delay=3.5s 워커 기동
        with patch("app.core.decodo.load_settings", return_value=DecodoSettings(username="", password="")), \
             patch.object(QMessageBox, "exec", make_simulator("로컬 회선으로 안전 수집")), \
             patch.object(QMessageBox, "clickedButton", lambda b: getattr(b, "_mock_choice", None)), \
             patch.object(AliexpressCategoryCrawlWorker, "start"):
            self.win.on_alicat_start(cfg)
            self.assertIsNotNone(self.win.alicat_worker)
            self.assertFalse(self.win.alicat_worker.config.use_proxy)
            self.assertEqual(self.win.alicat_worker.config.delay, 3.5)
            self.win.alicat_worker = None

    def test_resume_mode_choices(self):
        """미완료 진행 기록이 감지되었을 때 이어서/새로/취소 분기를 검증한다."""
        from unittest.mock import patch
        from app.core.aliexpress_resume_store import ali_category_run_dir, AliexpressResumeStore

        out_dir = Path("/tmp/ali_resume_mw_test")
        cfg = AliexpressCategoryRunConfig(
            output_dir=out_dir,
            category_name="재개테스트",
            category_url="https://ko.aliexpress.com/category/555/test.html",
        )
        run_dir = ali_category_run_dir(out_dir, cfg.category_name, cfg.category_url)
        run_dir.mkdir(parents=True, exist_ok=True)
        store = AliexpressResumeStore(run_dir)
        store.open()
        store.record_page(1, [{"id": "item1", "title": "상품1"}])
        store.close()

        # 1. 취소
        with patch("app.core.decodo.load_settings"), \
             patch("app.core.decodo.credentials_ready", return_value=True), \
             patch.object(self.win, "_ask_resume_mode", return_value="cancel"):
            self.win.on_alicat_start(cfg)
            self.assertIsNone(self.win.alicat_worker)

        # 2. 이어서 수집 (start_fresh=False)
        with patch("app.core.decodo.load_settings"), \
             patch("app.core.decodo.credentials_ready", return_value=True), \
             patch.object(self.win, "_ask_resume_mode", return_value="resume"), \
             patch.object(AliexpressCategoryCrawlWorker, "start"):
            self.win.on_alicat_start(cfg)
            self.assertIsNotNone(self.win.alicat_worker)
            self.assertFalse(self.win.alicat_worker.config.start_fresh)
            self.win.alicat_worker = None

        # 3. 처음부터 다시 수집 (start_fresh=True)
        with patch("app.core.decodo.load_settings"), \
             patch("app.core.decodo.credentials_ready", return_value=True), \
             patch.object(self.win, "_ask_resume_mode", return_value="fresh"), \
             patch.object(AliexpressCategoryCrawlWorker, "start"):
            self.win.on_alicat_start(cfg)
            self.assertIsNotNone(self.win.alicat_worker)
            self.assertTrue(self.win.alicat_worker.config.start_fresh)
            self.win.alicat_worker = None

    def test_start_acceptance_clears_results_only_after_acceptance(self):
        """결과 화면 비우기가 시작 수락 이후에만 일어나는지 검증.

        시작 대화상자에서 취소하면 이전 결과가 보존되고, 시작이 수락되어
        워커가 기동될 때 비워진다(쿠팡 탭과 동일한 규율).
        """
        panel = self.win.aliexpress_category_panel
        panel.add_record({"company_name": "(주)이전결과", "email": "old@x.com"})
        cfg = AliexpressCategoryRunConfig(
            output_dir=Path("/tmp/ali_test_out"),
            category_name="화면정리",
            category_url="https://ko.aliexpress.com/w/clear.html",
        )

        # 1. 수집 방식 대화상자에서 취소 → 워커 미기동, 이전 결과 보존
        with patch("app.core.decodo.load_settings", return_value=decodo.DecodoSettings(username="", password="")), \
             patch.object(QMessageBox, "exec", lambda b: None), \
             patch.object(QMessageBox, "clickedButton", lambda b: None):
            self.win.on_alicat_start(cfg)
            self.assertIsNone(self.win.alicat_worker)
            self.assertEqual(panel.result_table.rowCount(), 1, "취소 시 이전 결과가 보존되어야 합니다")

        # 2. 시작 수락(자격 증명 있음) → 워커 기동과 함께 결과 화면 정리
        with patch("app.core.decodo.load_settings"), \
             patch("app.core.decodo.credentials_ready", return_value=True), \
             patch.object(AliexpressCategoryCrawlWorker, "start"):
            self.win.on_alicat_start(cfg)
            self.assertIsNotNone(self.win.alicat_worker)
            self.assertEqual(panel.result_table.rowCount(), 0, "시작 수락 후 결과 화면이 비워져야 합니다")
            self.win.alicat_worker = None

    def test_finished_blocked_summary_shows_warning_and_failed_state(self):
        """차단(blocked) 요약 종료 시 IP 차단 의심 경고 대화상자와 failed 상태 전환 검증.

        소프트 차단으로 안전 중단된 실행은 재개 안내가 담긴 warning 다이얼로그를
        보여야 하며, 패널은 failed 상태로 전환돼 다시 시작할 수 있어야 한다.
        """
        summary = AliexpressCrawlSummary(
            collected_items=7,
            unique_vendors=3,
            has_email=5,
            has_business_number=6,
            termination_reason="blocked",
            error="IP 차단이 해소되지 않아 수집을 중단했습니다. 다시 시작할 때 우회 회선(Decodo)을 선택하세요.",
        )
        with patch.object(QMessageBox, "warning") as mock_warn:
            self.win._on_alicat_finished(summary)

        mock_warn.assert_called_once()
        self.assertIn("IP 차단", mock_warn.call_args.args[1])
        self.assertEqual(self.win.aliexpress_category_panel._state, "failed")
        self.assertTrue(
            self.win.aliexpress_category_panel.btn_start.isEnabled(),
            "차단 중단 뒤에도 다시 시작할 수 있어야 합니다",
        )

    def test_finished_error_summary_shows_critical_dialog(self):
        """일반 오류 요약 종료 시 critical 다이얼로그와 failed 상태 전환 검증."""
        summary = AliexpressCrawlSummary(
            collected_items=1,
            termination_reason="error",
            error="RuntimeError: 붕괴",
        )
        with patch.object(QMessageBox, "critical") as mock_crit, \
             patch.object(QMessageBox, "warning") as mock_warn:
            self.win._on_alicat_finished(summary)

        mock_crit.assert_called_once()
        mock_warn.assert_not_called()
        self.assertEqual(self.win.aliexpress_category_panel._state, "failed")

    def test_finished_cancelled_summary_logs_resume_guidance(self):
        """취소 요약 종료 시 재개 안내 로그와 finished 상태 전환 검증."""
        summary = AliexpressCrawlSummary(
            collected_items=5,
            unique_vendors=2,
            cancelled=True,
            termination_reason="cancelled",
        )
        with patch.object(QMessageBox, "warning") as mock_warn, \
             patch.object(QMessageBox, "critical") as mock_crit:
            self.win._on_alicat_finished(summary)

        mock_warn.assert_not_called()
        mock_crit.assert_not_called()
        self.assertEqual(self.win.aliexpress_category_panel._state, "finished")
        self.assertIn("[재개 안내]", self.win.aliexpress_category_panel.log_text.toPlainText())

    def test_finished_success_summary_keeps_finished_state(self):
        """정상 완료 요약은 다이얼로그 없이 finished 상태로 종료 검증."""
        summary = AliexpressCrawlSummary(
            collected_items=9,
            unique_vendors=4,
            has_email=8,
            has_business_number=9,
            termination_reason="success",
        )
        with patch.object(QMessageBox, "warning") as mock_warn, \
             patch.object(QMessageBox, "critical") as mock_crit:
            self.win._on_alicat_finished(summary)

        mock_warn.assert_not_called()
        mock_crit.assert_not_called()
        self.assertEqual(self.win.aliexpress_category_panel._state, "finished")


@unittest.skipUnless(_QT_OK, "PyQt6 필요")
class AliexpressCrawlWorkerSummaryTest(unittest.TestCase):
    """워커가 어떤 종료 경로에서도 finished_crawl 로 요약을 내보내는지 검증.

    성공·취소·예외 모두 AliexpressCrawlSummary 를 정확히 1회 방출해야 하며,
    요약에는 cancelled(bool) / termination_reason(str) 이 실려 있다.
    """

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config = AliexpressCategoryRunConfig(
            output_dir=Path(self.tmp_dir.name),
            category_name="워커요약",
            category_url="https://ko.aliexpress.com/category/321/worker.html",
        )
        self.control = Control()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _run_and_collect(self) -> list:
        summaries: list = []
        worker = AliexpressCategoryCrawlWorker(self.config, control=self.control)
        worker.finished_crawl.connect(lambda s: summaries.append(s))
        worker.run()
        return summaries

    def test_success_summary_passes_through_with_reason(self):
        """정상 종료 시 크롤러 요약이 그대로 1회 방출되는지 검증."""
        expected = AliexpressCrawlSummary(
            total_items=3,
            collected_items=3,
            unique_vendors=2,
            termination_reason="success",
        )
        with patch.object(AliexpressCategoryCrawler, "crawl", return_value=expected):
            summaries = self._run_and_collect()

        self.assertEqual(len(summaries), 1)
        self.assertIs(summaries[0], expected)
        self.assertEqual(summaries[0].termination_reason, "success")
        self.assertFalse(summaries[0].cancelled)

    def test_cancelled_crawl_still_emits_summary(self):
        """취소 예외로 끊겨도 cancelled 요약을 1회 방출하는지 검증 (요약 None 금지)."""
        with patch.object(AliexpressCategoryCrawler, "crawl", side_effect=CancelledError()):
            summaries = self._run_and_collect()

        self.assertEqual(len(summaries), 1)
        self.assertIsInstance(summaries[0], AliexpressCrawlSummary)
        self.assertTrue(summaries[0].cancelled)
        self.assertEqual(summaries[0].termination_reason, "cancelled")

    def test_crash_still_emits_error_summary(self):
        """크롤러 예외로 끊겨도 error 요약을 1회 방출하는지 검증 (요약 None 금지)."""
        with patch.object(AliexpressCategoryCrawler, "crawl", side_effect=RuntimeError("붕괴")):
            summaries = self._run_and_collect()

        self.assertEqual(len(summaries), 1)
        self.assertIsInstance(summaries[0], AliexpressCrawlSummary)
        self.assertEqual(summaries[0].termination_reason, "error")
        self.assertIn("붕괴", str(summaries[0].error))
        self.assertFalse(summaries[0].cancelled)


class AliexpressResumeStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_ali_category_run_dir_and_peek_resume(self):
        from app.core.aliexpress_resume_store import (
            AliexpressResumeStore,
            ali_category_run_dir,
            peek_resume,
        )

        run_dir = ali_category_run_dir(
            self.output_dir, "과일/채소", "https://ko.aliexpress.com/category/200000343/food.html"
        )
        self.assertIn("과일_채소_200000343", run_dir.name)
        self.assertIsNone(peek_resume(run_dir))

        store = AliexpressResumeStore(run_dir)
        store.open()
        store.record_page(1, [{"id": "1001", "title": "사과"}])
        store.record_seller("v123", {"company_name": "(주)애플농장"})
        store.close()

        peek_msg = peek_resume(run_dir)
        self.assertIsNotNone(peek_msg)
        self.assertIn("마지막 확인 페이지 1", peek_msg)
        self.assertIn("상품 1개", peek_msg)
        self.assertIn("확인된 판매자 1명", peek_msg)

    def test_atomic_page_and_seller_recording(self):
        from app.core.aliexpress_resume_store import (
            STATUS_FINISHED,
            STATUS_LISTING_DONE,
            AliexpressResumeStore,
            ali_category_run_dir,
            peek_resume,
        )

        run_dir = ali_category_run_dir(
            self.output_dir, "신선식품", "https://ko.aliexpress.com/category/12345/fresh.html"
        )
        store = AliexpressResumeStore(run_dir)
        store.open()

        # Phase 1: record pages
        items_p1 = [{"id": "item1", "title": "토마토 1kg"}, {"id": "item2", "title": "오이 5입"}]
        store.record_page(1, items_p1)
        self.assertEqual(store.last_completed_page, 1)
        self.assertEqual(store.product_count, 2)

        items_p2 = [{"id": "item3", "title": "감자 2kg"}]
        store.record_page(2, items_p2)
        self.assertEqual(store.last_completed_page, 2)
        self.assertEqual(store.product_count, 3)

        prods = store.load_products()
        self.assertEqual(len(prods), 3)
        self.assertEqual(prods[0]["id"], "item1")

        store.mark_listing_done()
        self.assertEqual(store.status, STATUS_LISTING_DONE)

        # Phase 2: record sellers and items
        seller_data = {
            "company_name": "신선마켓",
            "ceo_name": "홍길동",
            "business_number": "111-22-33333",
            "phone": "02-111-2222",
            "email": "fresh@market.com",
            "address": "서울시 마포구",
            "ecommerce_report_number": "2026-서울마포-0001",
        }
        store.record_seller("vendor_99", seller_data)
        self.assertEqual(store.confirmed_seller_count, 1)
        confirmed = store.confirmed_sellers()
        self.assertIn("vendor_99", confirmed)
        self.assertEqual(confirmed["vendor_99"]["company_name"], "신선마켓")

        item_rec = {"item_id": "item1", "vendor_id": "vendor_99", **seller_data}
        store.record_item_result("item1", "vendor_99", item_rec)
        self.assertIn("item1", store.processed_item_ids())
        self.assertEqual(len(store.load_item_results()), 1)

        store.mark_finished()
        self.assertEqual(store.status, STATUS_FINISHED)
        # finished store should not be resumed
        self.assertIsNone(peek_resume(run_dir))
        store.close()

    def test_archive_on_fresh_start(self):
        from app.core.aliexpress_resume_store import (
            AliexpressResumeStore,
            ali_category_run_dir,
        )

        run_dir = ali_category_run_dir(
            self.output_dir, "견과류", "https://ko.aliexpress.com/category/999/nuts.html"
        )
        store = AliexpressResumeStore(run_dir)
        store.open()
        store.record_page(1, [{"id": "nut1", "title": "아몬드 500g"}])
        store.close()

        # Archive
        archived_path = store.archive()
        self.assertIsNotNone(archived_path)
        self.assertTrue(archived_path.exists())
        self.assertIn("resume_archive_", archived_path.name)
        self.assertFalse(store.path.exists())

        # Start fresh
        store.open()
        self.assertEqual(store.product_count, 0)
        self.assertEqual(store.last_completed_page, 0)
        store.close()

    def test_check_config_mismatch(self):
        from app.core.aliexpress_resume_store import (
            AliexpressResumeStore,
            ali_category_run_dir,
        )

        run_dir = ali_category_run_dir(
            self.output_dir, "패션", "https://ko.aliexpress.com/category/111/fashion.html"
        )
        store = AliexpressResumeStore(run_dir)
        store.open()
        self.assertIsNone(store.check_config("패션", "https://ko.aliexpress.com/category/111/fashion.html", 30))

        # Different URL
        mismatch = store.check_config("패션", "https://ko.aliexpress.com/category/222/diff.html", 30)
        self.assertIsNotNone(mismatch)
        self.assertIn("다릅니다", mismatch)
        store.close()

