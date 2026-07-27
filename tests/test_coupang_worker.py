"""CoupangWorker QThread 테스트 (AC-06, AC-14, AC-15)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QApplication

from app.core.base import Control
from app.models.coupang_records import CoupangRunConfig, CoupangRunSummary
from app.workers.coupang_worker import CoupangWorker

_app = None


def setUpModule():
    global _app
    _app = QApplication.instance() or QApplication(sys.argv)


def _make_config(tmp_dir):
    return CoupangRunConfig(
        output_dir=Path(tmp_dir),
        output_prefix="worker_test",
        max_scroll_pages=1,
        batch_size=5,
        warmup_time=0,
        delay_min=0,
        delay_max=0,
    )


class FakeBrowserForWorker:
    def __init__(self):
        self.closed = False

    def new_page(self):
        return FakePageForWorker()

    def close(self):
        self.closed = True


class FakePageForWorker:
    def __init__(self):
        self.mouse = MagicMock()
        self._handlers = []

    def on(self, event, handler):
        self._handlers.append(handler)

    def goto(self, url, **kwargs):
        pass

    def evaluate(self, script, *args):
        return {"status": 200, "body": '{"ret": "0", "data": {"promotionData": [], "token": null}}'}


class WorkerSignalTest(unittest.TestCase):
    """AC-14: finished_crawl emitted exactly once on all paths."""

    def test_finished_emitted_once_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            control = Control()
            finished_count = [0]

            def on_finished(summary):
                finished_count[0] += 1

            worker = CoupangWorker(
                config, control,
                browser_factory=lambda: FakeBrowserForWorker(),
            )
            worker.finished_crawl.connect(on_finished)
            worker.start()
            worker.wait(10000)
            QCoreApplication.processEvents()

        self.assertEqual(finished_count[0], 1)

    def test_finished_emitted_once_on_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            control = Control()
            finished_count = [0]

            def exploding_factory():
                raise RuntimeError("browser exploded")

            worker = CoupangWorker(config, control, browser_factory=exploding_factory)
            worker.finished_crawl.connect(lambda s: finished_count.__setitem__(0, finished_count[0] + 1))
            worker.start()
            worker.wait(10000)
            QCoreApplication.processEvents()

        self.assertEqual(finished_count[0], 1)
        self.assertIsNotNone(worker.summary)
        self.assertEqual(worker.summary.termination_reason, "error")

    def test_summary_stored_on_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            worker = CoupangWorker(config, Control(), browser_factory=lambda: FakeBrowserForWorker())
            worker.start()
            worker.wait(10000)
            QCoreApplication.processEvents()

        self.assertIsInstance(worker.summary, CoupangRunSummary)


class WorkerThreadExportTest(unittest.TestCase):
    """AC-06: browser_factory + crawler + exporter all run on same QThread."""

    def test_all_ops_on_single_qthread(self):
        import json
        import threading
        from unittest.mock import patch

        from app.core.coupang.exporter import CoupangExporter

        main_tid = threading.current_thread().ident
        factory_threads = set()
        eval_threads = set()
        export_threads = set()

        class TrackingPage:
            def __init__(self):
                self.mouse = MagicMock()
                self._handlers = []
                self._call = 0

            def on(self, event, handler):
                self._handlers.append(handler)

            def goto(self, url, **kwargs):
                if "/np/omp" in str(url):
                    req = MagicMock()
                    req.url = "https://www.coupang.com/np/omp/api/getPromotion"
                    req.post_data = json.dumps({"query": {"feedId": "f", "continuationToken": "seemore=CGs="}})
                    for h in self._handlers:
                        h(req)

            def evaluate(self, script, *args):
                eval_threads.add(threading.current_thread().ident)
                if "getPromotion" in script:
                    self._call += 1
                    if self._call == 1:
                        items = [{"vendorItemId": "VI1", "itemId": "I1", "title": "T1", "categoryId": "1"}]
                        return {"status": 200, "body": json.dumps({"ret": "0", "data": {"promotionData": items, "token": None}})}
                    return {"status": 200, "body": json.dumps({"ret": "0", "data": {"promotionData": [], "token": None}})}
                if "individualInfo" in script:
                    prods = [{"productId": "P0", "itemId": "I0", "vendorItemId": "VI1",
                              "storeInfoArea": {"vendorId": "V0", "storeId": 1, "displayName": "S"}}]
                    return {"status": 200, "body": json.dumps({"code": 200, "data": {"products": prods}})}
                if "getStoreReview" in script:
                    return {"status": 200, "body": json.dumps({
                        "name": "상호", "repPersonName": "대표", "businessNumber": "123-45-67890",
                        "repPhoneNum": "02-000", "repEmail": "t@t", "repAddr1": "서울", "repAddr2": "동",
                        "eCommerceReportNumber": "2024-001", "qualitySellerBadgeDto": None,
                        "ratingCount": 10, "thumbUpRatio": 90.0})}
                return {}

            def close(self):
                pass

        class TrackingBrowser:
            def __init__(self, page):
                self._page = page
            def new_page(self):
                return self._page
            def close(self):
                pass

        page = TrackingPage()

        def tracking_factory():
            factory_threads.add(threading.current_thread().ident)
            return TrackingBrowser(page)

        orig_save = CoupangExporter.save

        def tracking_save(self_exporter, records, partial=False):
            export_threads.add(threading.current_thread().ident)
            return orig_save(self_exporter, records, partial=partial)

        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            worker = CoupangWorker(config, Control(), browser_factory=tracking_factory)
            with patch.object(CoupangExporter, "save", tracking_save):
                worker.start()
                worker.wait(10000)
                QCoreApplication.processEvents()

        self.assertTrue(factory_threads, "browser_factory was never called")
        self.assertTrue(eval_threads, "evaluate was never called")
        self.assertTrue(export_threads, "exporter.save was never called")
        for tid in factory_threads:
            self.assertNotEqual(tid, main_tid, "browser_factory ran on main thread")
        for tid in eval_threads:
            self.assertNotEqual(tid, main_tid, "evaluate ran on main thread")
        for tid in export_threads:
            self.assertNotEqual(tid, main_tid, "exporter ran on main thread")
        all_threads = factory_threads | eval_threads | export_threads
        self.assertEqual(len(all_threads), 1, "operations ran on multiple threads")


if __name__ == "__main__":
    unittest.main()
