"""Patchright 다음 날 대량 수집 일정 — 실제 브라우저·네트워크 없음."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from itertools import pairwise
from pathlib import Path

from app.core.coupang.patchright_daily_schedule import (
    DAILY_STATE_FILENAME,
    _remaining_page_count,
    build_schedule,
    run_daily_stage,
)


class PatchrightDailyScheduleTest(unittest.TestCase):
    def test_plan_uses_full_daily_envelope_with_two_hour_interval(self):
        plan = build_schedule()
        live = [item for item in plan if item["mode"] == "live"]

        self.assertEqual(len(live), 3)
        self.assertEqual(
            [item["scheduled_at"] for item in live],
            [
                "2026-09-03 00:10:00",
                "2026-09-03 02:15:00",
                "2026-09-03 04:20:00",
            ],
        )
        self.assertEqual([item["page_limit"] for item in live], [10, 10, 5])
        self.assertEqual(sum(item["item_budget"] for item in live), 1_500)
        self.assertTrue(
            all(
                later["timestamp"] - earlier["timestamp"] >= 2 * 60 * 60
                for earlier, later in pairwise(live)
            )
        )
        self.assertLessEqual(max(item["item_budget"] for item in live), 600)

    def test_last_stage_uses_remaining_daily_budget(self):
        self.assertEqual(
            _remaining_page_count(
                {"daily_date": "2026-09-03", "daily_items_reserved": 1_200}
            ),
            10,
        )
        self.assertEqual(
            _remaining_page_count(
                {"daily_date": "2026-09-03", "daily_items_reserved": 800}
            ),
            10,
        )

    def test_three_stages_run_only_in_order(self):
        calls: list[str] = []

        def runner(stage: str):
            def run():
                calls.append(stage)
                return {"event": "listing_pages_completed"}

            return run

        runners = {stage: runner(stage) for stage in (
            "pages10_a",
            "pages10_b",
            "daily_remainder",
        )}
        times = (
            time.mktime((2026, 9, 3, 0, 10, 0, 0, 0, -1)),
            time.mktime((2026, 9, 3, 2, 15, 0, 0, 0, -1)),
            time.mktime((2026, 9, 3, 4, 20, 0, 0, 0, -1)),
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            results = [
                run_daily_stage(
                    output_dir=output_dir,
                    stage=stage,
                    state_root=output_dir / "guard",
                    now=stage_time,
                    runners=runners,
                )
                for stage, stage_time in zip(
                    ("pages10_a", "pages10_b", "daily_remainder"),
                    times,
                    strict=True,
                )
            ]
            state = json.loads(
                (output_dir / DAILY_STATE_FILENAME).read_text("utf-8")
            )
        self.assertEqual(
            [result["event"] for result in results],
            ["daily_stage_completed"] * 3,
        )
        self.assertEqual(
            calls, ["pages10_a", "pages10_b", "daily_remainder"]
        )
        self.assertFalse(state["halted"])

    def test_failure_prevents_later_runner(self):
        calls: list[str] = []
        first_time = time.mktime((2026, 9, 3, 0, 10, 0, 0, 0, -1))
        second_time = time.mktime((2026, 9, 3, 2, 15, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            first = run_daily_stage(
                output_dir=output_dir,
                stage="pages10_a",
                state_root=output_dir / "guard",
                now=first_time,
                runners={"pages10_a": lambda: {"event": "blocked"}},
            )
            second = run_daily_stage(
                output_dir=output_dir,
                stage="pages10_b",
                state_root=output_dir / "guard",
                now=second_time,
                runners={
                    "pages10_b": lambda: calls.append("called")
                    or {"event": "listing_pages_completed"}
                },
            )
        self.assertEqual(first["event"], "daily_stage_halted")
        self.assertEqual(second["event"], "daily_stage_skipped")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
