"""카테고리 탭 패널 UI 테스트 — 트리 렌더·단일 카테고리 선택 (offscreen)."""

import os
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app.core.coupang.categories import parse_category_groups
from app.ui.category_panel import CategoryPanel

_app = None


def setUpModule():
    global _app
    _app = QApplication.instance() or QApplication(sys.argv)


def _payload():
    return {"data": {"gnb": {"shoppingComponent": [
        {"id": 1, "name": "헬스/건강식품", "linkCode": "305798",
         "linkUri": "/np/categories/305798",
         "visibleChildren": [
             {"id": 2, "name": "비타민/미네랄", "linkCode": "310632",
              "linkUri": "/np/categories/310632",
              "visibleChildren": [
                  {"id": 4, "name": "비타민C", "linkCode": "310637",
                   "linkUri": "/np/categories/310637", "visibleChildren": []}]},
             {"id": 3, "name": "건강식품", "linkCode": "310655",
              "linkUri": "/np/categories/310655", "visibleChildren": []},
         ]},
    ]}}}


class CategoryPanelTest(unittest.TestCase):
    def setUp(self):
        self.panel = CategoryPanel()
        self.groups = parse_category_groups(_payload())
        self.panel.set_category_groups(self.groups, "테스트", 4)

    def _select(self, cat_id: str):
        tree = self.panel.category_tree
        stack = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            if item.data(0, 0x0100) == cat_id:
                tree.setCurrentItem(item)
                return
            for i in range(item.childCount()):
                stack.append(item.child(i))
        raise AssertionError(f"트리에서 {cat_id} 미발견")

    def test_tree_rendered(self):
        tree = self.panel.category_tree
        self.assertEqual(tree.topLevelItemCount(), 1)
        self.assertEqual(tree.topLevelItem(0).childCount(), 1)  # 헬스/건강식품
        self.assertEqual(tree.topLevelItem(0).child(0).childCount(), 2)

    def test_select_is_single_category_only(self):
        self._select("305798")
        self.assertEqual(self.panel.selected_category(), ("305798", "헬스/건강식품"))
        self.assertIn("이 카테고리만", self.panel.selected_label.text())

    def test_build_config_one_category_no_subs_or_login(self):
        self._select("305798")
        with tempfile.TemporaryDirectory() as tmp:
            self.panel.output_dir_edit.setText(tmp)
            config = self.panel.build_config()
        self.assertIsNotNone(config)
        self.assertTrue(config.category_only)
        self.assertEqual(config.category_id, "305798")
        self.assertEqual(config.category_name, "헬스/건강식품")
        self.assertEqual(config.subcategories, ())
        self.assertFalse(config.require_login)
        self.assertFalse(config.exclude_rocket)
        self.assertIn("coupang_category_", config.output_prefix)
        self.assertFalse(hasattr(self.panel, "btn_login"))
        self.assertFalse(hasattr(self.panel, "chk_include_subs"))

    def test_build_config_requires_selection_and_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.panel.output_dir_edit.setText(tmp)
            self.assertIsNone(self.panel.build_config())  # 미선택
        self._select("310655")
        self.panel.output_dir_edit.setText("")
        self.assertIsNone(self.panel.build_config())  # 폴더 미지정

    def test_leaf_selection(self):
        self._select("310655")
        self.assertEqual(self.panel.selected_category(), ("310655", "건강식품"))


if __name__ == "__main__":
    unittest.main()
