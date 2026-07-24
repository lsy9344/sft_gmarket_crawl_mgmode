"""app.core.config 회귀 테스트: PyInstaller one-file 번들 경로 처리 (HIGH-5)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core import config


class FrozenPathTest(unittest.TestCase):
    def test_non_frozen_uses_file_based_root(self) -> None:
        """일반 실행(sys.frozen 없음)에서는 기존처럼 __file__ 기준 루트를 사용."""
        with patch.object(sys, "frozen", False, create=True):
            root = config._default_project_root()
        self.assertTrue((root / "app").exists())

    def test_frozen_uses_executable_directory_not_tmp_bundle(self) -> None:
        """PyInstaller one-file 번들(sys.frozen=True)에서는 __file__ (임시 추출
        경로, 프로세스 종료 시 삭제됨) 대신 sys.executable 의 디렉터리를 써야
        한다 — 그래야 종료 후에도 output/ 이 실제 실행 파일 옆에 남는다."""
        fake_exe = "/opt/GmarketSellerCollector/GmarketSellerCollector.exe"
        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", fake_exe):
            root = config._default_project_root()
        self.assertEqual(root, Path(fake_exe).resolve().parent)
        self.assertNotIn("MEIPASS", str(root))


if __name__ == "__main__":
    unittest.main()
