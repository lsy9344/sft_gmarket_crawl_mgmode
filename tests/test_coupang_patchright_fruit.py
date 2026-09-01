"""Patchright 과일 순차 작업 상태 시험 — 실제 네트워크 없음."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.core.coupang.patchright_fruit import JOB_FILENAME, run_fruit_step


class _SampleRunner:
    def __init__(self, results: list[dict]) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        result = dict(self.results.pop(0))
        result.setdefault("category_id", kwargs["category_id"])
        result.setdefault("page_number", kwargs["page_number"])
        result.setdefault("limit", kwargs["limit"])
        result.setdefault("offset", 0)
        return result


class PatchrightFruitTest(unittest.TestCase):
    def test_first_success_increases_next_batch_from_eight_to_twelve(self):
        runner = _SampleRunner(
            [
                {
                    "event": "sample_completed",
                    "offset": 0,
                    "next_offset": 8,
                    "record_count": 2,
                }
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            result = run_fruit_step(output_dir=output_dir, sample_runner=runner)
            job = json.loads((output_dir / JOB_FILENAME).read_text("utf-8"))

        self.assertEqual(result["event"], "fruit_step_completed")
        self.assertEqual(runner.calls[0]["category_id"], "194284")
        self.assertEqual(runner.calls[0]["limit"], 8)
        self.assertEqual(result["next_batch_limit"], 12)
        self.assertEqual(job["products_processed"], 8)
        self.assertEqual(job["records_saved"], 2)

    def test_two_empty_pages_finish_current_category(self):
        runner = _SampleRunner(
            [
                {"event": "page_exhausted", "offset": 0},
                {"event": "page_exhausted", "offset": 0},
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            first = run_fruit_step(output_dir=output_dir, sample_runner=runner)
            second = run_fruit_step(output_dir=output_dir, sample_runner=runner)
            job = json.loads((output_dir / JOB_FILENAME).read_text("utf-8"))

        self.assertEqual(first["next_page_number"], 2)
        self.assertEqual(second["completed_category_count"], 1)
        self.assertEqual(job["completed_categories"], ["194284"])
        self.assertEqual(job["category_index"], 1)
        self.assertEqual(job["page_number"], 1)

    def test_block_does_not_create_or_advance_job(self):
        runner = _SampleRunner([{"event": "blocked", "blocked_at": "category"}])
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            result = run_fruit_step(output_dir=output_dir, sample_runner=runner)
            job_exists = (output_dir / JOB_FILENAME).exists()

        self.assertEqual(result["event"], "blocked")
        self.assertFalse(job_exists)


if __name__ == "__main__":
    unittest.main()
