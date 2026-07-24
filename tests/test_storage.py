"""app.core.storage 회귀 테스트: 영속성, CSV 인젝션 방지, 파일명 충돌 회피,
손상 상태 파일 격리, 부분 결과 체크포인트."""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from app.core import config
from app.core import storage as storage_module
from app.core.storage import LoadStatus, Storage
from app.models.records import RECORD_FIELDS


def _tmp_storage() -> Storage:
    return Storage(tempfile.mkdtemp())


class CollectedIdsTest(unittest.TestCase):
    def test_round_trip_sorted(self) -> None:
        st = _tmp_storage()
        st.save_collected_ids({"333", "111", "222"})
        self.assertEqual(st.load_collected_ids(), {"111", "222", "333"})
        self.assertEqual(
            json.loads(st.collected_ids_path.read_text()), ["111", "222", "333"]
        )

    def test_missing_file_returns_empty_set(self) -> None:
        st = _tmp_storage()
        self.assertEqual(st.load_collected_ids(), set())

    def test_corrupt_file_is_quarantined_not_silently_dropped(self) -> None:
        st = _tmp_storage()
        st.collected_ids_path.write_text("{not valid json", encoding="utf-8")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            ids = st.load_collected_ids()
        self.assertEqual(ids, set())
        self.assertTrue(any(issubclass(w.category, RuntimeWarning) for w in caught))
        # 원본 내용이 보존된 백업 파일이 남아 있어야 한다(데이터 유실 방지).
        # 5차 리뷰 HIGH: 백업은 .bak 확장자를 써서 .json 체크포인트 glob 과
        # 절대 겹치지 않는다(재귀적 손상 격리/오삭제 방지).
        backups = list(st.output_dir.glob("collected_ids.json.corrupt_*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertIn("not valid json", backups[0].read_text(encoding="utf-8"))
        # 원본 경로 자체는 더 이상 손상된 내용을 갖지 않는다(다음 save 로 재생성됨).
        self.assertFalse(st.collected_ids_path.exists())

    def test_semantically_wrong_type_is_quarantined(self) -> None:
        """MEDIUM-2 회귀: 문법적으로는 유효한 JSON 이지만 최상위 타입이 다르면
        (list 기대 vs dict/문자열 등) 크래시 대신 격리 처리해야 한다."""
        st = _tmp_storage()
        st.collected_ids_path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            ids = st.load_collected_ids()
        self.assertEqual(ids, set())
        self.assertTrue(any(issubclass(w.category, RuntimeWarning) for w in caught))
        self.assertEqual(len(list(st.output_dir.glob("collected_ids.json.corrupt_*.bak"))), 1)


class StateTest(unittest.TestCase):
    def test_mark_completed_accumulates_and_dedups(self) -> None:
        st = _tmp_storage()
        st.reset_state()
        st.mark_completed("가공식품", 63)
        st.mark_completed("신선식품", 42)
        state = st.mark_completed("가공식품", 5)  # 같은 카테고리 재호출: 이름 중복 안 됨
        self.assertEqual(state["completed"], ["가공식품", "신선식품"])
        self.assertEqual(state["total_success"], 63 + 42 + 5)

    def test_corrupt_state_quarantined(self) -> None:
        st = _tmp_storage()
        st.state_path.write_text("{bad", encoding="utf-8")
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            state = st.load_state()
        self.assertEqual(state, {"completed": [], "total_success": 0})
        self.assertEqual(len(list(st.output_dir.glob("fastcrawl_state.json.corrupt_*.bak"))), 1)

    def test_state_as_json_array_does_not_crash(self) -> None:
        """MEDIUM-2 회귀: fastcrawl_state.json 이 (문법은 유효한) list 이면
        예전엔 state.setdefault(...) 에서 AttributeError 로 크래시했다."""
        st = _tmp_storage()
        st.state_path.write_text(json.dumps(["신선식품", "가공식품"]), encoding="utf-8")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            state = st.load_state()
        self.assertEqual(state, {"completed": [], "total_success": 0})
        self.assertTrue(any(issubclass(w.category, RuntimeWarning) for w in caught))

    def test_state_with_wrong_field_types_quarantined(self) -> None:
        st = _tmp_storage()
        st.state_path.write_text(
            json.dumps({"completed": "가공식품", "total_success": "many"}), encoding="utf-8"
        )
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            state = st.load_state()
        self.assertEqual(state, {"completed": [], "total_success": 0})


class SaveResultsTest(unittest.TestCase):
    def _sample_record(self, **overrides) -> dict:
        rec = {
            "goodscode": "1", "url": "u", "store_name": "스토어",
            "company_name": "에스엠맨테크", "ceo_name": "", "email": "a@b.co",
            "phone": "02-1", "business_number": "123", "address": "서울",
            "collected_at": "2026-07-24T11:00:00", "source": "가공식품",
        }
        rec.update(overrides)
        return rec

    def test_json_and_csv_written_with_bom_and_header_order(self) -> None:
        st = _tmp_storage()
        jp, cp = st.save_results([self._sample_record()], label="생필품/육아")
        self.assertTrue(jp.exists() and cp.exists())
        self.assertIn("생필품_육아", jp.name)
        self.assertNotIn("/", jp.name)

        raw = cp.read_bytes()
        self.assertEqual(raw[:3], b"\xef\xbb\xbf")  # utf-8-sig BOM

        with open(cp, encoding="utf-8-sig") as f:
            header = next(csv.reader(f))
        self.assertEqual(header, RECORD_FIELDS)

    def test_empty_results_still_writes_header_only_csv(self) -> None:
        st = _tmp_storage()
        _, cp = st.save_results([], label="ALL")
        with open(cp, encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows, [RECORD_FIELDS])

    def test_atomic_write_leaves_no_tmp_files(self) -> None:
        st = _tmp_storage()
        st.save_collected_ids({"1"})
        st.save_results([self._sample_record()], label="x")
        self.assertFalse(any(p.name.endswith(".tmp") for p in st.output_dir.iterdir()))

    def test_filename_collision_gets_unique_suffix(self) -> None:
        st = _tmp_storage()
        jp1, cp1 = st.save_results([self._sample_record()], label="가공식품")
        # 같은 초에 같은 라벨로 즉시 재저장 — 타임스탬프가 같을 수 있다.
        jp2, cp2 = st.save_results([self._sample_record()], label="가공식품")
        self.assertNotEqual(jp1, jp2)
        self.assertNotEqual(cp1, cp2)
        self.assertTrue(jp1.exists() and jp2.exists())
        # 첫 파일 내용이 두 번째 저장에 의해 덮어써지지 않았어야 한다.
        self.assertEqual(len(json.loads(jp1.read_text())), 1)

    def test_csv_formula_injection_is_escaped(self) -> None:
        st = _tmp_storage()
        malicious = self._sample_record(
            store_name="=cmd|'/c calc'!A1",
            company_name="+HYPERLINK(\"http://evil\")",
            email="-2+3",
            phone="@SUM(1,1)",
            address="\t안전해보이지만위험",
            business_number="\n=malicious",
        )
        _, cp = st.save_results([malicious], label="위험")
        with open(cp, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        row = rows[0]
        for field in ("store_name", "company_name", "email", "phone", "address", "business_number"):
            self.assertTrue(
                row[field].startswith("'"),
                f"{field} 값이 이스케이프되지 않음: {row[field]!r}",
            )

    def test_csv_normal_values_not_escaped(self) -> None:
        st = _tmp_storage()
        _, cp = st.save_results([self._sample_record()], label="정상")
        with open(cp, encoding="utf-8-sig") as f:
            row = next(csv.DictReader(f))
        self.assertEqual(row["business_number"], "123")
        self.assertFalse(row["store_name"].startswith("'"))

    def test_write_records_failure_leaves_no_half_written_final_files(self) -> None:
        """5차 리뷰 MEDIUM 회귀: JSON 을 최종 경로에 먼저 쓰고 CSV 를 나중에
        쓰면, CSV 작성이 실패했을 때 '완료된 것처럼 보이는' JSON 파일만 남는
        반쪽짜리 결과가 생겼다. 이제는 둘 다 임시 파일에 쓰고 성공한 뒤에만
        최종 이름으로 교체하므로, 실패 시 최종 파일이 아예 생기지 않아야 한다."""
        st = _tmp_storage()
        with patch.object(csv.DictWriter, "writerow", side_effect=RuntimeError("boom")), \
             self.assertRaises(RuntimeError):
            st.save_results([self._sample_record()], label="실패")

        self.assertEqual(list(st.output_dir.glob("*.json")), [])
        self.assertEqual(list(st.output_dir.glob("*.csv")), [])
        self.assertFalse(any(p.name.endswith(".tmp") for p in st.output_dir.iterdir()))

    def test_promote_partial_same_content_hash_overwrites_deterministically(self) -> None:
        """4차 리뷰 HIGH 회귀: 체크포인트 승격 재시도가 타임스탬프 기반
        save_results() 를 쓰면 재시도마다 새 파일이 생겨 중복된다.
        promote_partial() 은 (label, content_hash) 가 같으면 항상 같은 경로에
        덮어써야 한다(멱등)."""
        st = _tmp_storage()
        records = [self._sample_record()]
        jp1, cp1 = st.promote_partial(records, "가공식품", "abc123")
        jp2, cp2 = st.promote_partial(records, "가공식품", "abc123")
        self.assertEqual(jp1, jp2)
        self.assertEqual(cp1, cp2)
        self.assertEqual(len(list(st.output_dir.glob("*_recovered_abc123.json"))), 1)

    def test_promote_partial_different_content_hash_does_not_collide(self) -> None:
        """내용이 다른(해시가 다른) 별개의 승격 사례는 서로 다른 파일에
        남아야 한다 — 그렇지 않으면 서로 다른 크래시 회차의 데이터가 서로를
        덮어써 유실될 수 있다."""
        st = _tmp_storage()
        jp1, _ = st.promote_partial([self._sample_record()], "가공식품", "hash1")
        jp2, _ = st.promote_partial([self._sample_record(goodscode="2")], "가공식품", "hash2")
        self.assertNotEqual(jp1, jp2)
        self.assertTrue(jp1.exists())
        self.assertTrue(jp2.exists())


class PartialResultsTest(unittest.TestCase):
    def test_save_load_clear_round_trip(self) -> None:
        st = _tmp_storage()
        records = [{"goodscode": "1", "store_name": "s"}]
        path = st.save_partial_results(records, "가공식품")
        self.assertTrue(path.exists())
        self.assertEqual(st.load_partial_results("가공식품"), records)

        st.clear_partial("가공식품")
        self.assertEqual(st.load_partial_results("가공식품"), [])

    def test_load_missing_partial_returns_empty(self) -> None:
        st = _tmp_storage()
        self.assertEqual(st.load_partial_results("없는카테고리"), [])

    def test_corrupt_partial_is_quarantined(self) -> None:
        st = _tmp_storage()
        path = st._partial_path("가공식품")
        path.write_text("{broken", encoding="utf-8")
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result = st.load_partial_results("가공식품")
        self.assertEqual(result, [])

    def test_semantically_wrong_type_quarantined(self) -> None:
        st = _tmp_storage()
        path = st._partial_path("가공식품")
        path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result = st.load_partial_results("가공식품")
        self.assertEqual(result, [])

    def test_element_not_dict_is_quarantined(self) -> None:
        """4차 리뷰 MEDIUM 회귀: 최상위는 list 지만 원소가 dict 가 아니면
        (예: [1]) 예전엔 통과시켜 save_results() 가 JSON은 쓰고 CSV 작성
        중에야 AttributeError 로 실패했다 — 재시도할 때마다 쓰레기 JSON
        파일이 쌓였다. 이제는 로드 시점에 통째로 격리해야 한다."""
        st = _tmp_storage()
        path = st._partial_path("가공식품")
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result = st.load_partial_results("가공식품")
        self.assertEqual(result, [])
        self.assertEqual(len(list(st.output_dir.glob("*.corrupt_*.bak"))), 1)

    def test_element_missing_goodscode_is_quarantined(self) -> None:
        st = _tmp_storage()
        path = st._partial_path("가공식품")
        path.write_text(json.dumps([{"store_name": "이름만있음"}]), encoding="utf-8")
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result = st.load_partial_results("가공식품")
        self.assertEqual(result, [])

    def test_element_goodscode_wrong_type_is_quarantined(self) -> None:
        st = _tmp_storage()
        path = st._partial_path("가공식품")
        path.write_text(json.dumps([{"goodscode": 12345}]), encoding="utf-8")
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result = st.load_partial_results("가공식품")
        self.assertEqual(result, [])

    def test_quarantine_failure_preserves_original_and_reports_quarantine_failed(self) -> None:
        """5차 리뷰 HIGH 회귀: 격리(백업) 자체가 실패하면(rename 도, 복사도 모두
        실패) 원본을 절대 건드리지 않고 QUARANTINE_FAILED 를 반환해야 한다.
        예전에는 path.exists() 만으로 판단해 rename 실패 시 원본이 그대로
        남아있는데도 '빈 체크포인트'로 오인해 유일한 원본을 그냥 삭제했다."""
        st = _tmp_storage()
        path = st._partial_path("가공식품")
        path.write_text("{broken", encoding="utf-8")

        with patch.object(Path, "replace", side_effect=OSError("simulated replace failure")), \
             patch.object(Path, "write_bytes", side_effect=OSError("simulated copy failure")), \
             warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            status, result = st.load_partial_results_status("가공식품")

        self.assertEqual(status, LoadStatus.QUARANTINE_FAILED)
        self.assertEqual(result, [])
        self.assertTrue(any(issubclass(w.category, RuntimeWarning) for w in caught))
        # 원본이 그대로 남아 있어야 한다 — 삭제되지도, 옮겨지지도 않았다.
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_find_leftover_partials_excludes_corrupt_backup_style_names(self) -> None:
        """5차 리뷰 HIGH 방어: 옛 방식(.json 로 끝나는) 손상 백업이 디렉터리에
        남아있어도 체크포인트로 오인해 재귀적으로 또 손상 처리하거나
        '초기화'가 지워버리면 안 된다."""
        st = _tmp_storage()
        st.save_partial_results([{"goodscode": "1"}], "가공식품")
        stale_backup = st.output_dir / f".partial_{config.FILE_PREFIX}_old.corrupt_20260101_000000.json"
        stale_backup.write_text('{"broken": true}', encoding="utf-8")

        found_labels = {label for label, _ in st.find_leftover_partials()}
        self.assertIn("가공식품", found_labels)
        self.assertFalse(any("corrupt" in label for label in found_labels))

        failed = st.clear_all_partials()
        self.assertEqual(failed, [])
        self.assertTrue(stale_backup.exists())  # 백업은 초기화로도 지워지지 않는다.

    def test_find_leftover_partials_discovers_all(self) -> None:
        st = _tmp_storage()
        st.save_partial_results([{"goodscode": "1"}], "가공식품")
        st.save_partial_results([{"goodscode": "2"}], "신선식품")

        found = dict(st.find_leftover_partials())
        self.assertEqual(set(found.keys()), {"가공식품", "신선식품"})
        for path in found.values():
            self.assertTrue(path.exists())

    def test_find_leftover_partials_empty_when_none(self) -> None:
        st = _tmp_storage()
        self.assertEqual(st.find_leftover_partials(), [])

    def test_clear_all_partials_removes_every_checkpoint(self) -> None:
        st = _tmp_storage()
        st.save_partial_results([{"goodscode": "1"}], "가공식품")
        st.save_partial_results([{"goodscode": "2"}], "신선식품")
        failed = st.clear_all_partials()
        self.assertEqual(failed, [])
        self.assertEqual(st.find_leftover_partials(), [])
        self.assertEqual(st.load_partial_results("가공식품"), [])
        self.assertEqual(st.load_partial_results("신선식품"), [])

    def test_clear_all_partials_reports_failed_deletions(self) -> None:
        """3차 리뷰 MEDIUM 회귀: 삭제 실패를 조용히 삼키면 main_window.on_reset()
        이 실제로는 일부 실패했는데도 '초기화 완료'로 잘못 표시할 수 있다."""
        st = _tmp_storage()
        st.save_partial_results([{"goodscode": "1"}], "가공식품")
        with patch.object(Path, "unlink", side_effect=OSError("simulated disk error")):
            failed = st.clear_all_partials()
        self.assertEqual(len(failed), 1)
        self.assertTrue(failed[0].name.startswith(".partial_"))


class InstanceLockTest(unittest.TestCase):
    """6차 리뷰 MEDIUM 회귀: 서로 다른 프로세스가 같은 저장 경로를 동시에
    쓰면 체크포인트/manifest 가 손상될 수 있다 — 출력 디렉터리 단위로
    잠근다."""

    def test_second_storage_same_dir_same_process_does_not_conflict(self) -> None:
        """main_window._make_storage() 는 버튼 클릭마다 같은 경로로 새
        Storage 를 만든다 — 같은 프로세스 안에서는 절대 서로 충돌하면
        안 된다(인스턴스가 아니라 프로세스+경로 단위 잠금)."""
        tmp = tempfile.mkdtemp()
        st1 = Storage(tmp)
        st2 = Storage(tmp)  # 같은 프로세스, 같은 경로 — 실패하면 안 된다.
        self.assertEqual(st1.output_dir, st2.output_dir)

    @unittest.skipUnless(sys.platform != "win32", "POSIX fcntl 기반 테스트")
    def test_lock_held_by_another_process_blocks_storage_construction(self) -> None:
        """다른 프로세스가 이미 이 경로를 잠근 상황을 재현한다 —
        Storage.__init__ 의 프로세스 내 캐시(_held_locks)를 우회해, 정말
        "외부에서 이미 잠금"인 것처럼 raw fcntl 로 직접 잠근다."""
        import fcntl

        tmp = tempfile.mkdtemp()
        lock_path = Path(tmp) / storage_module._LOCK_FILENAME
        with open(lock_path, "a+") as external:
            fcntl.flock(external.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(OSError):
                storage_module._acquire_output_lock(Path(tmp))


if __name__ == "__main__":
    unittest.main()
