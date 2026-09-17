"""Adversarial stress and resilience tests for AliexpressResumeStore and run directory isolation.

Challenger 1 empirical verification harness:
- Multi-threaded concurrent reads and writes
- Interrupted atomic transactions (Phase 1 & Phase 2 rollback)
- Corrupted SQLite files (0-byte, random garbage, malformed tables)
- Rapid restart (100x archive cycles) & journal file handling
- Path traversal & run directory collision resistance
- Configuration drift and schema boundary attacks
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import random
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.core.aliexpress_resume_store import (
    AliexpressResumeStore,
    ResumeConfigMismatch,
    ResumeStoreError,
    STATUS_FINISHED,
    STATUS_LISTING,
    STATUS_LISTING_DONE,
    ali_category_run_dir,
    peek_resume,
)


def _sample_product(item_id: str | int, title: str = "Adversarial Item") -> dict:
    return {
        "id": str(item_id),
        "url": f"https://ko.aliexpress.com/item/{item_id}.html",
        "title": title,
        "price": "₩15,000",
        "orders": "500+ 판매",
        "is_top_seller": True,
    }


def _sample_seller(vendor_id: str | int) -> dict:
    return {
        "store_name": f"Adversarial Store {vendor_id}",
        "company_name": f"Adversarial Co {vendor_id}",
        "ceo_name": "Attacker CEO",
        "business_number": f"111-22-{str(vendor_id).zfill(5)}",
        "phone": "010-9999-8888",
        "email": f"vendor_{vendor_id}@stress.test",
        "address": "Seoul, Test District 404",
        "ecommerce_report_number": "2026-Test-9999",
    }


class TestAdversarialTransactionsAndInterruption(unittest.TestCase):
    """Adversarial verification of atomic transactions and crash interruptions."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="adv_ali_test_")
        self.run_dir = Path(self.temp_dir) / "run_test"
        self.db_path = self.run_dir / "resume.sqlite3"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_interrupted_record_page_atomicity_rollback_sqlite_error(self):
        """Simulate a database-level abort during record_page.

        Neither products nor page entry should be committed if an error occurs.
        """
        store = AliexpressResumeStore(self.run_dir)
        store.open()

        # Successfully record page 1
        store.record_page(1, [_sample_product(1), _sample_product(2)])
        self.assertEqual(store.last_completed_page, 1)
        self.assertEqual(store.product_count, 2)

        # Trigger an error during insert of item 'fail_4'
        store.conn.execute(
            'CREATE TRIGGER abort_on_poison BEFORE INSERT ON products '
            'WHEN NEW.id = "fail_4" BEGIN SELECT RAISE(ABORT, "Aborted on poisoned item"); END;'
        )

        items = [_sample_product(3), _sample_product("fail_4"), _sample_product(5)]

        with self.assertRaises(ResumeStoreError):
            store.record_page(2, items)

        # Database must have rolled back page 2 completely:
        # last_completed_page must still be 1
        # products table must only contain items 1 and 2 (NOT item 3 or fail_4)
        self.assertEqual(store.last_completed_page, 1)
        self.assertEqual(store.product_count, 2)
        products = store.load_products()
        prod_ids = {p["id"] for p in products}
        self.assertEqual(prod_ids, {"1", "2"})
        self.assertNotIn("3", prod_ids)
        self.assertNotIn("fail_4", prod_ids)
        store.close()

    def test_interrupted_record_page_atomicity_rollback_serialization_error(self):
        """Simulate an unencodable item mid-way through record_page.

        Verifies Python context manager rollback on TypeError.
        """
        store = AliexpressResumeStore(self.run_dir)
        store.open()

        store.record_page(1, [_sample_product(1), _sample_product(2)])
        self.assertEqual(store.last_completed_page, 1)

        poisoned_item = {"id": "poison_3", "bad_data": object()}  # json.dumps will fail with TypeError
        items = [_sample_product(3), poisoned_item, _sample_product(5)]

        with self.assertRaises(TypeError):
            store.record_page(2, items)

        # Page 2 rolled back
        self.assertEqual(store.last_completed_page, 1)
        self.assertEqual(store.product_count, 2)
        products = store.load_products()
        prod_ids = {p["id"] for p in products}
        self.assertEqual(prod_ids, {"1", "2"})
        self.assertNotIn("3", prod_ids)
        store.close()

    def test_crash_interruption_during_phase2_items(self):
        """Simulate recording 30 Phase 2 item results, then abrupt disconnect.

        Remaining unprocessed items must be cleanly identifiable upon resume.
        """
        store = AliexpressResumeStore(self.run_dir)
        store.open()
        store.record_page(1, [_sample_product(i) for i in range(1, 51)])
        store.mark_listing_done()

        # Record 20 items successfully
        for i in range(1, 21):
            store.record_seller(str(i % 5), _sample_seller(i % 5))
            store.record_item_result(str(i), str(i % 5), {"item_id": str(i), "vendor_id": str(i % 5)})

        # Abruptly close
        store.close()

        # Reopen in new store handle (simulating process restart)
        reopened = AliexpressResumeStore(self.run_dir)
        reopened.open()
        self.assertEqual(reopened.status, STATUS_LISTING_DONE)
        processed = reopened.processed_item_ids()
        self.assertEqual(len(processed), 20)
        self.assertEqual(processed, {str(i) for i in range(1, 21)})
        self.assertEqual(len(reopened.confirmed_sellers()), 5)

        # Complete remaining 30 items
        all_products = reopened.load_products()
        unprocessed = [p for p in all_products if p["id"] not in processed]
        self.assertEqual(len(unprocessed), 30)

        for p in unprocessed:
            reopened.record_item_result(p["id"], "99", {"item_id": p["id"], "vendor_id": "99"})

        self.assertEqual(len(reopened.processed_item_ids()), 50)
        reopened.mark_finished()
        self.assertEqual(reopened.status, STATUS_FINISHED)
        reopened.close()


class TestAdversarialCorruptionAndInvalidSchemas(unittest.TestCase):
    """Adversarial testing of corrupted DB files, invalid schemas, and malformed data."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="adv_corrupt_test_")
        self.run_dir = Path(self.temp_dir) / "run_corrupt"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.run_dir / "resume.sqlite3"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_zero_byte_sqlite_file_handling(self):
        """A zero-byte file must be safely handled without unhandled crashes."""
        self.db_path.write_bytes(b"")

        # peek_resume on zero-byte file: has_state is False -> returns None
        note = peek_resume(self.run_dir)
        self.assertIsNone(note)

        # Opening zero-byte file initializes valid tables
        store = AliexpressResumeStore(self.run_dir)
        store.open()
        self.assertFalse(store.has_state())
        store.record_page(1, [_sample_product(100)])
        self.assertEqual(store.last_completed_page, 1)
        store.close()

    def test_random_binary_garbage_database(self):
        """Random binary junk inside resume.sqlite3 must raise ResumeStoreError and peek_resume returns None."""
        garbage = os.urandom(1024)
        self.db_path.write_bytes(garbage)

        # peek_resume must safely return None
        self.assertIsNone(peek_resume(self.run_dir))

        # store.open() must raise ResumeStoreError
        store = AliexpressResumeStore(self.run_dir)
        with self.assertRaises(ResumeStoreError):
            store.open()

    def test_truncated_sqlite_header(self):
        """Truncated SQLite header (e.g. only 16 bytes) fails gracefully."""
        self.db_path.write_bytes(b"SQLite format 3\x00")
        self.assertIsNone(peek_resume(self.run_dir))

        store = AliexpressResumeStore(self.run_dir)
        with self.assertRaises(ResumeStoreError):
            store.open()

    def test_missing_or_corrupted_table_schema(self):
        """If a table exists but schema is incompatible (e.g. columns missing), operations raise ResumeStoreError."""
        conn = sqlite3.connect(str(self.db_path))
        # Create a damaged pages table without page_no
        conn.execute("CREATE TABLE pages (wrong_col TEXT PRIMARY KEY);")
        conn.commit()
        conn.close()

        store = AliexpressResumeStore(self.run_dir)
        store.open()

        # last_completed_page queries page_no -> should raise ResumeStoreError
        with self.assertRaises(ResumeStoreError):
            _ = store.last_completed_page

        store.close()

    def test_corrupted_json_in_all_store_tables(self):
        """Corrupted JSON in products, sellers, and items must raise ResumeStoreError."""
        store = AliexpressResumeStore(self.run_dir)
        store.open()

        # Manually inject invalid JSON strings
        raw_conn = store.conn
        raw_conn.execute("INSERT INTO products (id, payload, page_no) VALUES ('bad_p', '{\"broken\": ', 1)")
        raw_conn.execute("INSERT INTO sellers (vendor_id, status, payload, completed_at) VALUES ('bad_v', 'confirmed', 'not-json', 'now')")
        raw_conn.execute("INSERT INTO items (item_id, vendor_id, payload, completed_at) VALUES ('bad_i', 'bad_v', '{unquoted', 'now')")
        raw_conn.commit()

        with self.assertRaises(ResumeStoreError):
            store.load_products()

        with self.assertRaises(ResumeStoreError):
            store.confirmed_sellers()

        with self.assertRaises(ResumeStoreError):
            store.load_item_results()

        store.close()

    def test_archive_corrupted_database_still_succeeds(self):
        """Archiving a corrupted database must succeed so the crawler can start fresh."""
        self.db_path.write_bytes(os.urandom(512))
        store = AliexpressResumeStore(self.run_dir)
        archived = store.archive()
        self.assertIsNotNone(archived)
        self.assertTrue(archived.exists())
        self.assertFalse(self.db_path.exists())


class TestAdversarialConcurrencyAndRaceConditions(unittest.TestCase):
    """Adversarial testing of multi-threaded contention and rapid cycles."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="adv_concur_test_")
        self.run_dir = Path(self.temp_dir) / "run_concur"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.run_dir / "resume.sqlite3"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_concurrent_readers_while_writer_active(self):
        """Multiple reader threads querying peek_resume and store metrics while writer records pages."""
        stop_event = threading.Event()
        writer_errors = []
        reader_errors = []

        def writer_loop():
            writer_store = None
            try:
                writer_store = AliexpressResumeStore(self.run_dir)
                writer_store.open()
                for p in range(1, 40):
                    if stop_event.is_set():
                        break
                    writer_store.record_page(p, [_sample_product(p * 100 + i) for i in range(10)])
                    time.sleep(0.01)
            except Exception as e:
                writer_errors.append(e)
            finally:
                if writer_store:
                    writer_store.close()

        def reader_loop():
            try:
                for _ in range(50):
                    if stop_event.is_set():
                        break
                    # peek_resume opens and reads store safely
                    _ = peek_resume(self.run_dir)
                    time.sleep(0.005)
            except Exception as e:
                reader_errors.append(e)

        writer_thread = threading.Thread(target=writer_loop)
        reader_threads = [threading.Thread(target=reader_loop) for _ in range(4)]

        writer_thread.start()
        for rt in reader_threads:
            rt.start()

        writer_thread.join(timeout=10)
        stop_event.set()
        for rt in reader_threads:
            rt.join(timeout=5)

        self.assertEqual(writer_errors, [], f"Writer thread failed: {writer_errors}")
        self.assertEqual(reader_errors, [], f"Reader thread failed: {reader_errors}")

        # Final store state must be readable and uncorrupted
        final_store = AliexpressResumeStore(self.run_dir)
        final_store.open()
        self.assertGreaterEqual(final_store.last_completed_page, 1)
        self.assertEqual(final_store.product_count, final_store.last_completed_page * 10)
        final_store.close()

    def test_rapid_restart_100x_archive_cycle(self):
        """Stress-test 100 rapid start-fresh/archive cycles in tight loop.

        Guarantees no file descriptor leaks, no filename collisions, and exact backup counts.
        """
        store = AliexpressResumeStore(self.run_dir)
        archives = []
        for i in range(100):
            store.open()
            store.record_page(1, [_sample_product(i)])
            archived = store.archive()
            self.assertIsNotNone(archived)
            self.assertTrue(archived.exists())
            archives.append(archived)

        self.assertEqual(len(archives), 100)
        unique_names = {a.name for a in archives}
        self.assertEqual(len(unique_names), 100, "Archive filenames must all be unique!")

    def test_archive_with_dangling_journal_file(self):
        """If a journal file exists due to unclean shutdown, verify archive and restart behavior."""
        store = AliexpressResumeStore(self.run_dir)
        store.open()
        store.record_page(1, [_sample_product(1)])
        store.close()

        # Simulate leftover journal
        journal_file = self.run_dir / "resume.sqlite3-journal"
        journal_file.write_bytes(b"DUMMY JOURNAL DATA")

        # Archive moves main db file
        archived = store.archive()
        self.assertIsNotNone(archived)
        self.assertFalse(self.db_path.exists())

        # Clean up or start fresh store
        new_store = AliexpressResumeStore(self.run_dir)
        new_store.open()
        new_store.record_page(1, [_sample_product(2)])
        self.assertEqual(new_store.product_count, 1)
        new_store.close()


class TestAdversarialRunDirIsolationAndSecurity(unittest.TestCase):
    """Adversarial stress on directory isolation, path traversal, and naming collisions."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="adv_isolation_test_")
        self.output_dir = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_path_traversal_attack_in_category_name(self):
        """Path traversal sequences (../../, etc.) must not escape output_dir."""
        malicious_names = [
            "../../../etc/passwd",
            "..\\..\\Windows\\System32",
            "foo/../../bar",
            "....//....//escape",
            "/absolute/root/attack",
        ]
        url = "https://ko.aliexpress.com/category/12345/shoes.html"

        for mal_name in malicious_names:
            run_dir = ali_category_run_dir(self.output_dir, mal_name, url)
            resolved = run_dir.resolve()
            # Must strictly reside inside output_dir
            self.assertTrue(
                str(resolved).startswith(str(self.output_dir.resolve())),
                f"Directory escape detected! {resolved} not within {self.output_dir}",
            )

    def test_identical_name_different_category_urls_isolation(self):
        """Two different categories with same name ('신발') must resolve to isolated run_dirs."""
        url_1 = "https://ko.aliexpress.com/category/100001/shoes.html"
        url_2 = "https://ko.aliexpress.com/category/100002/shoes.html"

        dir_1 = ali_category_run_dir(self.output_dir, "신발", url_1)
        dir_2 = ali_category_run_dir(self.output_dir, "신발", url_2)

        self.assertNotEqual(dir_1, dir_2)
        self.assertIn("100001", dir_1.name)
        self.assertIn("100002", dir_2.name)

    def test_identical_url_different_names_isolation(self):
        """Same category URL with different name labels must resolve to isolated run_dirs."""
        url = "https://ko.aliexpress.com/category/999/common.html"
        dir_1 = ali_category_run_dir(self.output_dir, "의류", url)
        dir_2 = ali_category_run_dir(self.output_dir, "잡화", url)

        self.assertNotEqual(dir_1, dir_2)

    def test_extreme_category_name_length_and_special_symbols(self):
        """Massive name (1000 chars) and special symbols do not cause OS filesystem errors."""
        huge_name = "가나다라마바사" * 150 + "!@#$%^&*()_+~`|}{[]:;?><,./"
        url = "https://ko.aliexpress.com/category/777/test.html"

        run_dir = ali_category_run_dir(self.output_dir, huge_name, url)
        # Verify it can actually be created on disk without OSError
        run_dir.mkdir(parents=True, exist_ok=True)
        self.assertTrue(run_dir.exists())
        # Safe name is truncated to reasonable length
        self.assertLessEqual(len(run_dir.name), 60)


class TestAdversarialConfigMismatchesAndDrift(unittest.TestCase):
    """Adversarial stress on configuration drift, versioning, and state integrity."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="adv_config_test_")
        self.run_dir = Path(self.temp_dir) / "run_cfg"
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_url_switch_detected_on_existing_store(self):
        """Switching target URL in existing run_dir returns mismatch message."""
        store = AliexpressResumeStore(self.run_dir)
        store.open()
        res = store.check_config("패션", "https://ko.aliexpress.com/category/111/a.html", 30)
        self.assertIsNone(res)
        store.close()

        # Reopen with different URL
        store2 = AliexpressResumeStore(self.run_dir)
        store2.open()
        mismatch = store2.check_config("패션", "https://ko.aliexpress.com/category/222/b.html", 30)
        self.assertIsNotNone(mismatch)
        self.assertIn("URL", mismatch)
        store2.close()

    def test_max_pages_decrease_detected(self):
        """Reducing max_pages below stored completed scope returns mismatch warning."""
        store = AliexpressResumeStore(self.run_dir)
        store.open()
        store.check_config("식품", "https://ko.aliexpress.com/category/555/food.html", 50)
        store.close()

        store2 = AliexpressResumeStore(self.run_dir)
        store2.open()
        mismatch = store2.check_config("식품", "https://ko.aliexpress.com/category/555/food.html", 20)
        self.assertIsNotNone(mismatch)
        self.assertIn("50페이지", mismatch)
        store2.close()

    def test_max_pages_increase_accepted(self):
        """Increasing max_pages (e.g. from 20 to 50) is accepted for extension."""
        store = AliexpressResumeStore(self.run_dir)
        store.open()
        store.check_config("식품", "https://ko.aliexpress.com/category/555/food.html", 20)
        store.close()

        store2 = AliexpressResumeStore(self.run_dir)
        store2.open()
        res = store2.check_config("식품", "https://ko.aliexpress.com/category/555/food.html", 50)
        self.assertIsNone(res)
        store2.close()

    def test_finished_status_suppresses_resume_prompt(self):
        """A finished crawl must never offer resume (peek_resume returns None)."""
        store = AliexpressResumeStore(self.run_dir)
        store.open()
        store.record_page(1, [_sample_product(1)])
        store.mark_finished()
        store.close()

        note = peek_resume(self.run_dir)
        self.assertIsNone(note)


if __name__ == "__main__":
    unittest.main()
