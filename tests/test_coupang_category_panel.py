"""카테고리 탭 패널 UI 테스트 — 트리 렌더·하위 카테고리 선택 (offscreen)."""

import os
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QTreeWidgetItem

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

    def test_select_includes_descendants_in_parent_first_order(self):
        self._select("305798")
        self.assertEqual(self.panel.selected_category(), ("305798", "헬스/건강식품"))
        self.assertEqual(
            self.panel.selected_targets(),
            (
                ("305798", "헬스/건강식품"),
                ("310632", "비타민/미네랄"),
                ("310637", "비타민C"),
                ("310655", "건강식품"),
            ),
        )
        self.assertIn("하위 포함 4개", self.panel.selected_label.text())

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
        self.assertEqual(self.panel.selected_targets(), (("310655", "건강식품"),))

    def test_selected_targets_deduplicates_ids(self):
        self._select("305798")
        root = self.panel.category_tree.currentItem()
        duplicate = QTreeWidgetItem(["다른 이름", "310632"])
        duplicate.setData(0, 0x0100, "310632")
        duplicate.setData(0, 0x0101, "다른 이름")
        root.addChild(duplicate)
        self.assertEqual(
            [target[0] for target in self.panel.selected_targets()],
            ["305798", "310632", "310637", "310655"],
        )


if __name__ == "__main__":
    unittest.main()
