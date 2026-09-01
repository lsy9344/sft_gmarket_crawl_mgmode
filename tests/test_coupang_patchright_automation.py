"""Patchright 예약 확대 검증 — 실제 브라우저·네트워크 없음."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from app.core.coupang.patchright_automation import (
    AUTOMATION_STATE_FILENAME,
    run_automation_stage,
)
from app.core.coupang.patchright_canary import (
    claim_live_attempt,
    record_recovery_hold,
)


class PatchrightAutomationTest(unittest.TestCase):
    def _seed_recovery_hold(self, root: Path) -> None:
        attempt = time.mktime((2026, 9, 1, 21, 53, 51, 0, 0, -1))
        hold = time.mktime((2026, 9, 1, 21, 56, 4, 0, 0, -1))
        claim_live_attempt(root, now=attempt)
        record_recovery_hold(root, now=hold)

    def test_runs_three_successful_stages_only_in_order(self):
        times = (
            time.mktime((2026, 9, 2, 0, 55, 0, 0, 0, -1)),
            time.mktime((2026, 9, 2, 3, 55, 0, 0, 0, -1)),
            time.mktime((2026, 9, 2, 6, 55, 0, 0, 0, -1)),
        )
        calls: list[str] = []

        def runner(stage: str, event: str):
            def run():
                calls.append(stage)
                return {"event": event}

            return run

        runners = {
            "recovery8": runner("recovery8", "listing_batch_completed"),
            "recovery24": runner("recovery24", "listing_batch_completed"),
            "recovery600": runner("recovery600", "listing_pages_completed"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            state_root = root / "guard"
            self._seed_recovery_hold(state_root)
            results = [
                run_automation_stage(
                    output_dir=output_dir,
                    stage=stage,
                    state_root=state_root,
                    now=stage_time,
                    runners=runners,
                )
                for stage, stage_time in zip(
                    ("recovery8", "recovery24", "recovery600"),
                    times,
                    strict=True,
                )
            ]
            state = json.loads(
                (output_dir / AUTOMATION_STATE_FILENAME).read_text("utf-8")
            )
            guard = json.loads(
                (state_root / "canary_guard.json").read_text("utf-8")
            )
        self.assertEqual(
            [result["event"] for result in results],
            ["automation_stage_completed"] * 3,
        )
        self.assertEqual(
            calls, ["recovery8", "recovery24", "recovery600"]
        )
        self.assertEqual(state["completed_stages"], calls)
        self.assertFalse(state["halted"])
        self.assertNotIn("recovery_ramp_limit", guard)
        self.assertTrue(guard["recovery_ramp_completed"])

    def test_failure_halts_later_stage_without_calling_runner(self):
        first_time = time.mktime((2026, 9, 2, 0, 55, 0, 0, 0, -1))
        second_time = time.mktime((2026, 9, 2, 3, 55, 0, 0, 0, -1))
        second_calls: list[str] = []
        runners = {
            "recovery8": lambda: {
                "event": "blocked",
                "reference": "Reference #18.test",
            },
            "recovery24": lambda: second_calls.append("called") or {
                "event": "listing_batch_completed"
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            state_root = root / "guard"
            self._seed_recovery_hold(state_root)
            first = run_automation_stage(
                output_dir=output_dir,
                stage="recovery8",
                state_root=state_root,
                now=first_time,
                runners=runners,
            )
            second = run_automation_stage(
                output_dir=output_dir,
                stage="recovery24",
                state_root=state_root,
                now=second_time,
                runners=runners,
            )
        self.assertEqual(first["event"], "automation_halted")
        self.assertEqual(second["event"], "automation_skipped")
        self.assertEqual(second_calls, [])


if __name__ == "__main__":
    unittest.main()
