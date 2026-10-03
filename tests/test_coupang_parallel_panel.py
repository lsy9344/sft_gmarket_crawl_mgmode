"""쿠팡 병렬 수집 패널 — 가족 구성·상태 머신·인스턴스 카드(오프스크린).

QT_QPA_PLATFORM=offscreen 환경에서 QWidget 을 직접 구성해 검증한다.
네트워크 없음(카테고리 그룹은 테스트가 직접 주입).
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402

from PyQt6.QtWidgets import QApplication  # noqa: E402

from app.core.coupang.categories import CategoryNode  # noqa: E402
from app.core.coupang.parallel_manager import ParallelRunConfig  # noqa: E402
from app.ui.coupang_parallel_panel import CoupangParallelPanel  # noqa: E402

_APP: QApplication | None = None


def _ensure_app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def _node(node_id: str, name: str, children: list[CategoryNode] | None = None):
    return CategoryNode(
        id=node_id,
        name=name,
        uri=f"/np/categories/{node_id}",
        children=children or [],
    )


def _sample_groups() -> list[tuple[str, list[CategoryNode]]]:
    # 식품(하위 2개, 손자 1개) + 주방(하위 1개) — 가족 플래튼 검증용
    food = _node(
        "194276",
        "식품",
        [
            _node("194373", "견과/건과"),
            _node("194688", "축산", [_node("194690", "소고기")]),
        ],
    )
    kitchen = _node("185669", "주방용품", [_node("185671", "냄비/프라이팬")])
    return [("쇼핑", [food, kitchen])]


class CoupangParallelPanelTest(unittest.TestCase):
    def setUp(self) -> None:
        _ensure_app()
        self.panel = CoupangParallelPanel()

    def tearDown(self) -> None:
        self.panel.deleteLater()

    def _select_root(self, index: int) -> None:
        group = self.panel.category_tree.topLevelItem(0)
        item = group.child(index)
        item.setSelected(True)

    # ── 트리·가족 구성 ─────────────────────────────────────────────

    def test_set_category_groups_fills_tree(self) -> None:
        self.panel.set_category_groups(_sample_groups(), "2026-10-03 10:00", 5)
        self.assertEqual(self.panel.category_tree.topLevelItemCount(), 1)
        group = self.panel.category_tree.topLevelItem(0)
        self.assertEqual(group.childCount(), 2)
        self.assertIn("5", self.panel.cache_label.text())

    def test_selected_families_flatten_descendants_with_dedup(self) -> None:
        self.panel.set_category_groups(_sample_groups())
        self._select_root(0)  # 식품
        families = self.panel._selected_families()
        self.assertEqual(len(families), 1)
        ids = [cid for cid, _ in families[0]]
        # 루트 + 후송 전부, id 중복 없음
        self.assertEqual(ids, ["194276", "194373", "194688", "194690"])
        self.assertEqual(len(ids), len(set(ids)))

    def test_two_roots_make_two_families(self) -> None:
        self.panel.set_category_groups(_sample_groups())
        self._select_root(0)
        self._select_root(1)
        families = self.panel._selected_families()
        self.assertEqual(len(families), 2)  # 최상위 노드 각각 = 1개 가족
        self.assertEqual(families[0][0][0], "194276")
        self.assertEqual(families[1][0][0], "185669")

    def test_build_run_config_without_selection_returns_none(self) -> None:
        self.panel.set_category_groups(_sample_groups())
        self.assertIsNone(self.panel.build_run_config(output_dir="/tmp/x"))

    def test_build_run_config_returns_parallel_config(self) -> None:
        self.panel.set_category_groups(_sample_groups())
        self._select_root(0)
        config = self.panel.build_run_config(
            output_dir="/tmp/panel_out", instance_count=2
        )
        self.assertIsInstance(config, ParallelRunConfig)
        self.assertEqual(config.instance_count, 2)
        self.assertEqual(
            [cid for cid, _ in config.families[0]],
            ["194276", "194373", "194688", "194690"],
        )
        self.assertEqual(config.output_dir, Path("/tmp/panel_out"))

    def test_invalid_instance_count_is_rejected_with_log(self) -> None:
        self.panel.set_category_groups(_sample_groups())
        self._select_root(0)
        config = self.panel.build_run_config(
            output_dir="/tmp/panel_out", instance_count=9
        )
        self.assertIsNone(config)

    # ── 인스턴스 카드 ───────────────────────────────────────────────

    def test_instance_count_preview_creates_cards(self) -> None:
        self.panel.set_instance_count_preview(3)
        cards = {
            key
            for key in self.panel._instance_cards
            if key != "header"
        }
        self.assertEqual(len(cards), 3)
        self.assertIn("1", cards)
        self.assertIn("3", cards)

    def test_update_instance_creates_and_updates_card(self) -> None:
        self.panel.update_instance(
            "2", "collecting", {"total_products": 10, "total_sellers": 4}
        )
        self.assertIn("2", self.panel._instance_cards)
        card = self.panel._instance_cards["2"]
        text = card.status_label.text()
        self.assertTrue(text)  # 상태 문자가 표기됐다

    # ── 상태 머신·잠금 ─────────────────────────────────────────────

    def test_state_machine_toggles_buttons(self) -> None:
        self.panel.set_state("running")
        self.assertFalse(self.panel.btn_start.isEnabled())
        self.assertTrue(self.panel.btn_stop.isEnabled())
        self.panel.set_state("finished")
        self.assertTrue(self.panel.btn_start.isEnabled())
        self.assertFalse(self.panel.btn_stop.isEnabled())

    def test_external_busy_disables_everything(self) -> None:
        self.panel.set_state("idle")
        self.panel.set_external_busy(True)
        self.assertFalse(self.panel.btn_start.isEnabled())
        self.assertFalse(self.panel.btn_refresh_categories.isEnabled())
        self.panel.set_external_busy(False)
        self.assertTrue(self.panel.btn_start.isEnabled())

    def test_loading_categories_locks_tree(self) -> None:
        self.panel.set_loading_categories(True)
        self.assertFalse(self.panel.category_tree.isEnabled())
        self.panel.set_loading_categories(False)
        self.assertTrue(self.panel.category_tree.isEnabled())

    # ── 결과 표·시작 요청 ───────────────────────────────────────────

    def test_add_record_appends_row_with_checkmark(self) -> None:
        self.panel.add_record(
            {
                "vendor_id": "V9",
                "store_name": "스토어",
                "company_name": "상호",
                "ceo_name": "대표",
                "business_number": "123",
                "phone": "02",
                "email": "x@y.z",
                "power_seller": True,
            }
        )
        self.assertEqual(self.panel.result_table.rowCount(), 1)
        self.assertEqual(
            self.panel.result_table.item(0, 0).text(), "V9"
        )
        self.assertEqual(self.panel.result_table.item(0, 7).text(), "✓")

    def test_start_requested_payload(self) -> None:
        payloads: list[dict] = []
        self.panel.start_requested.connect(payloads.append)
        self.panel.set_category_groups(_sample_groups())
        self._select_root(0)
        self.panel._on_start_clicked()
        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertEqual(payload["family_count"], 1)
        self.assertEqual(payload["instance_count"], 3)  # 기본값
        self.assertTrue(payload["output_dir"])

    def test_start_without_selection_emits_nothing(self) -> None:
        payloads: list[dict] = []
        self.panel.start_requested.connect(payloads.append)
        self.panel.set_category_groups(_sample_groups())
        self.panel._on_start_clicked()
        self.assertEqual(payloads, [])


if __name__ == "__main__":
    unittest.main()
