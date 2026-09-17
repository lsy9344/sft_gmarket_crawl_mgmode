"""AliExpress 카테고리 수집 진행 저장·재개 단위 및 복원력 테스트 (WORK_ORDER_CATEGORY_RESUME 및 PROJECT.md 준용).

AliexpressResumeStore의 SQLite 수명 주기, Phase 1 목록 페이지 단위 원자적 저장 및 복원,
Phase 2 판매자 단위 실시간 저장 및 공정위 7대 사업자 정보 매핑, 손상/권한 예외 방어,
설정 불일치 감지, 완료 봉인 후 아카이브 안전성 및 신규 실행 격리를 검증한다.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from app.core.aliexpress_resume_store import (
        AliexpressResumeStore,
        ResumeStoreError,
        ResumeConfigMismatch,
        ali_category_run_dir,
        peek_resume,
        STATUS_LISTING,
        STATUS_LISTING_DONE,
        STATUS_FINISHED,
        SCHEMA_VERSION,
    )
    _RESUME_AVAILABLE = True
except ImportError as e:
    _RESUME_AVAILABLE = False
    _IMPORT_ERROR = str(e)


def _make_product(item_id: str, title: str = "테스트 상품", price: str = "₩10,000") -> dict:
    return {
        "id": str(item_id),
        "url": f"https://ko.aliexpress.com/item/{item_id}.html",
        "title": title,
        "price": price,
        "orders": "100+ 판매",
        "is_top_seller": True,
    }


def _make_seller(vendor_id: str, company: str = "(주)테스트상사", bnum: str = "123-45-67890") -> dict:
    return {
        "store_name": f"스토어_{vendor_id}",
        "company_name": company,
        "ceo_name": "홍길동",
        "business_number": bnum,
        "phone": "02-1234-5678",
        "email": f"seller_{vendor_id}@example.com",
        "address": "서울시 강남구 테헤란로 123",
        "ecommerce_report_number": "2026-서울강남-0123",
    }


class TestModuleAvailability(unittest.TestCase):
    """모듈 존재 여부 및 기본 무결성 검증."""

    def test_resume_store_module_importable(self):
        self.assertTrue(
            _RESUME_AVAILABLE,
            f"app.core.aliexpress_resume_store 모듈을 불러올 수 없습니다: {_IMPORT_ERROR if not _RESUME_AVAILABLE else ''}",
        )


@unittest.skipUnless(_RESUME_AVAILABLE, "AliexpressResumeStore 구현 대기")
class TestAliCategoryRunDir(unittest.TestCase):
    """카테고리별 격리 출력 디렉터리 생성 규칙 검증."""

    def test_ali_category_run_dir_with_category_id_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = "https://ko.aliexpress.com/category/200000343/vegetables.html"
            run_dir = ali_category_run_dir(Path(tmp), "신선 채소", url)
            self.assertTrue(str(run_dir).startswith(tmp))
            self.assertIn("200000343", run_dir.name)
            self.assertIn("신선_채소", run_dir.name)

    def test_ali_category_run_dir_with_wholesale_query_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = "https://ko.aliexpress.com/w/wholesale-nuts.html?g=y&SearchText=nuts"
            run_dir = ali_category_run_dir(Path(tmp), "견과류", url)
            self.assertTrue(str(run_dir).startswith(tmp))
            self.assertIn("견과류", run_dir.name)
            self.assertTrue(len(run_dir.name.split("_")[-1]) >= 6)

    def test_ali_category_run_dir_sanitizes_unsafe_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = "https://ko.aliexpress.com/category/10001/fruits.html"
            run_dir = ali_category_run_dir(Path(tmp), "과일/채소:특가*견과?", url)
            self.assertNotIn("/", run_dir.name)
            self.assertNotIn(":", run_dir.name)
            self.assertNotIn("*", run_dir.name)
            self.assertNotIn("?", run_dir.name)

    def test_ali_category_run_dir_empty_name_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = "https://ko.aliexpress.com/category/12345/test.html"
            run_dir = ali_category_run_dir(Path(tmp), "   ", url)
            self.assertTrue(len(run_dir.name) > 0)
            self.assertIn("12345", run_dir.name)

    def test_ali_category_run_dir_returns_path_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = ali_category_run_dir(tmp, "채소", "https://ko.aliexpress.com/category/999/c.html")
            self.assertIsInstance(run_dir, Path)


@unittest.skipUnless(_RESUME_AVAILABLE, "AliexpressResumeStore 구현 대기")
class TestResumeStoreLifecycle(unittest.TestCase):
    """Tier 1 - Feature 3: ResumeStore 라이프사이클, open, check_config, archive 검증 (>=5 tests)."""

    def test_open_creates_database_and_schema_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AliexpressResumeStore(Path(tmp) / "resume.sqlite3")
            store.open()
            self.assertTrue((Path(tmp) / "resume.sqlite3").exists())
            # SQLite 테이블 구조 검증
            cursor = store.conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cursor.fetchall()}
            self.assertIn("meta", tables)
            self.assertIn("pages", tables)
            self.assertIn("products", tables)
            self.assertIn("sellers", tables)
            store.close()

    def test_open_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AliexpressResumeStore(Path(tmp) / "resume.sqlite3")
            store.open()
            store.open()  # 두 번째 호출도 안전
            self.assertIsNotNone(store.conn)
            store.close()

    def test_context_manager_opens_and_closes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "resume.sqlite3"
            with AliexpressResumeStore(db_path) as store:
                self.assertIsNotNone(store.conn)
                self.assertTrue(db_path.exists())
            # 컨텍스트 종료 후 커넥션 닫힘
            self.assertIsNone(store._conn)

    def test_check_config_initial_run_saves_parameters(self):
        with tempfile.TemporaryDirectory() as tmp:
            with AliexpressResumeStore(Path(tmp) / "resume.sqlite3") as store:
                mismatch = store.check_config("채소", "https://ko.aliexpress.com/category/100/veg.html", 20)
                self.assertIsNone(mismatch)
                self.assertEqual(store.status, STATUS_LISTING)
                self.assertEqual(store.last_completed_page, 0)
                self.assertEqual(store.product_count, 0)

    def test_check_config_matching_run_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "resume.sqlite3"
            with AliexpressResumeStore(db_path) as store:
                store.check_config("채소", "https://ko.aliexpress.com/category/100/veg.html", 20)
                store.record_page(1, [_make_product("101")])

            # 새 인스턴스로 동일 설정 검사
            with AliexpressResumeStore(db_path) as store2:
                mismatch = store2.check_config("채소", "https://ko.aliexpress.com/category/100/veg.html", 20)
                self.assertIsNone(mismatch)
                self.assertEqual(store2.last_completed_page, 1)
                self.assertEqual(store2.product_count, 1)

    def test_archive_moves_database_atomically_and_resets(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "resume.sqlite3"
            with AliexpressResumeStore(db_path) as store:
                store.check_config("채소", "https://ko.aliexpress.com/category/100/veg.html", 20)
                store.record_page(1, [_make_product("101")])
                archived_path = store.archive()

            self.assertIsNotNone(archived_path)
            self.assertTrue(archived_path.exists())
            self.assertIn("resume_archive_", archived_path.name)
            self.assertFalse(db_path.exists())

    def test_peek_resume_returns_formatted_preview_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            with AliexpressResumeStore(Path(tmp) / "resume.sqlite3") as store:
                store.check_config("채소", "https://ko.aliexpress.com/category/100/veg.html", 20)
                store.record_page(1, [_make_product("101"), _make_product("102")])
                store.record_seller("V001", _make_seller("V001"))

            preview = peek_resume(Path(tmp))
            self.assertIsNotNone(preview)
            self.assertIn("페이지 1", preview)
            self.assertIn("2개", preview)
            self.assertIn("1명", preview)


@unittest.skipUnless(_RESUME_AVAILABLE, "AliexpressResumeStore 구현 대기")
class TestPhase1ListingPersistence(unittest.TestCase):
    """Tier 1 - Feature 4: Phase 1 목록 탐색 페이지 단위 영속화 및 복원 (>=5 tests)."""

    def test_record_page_atomic_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            with AliexpressResumeStore(Path(tmp) / "resume.sqlite3") as store:
                store.check_config("과일", "https://ko.aliexpress.com/category/200/fruits.html", 10)
                items = [_make_product("p1"), _make_product("p2")]
                store.record_page(1, items)

                self.assertEqual(store.last_completed_page, 1)
                self.assertEqual(store.product_count, 2)
                loaded = store.load_products()
                self.assertEqual(len(loaded), 2)
                self.assertEqual({p["id"] for p in loaded}, {"p1", "p2"})

    def test_multiple_consecutive_pages_aggregation(self):
        with tempfile.TemporaryDirectory() as tmp:
            with AliexpressResumeStore(Path(tmp) / "resume.sqlite3") as store:
                store.check_config("과일", "https://ko.aliexpress.com/category/200/fruits.html", 10)
                store.record_page(1, [_make_product("p1"), _make_product("p2")])
                store.record_page(2, [_make_product("p3")])
                store.record_page(3, [_make_product("p4"), _make_product("p5")])

                self.assertEqual(store.last_completed_page, 3)
                self.assertEqual(store.product_count, 5)

            # 새 인스턴스로 다시 열어도 완벽히 보존
            with AliexpressResumeStore(Path(tmp) / "resume.sqlite3") as store2:
                self.assertEqual(store2.last_completed_page, 3)
                self.assertEqual(store2.product_count, 5)
                prods = store2.load_products()
                self.assertEqual(len(prods), 5)

    def test_load_products_preserves_attributes_and_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            with AliexpressResumeStore(Path(tmp) / "resume.sqlite3") as store:
                store.check_config("과일", "https://ko.aliexpress.com/category/200/fruits.html", 10)
                orig = _make_product("item999", title="제주 하우스 감귤 5kg", price="₩25,000")
                store.record_page(1, [orig])

                loaded = store.load_products()
                self.assertEqual(len(loaded), 1)
                self.assertEqual(loaded[0]["id"], "item999")
                self.assertEqual(loaded[0]["title"], "제주 하우스 감귤 5kg")
                self.assertEqual(loaded[0]["price"], "₩25,000")
                self.assertTrue(loaded[0]["url"].endswith("item999.html"))

    def test_record_page_duplicate_product_deduplication(self):
        with tempfile.TemporaryDirectory() as tmp:
            with AliexpressResumeStore(Path(tmp) / "resume.sqlite3") as store:
                store.check_config("과일", "https://ko.aliexpress.com/category/200/fruits.html", 10)
                # 페이지 1에 p1 존재
                store.record_page(1, [_make_product("p1", title="원래 제목")])
                # 페이지 2에 p1 재등장 (카테고리 랭킹 변동으로 중복 노출)
                store.record_page(2, [_make_product("p1", title="갱신 제목"), _make_product("p2")])

                self.assertEqual(store.last_completed_page, 2)
                self.assertEqual(store.product_count, 2)
                loaded = store.load_products()
                self.assertEqual(len(loaded), 2)

    def test_mark_listing_done_transitions_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            with AliexpressResumeStore(Path(tmp) / "resume.sqlite3") as store:
                store.check_config("과일", "https://ko.aliexpress.com/category/200/fruits.html", 10)
                store.record_page(1, [_make_product("p1")])
                self.assertEqual(store.status, STATUS_LISTING)

                store.mark_listing_done(end_reason="max_pages")
                self.assertEqual(store.status, STATUS_LISTING_DONE)

            # 재개 시에도 status 유지
            with AliexpressResumeStore(Path(tmp) / "resume.sqlite3") as store2:
                self.assertEqual(store2.status, STATUS_LISTING_DONE)

    def test_empty_page_records_and_increments_streak(self):
        with tempfile.TemporaryDirectory() as tmp:
            with AliexpressResumeStore(Path(tmp) / "resume.sqlite3") as store:
                store.check_config("과일", "https://ko.aliexpress.com/category/200/fruits.html", 10)
                store.record_page(1, [_make_product("p1")])
                store.record_page(2, [])  # 빈 페이지

                self.assertEqual(store.last_completed_page, 2)
                self.assertEqual(store.product_count, 1)


@unittest.skipUnless(_RESUME_AVAILABLE, "AliexpressResumeStore 구현 대기")
class TestPhase2SellerPersistence(unittest.TestCase):
    """Tier 1 - Feature 5: Phase 2 판매자 저장 및 공정위 7대 사업자 정보 매핑 (>=5 tests)."""

    def test_record_seller_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with AliexpressResumeStore(Path(tmp) / "resume.sqlite3") as store:
                store.check_config("견과", "https://ko.aliexpress.com/category/300/nuts.html", 5)
                seller_data = _make_seller("V100", company="(주)해피푸드", bnum="111-22-33333")
                store.record_seller("V100", seller_data)

                self.assertEqual(store.confirmed_seller_count, 1)
                confirmed = store.confirmed_sellers()
                self.assertIn("V100", confirmed)
                self.assertEqual(confirmed["V100"]["company_name"], "(주)해피푸드")

    def test_confirmed_sellers_retrieves_all_cached_sellers(self):
        with tempfile.TemporaryDirectory() as tmp:
            with AliexpressResumeStore(Path(tmp) / "resume.sqlite3") as store:
                store.check_config("견과", "https://ko.aliexpress.com/category/300/nuts.html", 5)
                store.record_seller("V1", _make_seller("V1", company="상호A"))
                store.record_seller("V2", _make_seller("V2", company="상호B"))
                store.record_seller("V3", _make_seller("V3", company="상호C"))

                self.assertEqual(store.confirmed_seller_count, 3)
                sellers = store.confirmed_sellers()
                self.assertEqual(len(sellers), 3)
                self.assertEqual({s["company_name"] for s in sellers.values()}, {"상호A", "상호B", "상호C"})

    def test_record_item_result_and_processed_item_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            with AliexpressResumeStore(Path(tmp) / "resume.sqlite3") as store:
                store.check_config("견과", "https://ko.aliexpress.com/category/300/nuts.html", 5)
                store.record_item_result("item_1", "V1", {"price": "1000"})
                store.record_item_result("item_2", "V1", {"price": "2000"})

                processed = store.processed_item_ids()
                self.assertEqual(processed, {"item_1", "item_2"})

    def test_core_7_business_fields_saved_accurately(self):
        with tempfile.TemporaryDirectory() as tmp:
            with AliexpressResumeStore(Path(tmp) / "resume.sqlite3") as store:
                store.check_config("견과", "https://ko.aliexpress.com/category/300/nuts.html", 5)
                exact_fields = {
                    "store_name": "글로벌 직구 스토어",
                    "company_name": "(주)알리유통코리아",
                    "ceo_name": "김대표",
                    "business_number": "505-81-12345",
                    "phone": "070-9876-5432",
                    "email": "contact@ali-korea.co.kr",
                    "address": "인천시 연수구 송도미래로 30",
                    "ecommerce_report_number": "2026-인천연수-0789",
                }
                store.record_seller("V_EXACT", exact_fields)

                restored = store.confirmed_sellers()["V_EXACT"]
                self.assertEqual(restored["company_name"], "(주)알리유통코리아")
                self.assertEqual(restored["ceo_name"], "김대표")
                self.assertEqual(restored["business_number"], "505-81-12345")
                self.assertEqual(restored["phone"], "070-9876-5432")
                self.assertEqual(restored["email"], "contact@ali-korea.co.kr")
                self.assertEqual(restored["address"], "인천시 연수구 송도미래로 30")
                self.assertEqual(restored["ecommerce_report_number"], "2026-인천연수-0789")

    def test_mark_finished_seals_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            with AliexpressResumeStore(Path(tmp) / "resume.sqlite3") as store:
                store.check_config("견과", "https://ko.aliexpress.com/category/300/nuts.html", 5)
                store.record_page(1, [_make_product("p1")])
                store.record_seller("V1", _make_seller("V1"))
                store.mark_finished()
                self.assertEqual(store.status, STATUS_FINISHED)

            # 완료 봉인 후 peek_resume는 None 반환 (재개 대상 아님)
            self.assertIsNone(peek_resume(Path(tmp)))

    def test_seller_status_unconfirmed_handling(self):
        with tempfile.TemporaryDirectory() as tmp:
            with AliexpressResumeStore(Path(tmp) / "resume.sqlite3") as store:
                store.check_config("견과", "https://ko.aliexpress.com/category/300/nuts.html", 5)
                # 빈 데이터나 에러 판매자는 confirmed_sellers에 포함되지 않거나 미기재 처리
                store.record_seller("V_EMPTY", {"company_name": "(상세 미기재)"})
                confirmed = store.confirmed_sellers()
                self.assertIn("V_EMPTY", confirmed)
                self.assertEqual(confirmed["V_EMPTY"]["company_name"], "(상세 미기재)")


@unittest.skipUnless(_RESUME_AVAILABLE, "AliexpressResumeStore 구현 대기")
class TestCorruptedDatabaseAndWriteErrors(unittest.TestCase):
    """Tier 2 - Boundary 2: 손상된 SQLite DB 및 쓰기 권한 오류 방어 (>=5 tests)."""

    def test_corrupted_sqlite_header_raises_store_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "resume.sqlite3"
            db_path.write_bytes(b"CORRUPTED_NON_SQLITE_GARBAGE_HEADER_DATA_12345" * 10)

            store = AliexpressResumeStore(db_path)
            with self.assertRaises(ResumeStoreError):
                store.open()
                store.check_config("채소", "http://example.com", 10)

    def test_peek_resume_on_corrupted_file_returns_none_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "resume.sqlite3"
            db_path.write_bytes(b"THIS_IS_TOTALLY_CORRUPTED_DATA_AND_NOT_SQLITE")

            # 앱 크래시 없이 안전하게 None 반환
            preview = peek_resume(Path(tmp))
            self.assertIsNone(preview)

    def test_write_permission_error_raises_resume_store_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 쓰기 불가능한 읽기 전용 가짜 연결 혹은 닫힌 DB에 쓰기 시도
            store = AliexpressResumeStore(Path(tmp) / "resume.sqlite3")
            store.open()
            store.close()
            # 닫힌 상태에서 쓰기 시도 시 ResumeStoreError 발생
            with self.assertRaises(ResumeStoreError):
                store.record_page(1, [_make_product("p1")])

    def test_corrupted_json_payload_in_products_raises_store_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "resume.sqlite3"
            with AliexpressResumeStore(db_path) as store:
                store.check_config("채소", "http://example.com", 10)
                # 데이터베이스 내 직접 손상된 JSON 주입
                store.conn.execute(
                    "INSERT INTO products (id, payload, page_no) VALUES (?, ?, ?)",
                    ("bad_item", "{invalid_json_missing_quotes: True", 1),
                )
                store.conn.commit()

                with self.assertRaises(ResumeStoreError):
                    store.load_products()

    def test_corrupted_json_payload_in_sellers_raises_store_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "resume.sqlite3"
            with AliexpressResumeStore(db_path) as store:
                store.check_config("채소", "http://example.com", 10)
                store.conn.execute(
                    "INSERT INTO sellers (vendor_id, status, payload, completed_at) VALUES (?, ?, ?, ?)",
                    ("V_BAD", "confirmed", "broken_payload_string_not_json", "2026-09-17"),
                )
                store.conn.commit()

                with self.assertRaises(ResumeStoreError):
                    store.confirmed_sellers()


@unittest.skipUnless(_RESUME_AVAILABLE, "AliexpressResumeStore 구현 대기")
class TestConfigurationMismatch(unittest.TestCase):
    """Tier 2 - Boundary 5: 설정 불일치 감지 및 안전 중단 (>=5 tests)."""

    def test_category_url_mismatch_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "resume.sqlite3"
            with AliexpressResumeStore(db_path) as store:
                store.check_config("채소", "https://ko.aliexpress.com/category/100/veg.html", 20)

            with AliexpressResumeStore(db_path) as store2:
                mismatch = store2.check_config("채소", "https://ko.aliexpress.com/category/999/different.html", 20)
                self.assertIsNotNone(mismatch)
                self.assertIn("URL", mismatch)

    def test_category_name_or_url_mismatch_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "resume.sqlite3"
            with AliexpressResumeStore(db_path) as store:
                store.check_config("신선과일", "https://ko.aliexpress.com/category/100/items.html", 20)

            with AliexpressResumeStore(db_path) as store2:
                mismatch = store2.check_config("전자기기", "https://ko.aliexpress.com/category/200/electronics.html", 20)
                self.assertIsNotNone(mismatch)
                self.assertIn("URL", mismatch)

    def test_max_pages_mismatch_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "resume.sqlite3"
            with AliexpressResumeStore(db_path) as store:
                store.check_config("채소", "https://ko.aliexpress.com/category/100/veg.html", 50)
                store.record_page(1, [_make_product("p1")])

            with AliexpressResumeStore(db_path) as store2:
                # 50페이지로 시작했으나 5페이지로 축소 시도
                mismatch = store2.check_config("채소", "https://ko.aliexpress.com/category/100/veg.html", 5)
                self.assertIsNotNone(mismatch)
                self.assertIn("페이지", mismatch)

    def test_config_mismatch_preserves_existing_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "resume.sqlite3"
            with AliexpressResumeStore(db_path) as store:
                store.check_config("채소", "https://ko.aliexpress.com/category/100/veg.html", 20)
                store.record_page(1, [_make_product("p1")])

            with AliexpressResumeStore(db_path) as store2:
                store2.check_config("채소", "https://ko.aliexpress.com/category/999/other.html", 20)

            # 불일치 확인 후에도 기존 p1은 그대로 보존되어야 함
            with AliexpressResumeStore(db_path) as store3:
                self.assertEqual(store3.product_count, 1)
                self.assertEqual(store3.last_completed_page, 1)

    def test_schema_version_stored_in_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "resume.sqlite3"
            with AliexpressResumeStore(db_path) as store:
                store.check_config("채소", "https://ko.aliexpress.com/category/100/veg.html", 20)
                stored_ver = store._get_meta("schema_version")
                self.assertEqual(stored_ver, str(SCHEMA_VERSION))


@unittest.skipUnless(_RESUME_AVAILABLE, "AliexpressResumeStore 구현 대기")
class TestReRunningOnCompletedCategory(unittest.TestCase):
    """Tier 2 - Boundary 6: 완료된 카테고리 재실행 및 아카이브 안전성 (>=5 tests)."""

    def test_completed_category_peek_resume_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with AliexpressResumeStore(Path(tmp) / "resume.sqlite3") as store:
                store.check_config("채소", "http://example.com", 5)
                store.record_page(1, [_make_product("p1")])
                store.mark_finished()

            self.assertIsNone(peek_resume(Path(tmp)))

    def test_archive_on_completed_store_moves_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "resume.sqlite3"
            with AliexpressResumeStore(db_path) as store:
                store.check_config("채소", "http://example.com", 5)
                store.record_page(1, [_make_product("p1")])
                store.mark_finished()
                archived = store.archive()

            self.assertIsNotNone(archived)
            self.assertTrue(archived.exists())
            self.assertFalse(db_path.exists())

    def test_consecutive_archives_generate_unique_filenames(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "resume.sqlite3"
            archives = []
            for i in range(3):
                with AliexpressResumeStore(db_path) as store:
                    store.check_config("채소", "http://example.com", 5)
                    store.record_page(1, [_make_product(f"p_{i}")])
                    archived = store.archive()
                    archives.append(archived)

            self.assertEqual(len(archives), 3)
            # 3개 아카이브 파일명이 모두 달라야 함
            names = {a.name for a in archives}
            self.assertEqual(len(names), 3)
            for a in archives:
                self.assertTrue(a.exists())

    def test_archive_when_file_not_exist_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "non_existent_resume.sqlite3"
            store = AliexpressResumeStore(db_path)
            res = store.archive()
            self.assertIsNone(res)

    def test_fresh_start_after_archive_creates_clean_empty_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "resume.sqlite3"
            with AliexpressResumeStore(db_path) as store:
                store.check_config("채소", "http://example.com", 5)
                store.record_page(1, [_make_product("p1"), _make_product("p2")])
                store.record_seller("V1", _make_seller("V1"))
                store.archive()

            # 새 저장소 열기
            with AliexpressResumeStore(db_path) as store2:
                store2.check_config("채소", "http://example.com", 5)
                self.assertEqual(store2.last_completed_page, 0)
                self.assertEqual(store2.product_count, 0)
                self.assertEqual(store2.confirmed_seller_count, 0)
                self.assertEqual(len(store2.load_products()), 0)


@unittest.skipUnless(_RESUME_AVAILABLE, "AliexpressResumeStore 구현 대기")
class TestFreshStartAfterInterruption(unittest.TestCase):
    """Tier 3 - Cross-Feature 3: 중단 후 '처음부터 다시 수집' 시 아카이브 보존 검증."""

    def test_fresh_start_after_phase1_interruption_preserves_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run_cat_001"
            run_dir.mkdir(parents=True)
            db_path = run_dir / "resume.sqlite3"

            # 1차 시도: 1~2페이지 수집 후 중단
            with AliexpressResumeStore(db_path) as store1:
                store1.check_config("신선과일", "https://ko.aliexpress.com/cat/fruit.html", 10)
                store1.record_page(1, [_make_product("p1"), _make_product("p2")])
                store1.record_page(2, [_make_product("p3"), _make_product("p4")])

            # 재개 감지 확인
            self.assertIsNotNone(peek_resume(run_dir))

            # 사용자가 [처음부터 다시 수집] 선택 -> archive 호출 후 신규 생성
            with AliexpressResumeStore(db_path) as store_arch:
                arch_file = store_arch.archive()
                self.assertIsNotNone(arch_file)

            # 아카이브 파일 내 4개 상품이 완벽히 보존되었는지 검증
            with AliexpressResumeStore(arch_file) as arch_store:
                self.assertEqual(arch_store.product_count, 4)
                self.assertEqual(arch_store.last_completed_page, 2)

            # 신규 수집은 깨끗하게 0부터 시작
            with AliexpressResumeStore(db_path) as store2:
                store2.check_config("신선과일", "https://ko.aliexpress.com/cat/fruit.html", 10)
                self.assertEqual(store2.last_completed_page, 0)
                self.assertEqual(store2.product_count, 0)

    def test_fresh_start_after_phase2_interruption_preserves_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run_cat_002"
            run_dir.mkdir(parents=True)
            db_path = run_dir / "resume.sqlite3"

            # Phase 1 완료 및 Phase 2 2명 판매자 수집 후 중단
            with AliexpressResumeStore(db_path) as store1:
                store1.check_config("견과류", "https://ko.aliexpress.com/cat/nuts.html", 10)
                store1.record_page(1, [_make_product("n1"), _make_product("n2")])
                store1.mark_listing_done()
                store1.record_seller("V_NUTS_1", _make_seller("V_NUTS_1", company="호두상사"))
                store1.record_seller("V_NUTS_2", _make_seller("V_NUTS_2", company="아몬드유통"))

            with AliexpressResumeStore(db_path) as store_arch:
                arch_file = store_arch.archive()

            # 백업 내 판매자 정보 보존 확인
            with AliexpressResumeStore(arch_file) as arch_store:
                self.assertEqual(arch_store.confirmed_seller_count, 2)
                self.assertIn("V_NUTS_1", arch_store.confirmed_sellers())

            # 신규 시작 확인
            with AliexpressResumeStore(db_path) as store2:
                store2.check_config("견과류", "https://ko.aliexpress.com/cat/nuts.html", 10)
                self.assertEqual(store2.confirmed_seller_count, 0)


if __name__ == "__main__":
    unittest.main()
