"""쿠팡 병렬 수집 워커 — 이벤트→시그널 브리지·스케줄 루프·증분 방출(오프라인).

워커는 self.manager 를 테스트에서 갈아끼울 수 있도록 설계됐다(모듈 docstring).
QThread.run() 을 동기 호출해 루프를 검증하고, 시그널은 직접 연결로 수집한다.
네트워크·브라우저 없음.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from app.core.base import Control
from app.core.coupang.parallel_manager import (
    STATUS_BLOCKED,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_WAITING,
    ParallelRunConfig,
)
from app.workers.coupang_parallel_worker import (
    SELLER_DISPLAY_FIELDS,
    CoupangParallelWorker,
)


def _make_config(output_dir: Path) -> ParallelRunConfig:
    return ParallelRunConfig(
        families=[[("100", "가족A"), ("101", "가족A/하위")]],
        output_dir=output_dir,
        instance_count=1,
    )


def _make_state(instance_id="1", status=STATUS_WAITING):
    return SimpleNamespace(
        config=SimpleNamespace(
            instance_id=instance_id,
            name=f"인스턴스 {instance_id}",
            line="direct",
        ),
        status=status,
        family_index=0,
        next_run_at=0.0,
        total_products=12,
        total_sellers=7,
    )


class _FakeManager:
    """all_done/run_due 시나리오를 테스트가 실시간으로 바꿔치는 가짜 매니저."""

    def __init__(self, done_after_runs=0):
        self.done_after_runs = done_after_runs
        self.run_due_calls = 0
        self.instances = {"1": _make_state()}

    def all_done(self) -> bool:
        return self.run_due_calls >= self.done_after_runs and self.done_after_runs >= 0

    def run_due(self, now: float) -> bool:
        self.run_due_calls += 1
        return True


class _Recorder:
    def __init__(self, worker: CoupangParallelWorker):
        self.logs: list[str] = []
        self.states: list[tuple] = []
        self.sellers: list[list[dict]] = []
        self.warnings: list[str] = []
        self.finished: list[dict] = []
        worker.log_message.connect(self.logs.append)
        worker.instance_state_changed.connect(
            lambda i, s, d: self.states.append((i, s, d))
        )
        worker.sellers_appended.connect(self.sellers.append)
        worker.warning_message.connect(self.warnings.append)
        worker.finished_parallel.connect(self.finished.append)


class WorkerLoopTest(unittest.TestCase):
    def test_all_done_immediately_emits_final_once(self) -> None:
        with TemporaryDirectory() as tmp:
            worker = CoupangParallelWorker(
                _make_config(Path(tmp)), Control()
            )
            worker.manager = _FakeManager(done_after_runs=0)
            rec = _Recorder(worker)
            worker.run()
            self.assertEqual(len(rec.finished), 1)
            summary = rec.finished[0]
            self.assertFalse(summary["cancelled"])
            self.assertIsNone(summary["error"])
            self.assertEqual(summary["instance_count"], 1)
            self.assertEqual(summary["instances"][0]["total_sellers"], 7)
            self.assertEqual(summary["total_products"], 12)

    def test_due_now_runs_once_then_finishes(self) -> None:
        with TemporaryDirectory() as tmp:
            worker = CoupangParallelWorker(
                _make_config(Path(tmp)), Control()
            )
            fake = _FakeManager(done_after_runs=1)
            fake.instances["1"].next_run_at = 0.0  # 즉시 due — 실 대기 없음
            worker.manager = fake
            rec = _Recorder(worker)
            worker.run()
            self.assertEqual(fake.run_due_calls, 1)
            self.assertEqual(len(rec.finished), 1)

    def test_cancel_before_run_marks_cancelled(self) -> None:
        with TemporaryDirectory() as tmp:
            control = Control()
            control.request_cancel()
            worker = CoupangParallelWorker(_make_config(Path(tmp)), control)
            worker.manager = _FakeManager(done_after_runs=5)
            rec = _Recorder(worker)
            worker.run()
            self.assertEqual(rec.finished[0]["cancelled"], True)
            self.assertEqual(rec.finished[0]["error"], None)

    def test_manager_exception_becomes_warning_and_error_summary(self) -> None:
        with TemporaryDirectory() as tmp:
            worker = CoupangParallelWorker(
                _make_config(Path(tmp)), Control()
            )

            class _Boom(_FakeManager):
                def run_due(self, now: float) -> bool:
                    raise RuntimeError("엔진 폭발")

            worker.manager = _Boom(done_after_runs=1)
            worker.manager.instances["1"].next_run_at = 0.0
            rec = _Recorder(worker)
            worker.run()
            self.assertTrue(rec.warnings)
            self.assertIn("RuntimeError", rec.finished[0]["error"])
            self.assertFalse(rec.finished[0]["cancelled"])


class EventBridgeTest(unittest.TestCase):
    def _worker(self) -> tuple[CoupangParallelWorker, _Recorder]:
        with TemporaryDirectory() as tmp:
            worker = CoupangParallelWorker(_make_config(Path(tmp)), Control())
        rec = _Recorder(worker)
        return worker, rec

    def test_log_event(self) -> None:
        worker, rec = self._worker()
        worker._on_manager_event({"type": "log", "instance": "1", "message": "안녕"})
        self.assertIn("안녕", rec.logs)

    def test_status_event_maps_state_and_logs_message(self) -> None:
        worker, rec = self._worker()
        worker._on_manager_event(
            {
                "type": "status",
                "instance": "2",
                "status": STATUS_WAITING,
                "message": "세션 완료",
                "family_index": 0,
            }
        )
        self.assertEqual(rec.states[-1][0], "2")
        self.assertEqual(rec.states[-1][1], STATUS_WAITING)
        self.assertIn("세션 완료", rec.logs)

    def test_blocked_and_error_events(self) -> None:
        worker, rec = self._worker()
        worker._on_manager_event(
            {"type": "blocked", "instance": "3", "reason": "403", "reference": "Ref#1"}
        )
        self.assertEqual(rec.states[-1][1], STATUS_BLOCKED)
        self.assertTrue(any("Ref#1" in w for w in rec.warnings))
        worker._on_manager_event({"type": "error", "instance": "3", "reason": "터널"})
        self.assertEqual(rec.states[-1][1], STATUS_ERROR)

    def test_degraded_event_is_warning(self) -> None:
        worker, rec = self._worker()
        worker._on_manager_event(
            {"type": "degraded", "message": "1개로 강등"}
        )
        self.assertIn("1개로 강등", rec.warnings[0])

    def test_exit_ip_events_log_rotation_and_failure(self) -> None:
        worker, rec = self._worker()
        worker._on_manager_event(
            {
                "type": "exit_ip",
                "instance": "2",
                "ok": True,
                "rotated": True,
                "proxy_session_id": "i2b",
                "ip": "1.2.3.4",
            }
        )
        self.assertTrue(any("교체" in log for log in rec.logs))
        worker._on_manager_event(
            {
                "type": "exit_ip",
                "instance": "2",
                "ok": False,
                "error_kind": "response",
                "tried": ["i2", "i2b"],
            }
        )
        self.assertTrue(any("회선 점검 실패" in log for log in rec.logs))

    def test_complete_event_done_and_inherited(self) -> None:
        worker, rec = self._worker()
        worker._on_manager_event(
            {
                "type": "complete",
                "instance": "1",
                "family_index": 0,
                "finals": {"sellers": "x.csv"},
                "inherited_family": None,
            }
        )
        self.assertEqual(rec.states[-1][1], STATUS_DONE)
        worker._on_manager_event(
            {
                "type": "complete",
                "instance": "1",
                "family_index": 0,
                "finals": {},
                "inherited_family": 2,
            }
        )
        self.assertEqual(rec.states[-1][1], STATUS_WAITING)
        self.assertEqual(rec.states[-1][2]["family_index"], 2)


class SellerIncrementTest(unittest.TestCase):
    def _write_sellers(self, output_dir: Path, rows: list[dict]) -> None:
        import csv

        family = output_dir / "family_01"
        family.mkdir(parents=True, exist_ok=True)
        fields = ["status", "vendor_id", "store_name", "company_name", "email"]
        with (family / "top_sellers.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def test_only_saved_rows_emitted_once(self) -> None:
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            worker = CoupangParallelWorker(_make_config(out), Control())
            rec = _Recorder(worker)
            self._write_sellers(
                out,
                [
                    {"status": "saved", "vendor_id": "V1", "email": "a@x.com"},
                    {"status": "no_public_info", "vendor_id": "V2"},
                    {"status": "saved", "vendor_id": "V3", "email": "c@x.com"},
                ],
            )
            worker._emit_new_sellers(0)
            self.assertEqual(len(rec.sellers), 1)
            rows = rec.sellers[0]
            self.assertEqual(
                [row["vendor_id"] for row in rows], ["V1", "V3"]
            )
            for row in rows:
                self.assertEqual(set(row.keys()), set(SELLER_DISPLAY_FIELDS))
            # 두 번째 호출 — 이미 방출한 판매자는 다시 나가지 않는다.
            worker._emit_new_sellers(0)
            self.assertEqual(len(rec.sellers), 1)

    def test_missing_family_file_is_silent(self) -> None:
        with TemporaryDirectory() as tmp:
            worker = CoupangParallelWorker(_make_config(Path(tmp)), Control())
            rec = _Recorder(worker)
            worker._emit_new_sellers(0)  # 파일 없음 — 예외 없이 조용히
            self.assertEqual(rec.sellers, [])


if __name__ == "__main__":
    unittest.main()
