"""Gmarket 카테고리 탭 패널 UI 테스트 — 트리 렌더·선택·대상 구성 (offscreen).

PyQt6 가 없으면 전체 skip(다른 PyQt6 테스트 모듈과 달리 import 오류를 내지
않도록 가드한다).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtWidgets import QApplication

    _QT_OK = True
except Exception:  # noqa: BLE001 - PyQt6 미설치 환경
    _QT_OK = False

if _QT_OK:
    from app.core.gmarket_categories import GmarketCategoryNode
    from app.ui.gmarket_category_panel import GmarketCategoryPanel

_app = None


def setUpModule():
    global _app
    if _QT_OK:
        _app = QApplication.instance() or QApplication(sys.argv)


def _roots() -> list:
    return [
        GmarketCategoryNode("100000003", "여성의류", "L", children=[
            GmarketCategoryNode("200000498", "티셔츠", "M", children=[
                GmarketCategoryNode("300026660", "무지 티셔츠", "S"),
                GmarketCategoryNode("300026664", "카라 티셔츠", "S"),
            ]),
            GmarketCategoryNode("200000502", "자켓/코트", "M"),
        ]),
        GmarketCategoryNode("100000005", "화장품/향수", "L"),
    ]


@unittest.skipUnless(_QT_OK, "PyQt6 필요")
class GmarketCategoryPanelTest(unittest.TestCase):
    def setUp(self):
        self.panel = GmarketCategoryPanel()
        self.panel.set_category_roots(_roots(), "테스트", 7)

    def _select(self, code: str):
        tree = self.panel.category_tree
        stack = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            if str(item.data(0, 0x0100)) == code:
                tree.setCurrentItem(item)
                return item
            for i in range(item.childCount()):
                stack.append(item.child(i))
        raise AssertionError(f"트리에서 {code} 미발견")

    def test_tree_rendered(self):
        tree = self.panel.category_tree
        self.assertEqual(tree.topLevelItemCount(), 2)
        self.assertEqual(tree.topLevelItem(0).childCount(), 2)  # 여성의류 중분류 2

    def test_build_targets_single(self):
        item = self._select("300026660")
        self.panel.chk_include_subs.setChecked(False)
        targets = self.panel.build_targets()
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].code, "300026660")
        self.assertEqual(targets[0].label, "여성의류 > 티셔츠 > 무지 티셔츠")

    def test_build_targets_include_subs(self):
        self._select("200000498")  # 티셔츠 중분류
        self.panel.chk_include_subs.setChecked(True)
        targets = self.panel.build_targets()
        codes = [t.code for t in targets]
        self.assertEqual(codes, ["200000498", "300026660", "300026664"])
        # 자식 라벨도 전체 경로
        self.assertEqual(targets[1].label, "여성의류 > 티셔츠 > 무지 티셔츠")

    def test_build_targets_all_targets_returns_middle_level(self):
        # 전체 대상 모드: 대분류(L)는 레거시 변형 위험이 있어 중분류(M)만 대상
        self.panel.chk_all_targets.setChecked(True)
        targets = self.panel.build_targets()
        codes = [t.code for t in targets]
        self.assertEqual(codes, ["200000498", "200000502"])
        self.assertTrue(all(" > " in t.label for t in targets))

    def test_filter_tree_hides_non_matching(self):
        self.panel.tree_filter.setText("자켓")
        tree = self.panel.category_tree
        visible = []
        stack = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            if not item.isHidden():
                visible.append(str(item.data(0, 0x0100) or item.text(1)))
            for i in range(item.childCount()):
                stack.append(item.child(i))
        self.assertIn("200000502", visible)          # 자켓/코트
        self.assertNotIn("200000498", visible)       # 티셔츠
        self.panel.tree_filter.clear()

    def test_build_config_requires_selection_and_dir(self):
        self.assertIsNone(self.panel.build_config())  # 출력 폴더 없음
        with tempfile.TemporaryDirectory() as td:
            self.panel.output_dir_edit.setText(td)
            self.panel.chk_include_subs.setChecked(False)
            self._select("300026660")
            cfg = self.panel.build_config()
            self.assertIsNotNone(cfg)
            self.assertEqual(cfg.targets[0].code, "300026660")
            self.assertTrue(str(cfg.output_prefix).startswith("gmarket_category_"))

    def test_state_disables_controls(self):
        self.panel.set_state("running")
        self.assertFalse(self.panel.btn_start.isEnabled())
        self.assertTrue(self.panel.btn_cancel.isEnabled())
        self.panel.set_state("finished")
        self.assertTrue(self.panel.btn_start.isEnabled())

    def test_external_busy_locks(self):
        self.panel.set_external_busy(True)
        self.assertFalse(self.panel.btn_start.isEnabled())
        self.panel.set_external_busy(False)
        self.assertTrue(self.panel.btn_start.isEnabled())


if __name__ == "__main__":
    unittest.main()
