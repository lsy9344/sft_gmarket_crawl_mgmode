"""파일 로깅(applog) 회귀: 로그 회전 파일 생성·기록·중복 핸들러 방지."""

from __future__ import annotations

import tempfile
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import patch

from app.core import applog


class FileLoggingTest(unittest.TestCase):
    def tearDown(self) -> None:
        logger = applog.get_logger()
        for handler in list(logger.handlers):
            if isinstance(handler, RotatingFileHandler):
                logger.removeHandler(handler)
                handler.close()

    def test_setup_creates_file_and_log_line_persists(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        with patch.object(applog, "log_file_dir", return_value=tmpdir):
            path = applog.setup_file_logging()
        self.assertIsNotNone(path)
        self.assertEqual(path.parent, tmpdir)

        applog.log_line("[Coupang] 진단 테스트 라인")
        content = path.read_text(encoding="utf-8")
        self.assertIn("[Coupang] 진단 테스트 라인", content)

    def test_setup_is_idempotent(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        with patch.object(applog, "log_file_dir", return_value=tmpdir):
            first = applog.setup_file_logging()
            second = applog.setup_file_logging()
        self.assertEqual(first, second)
        # pytest 가 로거에 캡처 핸들러를 덧붙일 수 있으므로 파일 핸들러만 센다.
        rotating = [
            h for h in applog.get_logger().handlers if isinstance(h, RotatingFileHandler)
        ]
        self.assertEqual(len(rotating), 1)

    def test_ali_logging_does_not_write_running_gmarket_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "seller_collector.log"
            original.write_text("gmarket running", encoding="utf-8")
            with patch.object(applog, "log_file_dir", return_value=root):
                path = applog.setup_file_logging(filename="ali_collector.log")
            applog.log_line("ali started")
            self.assertEqual(original.read_text(encoding="utf-8"), "gmarket running")
            self.assertIn("ali started", path.read_text(encoding="utf-8"))
            self.tearDown()

    def test_setup_failure_returns_none_without_raising(self) -> None:
        with patch.object(applog, "log_file_dir", side_effect=OSError("no dir")):
            self.assertIsNone(applog.setup_file_logging())

    def test_log_line_without_setup_is_silent_noop(self) -> None:
        applog.log_line("핸들러 없이 호출해도 예외가 없어야 한다")


if __name__ == "__main__":
    unittest.main()
