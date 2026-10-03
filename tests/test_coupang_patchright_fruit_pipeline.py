import tempfile
import time
import unittest
from pathlib import Path

from app.core.coupang.patchright_full_fruit import (
    PRODUCT_FIELDS,
    FullFruitStore,
    _write_csv,
    _write_json,
)
from scripts.prototypes.coupang_patchright_fruit_pipeline import (
    choose_action,
    choose_scheduled_action,
)


class PatchrightFruitPipelineTests(unittest.TestCase):
    def test_scheduled_runner_uses_one_shared_guard(self):
        runner = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "prototypes"
            / "run_patchright_fruit_pipeline.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn('"SellerCollectorPatchrightCanary"', runner)
        self.assertNotIn('"SellerCollectorPatchrightSeller"', runner)
        self.assertIn("--state-root $stateRoot", runner)
        self.assertIn('"PatchrightFruitPipeline-61Min-Continuation"', runner)
        self.assertIn("$trialRuns.Count -eq 5", runner)
        self.assertIn("$successfulTrialRuns.Count -eq 5", runner)
        self.assertIn("Enable-ScheduledTask", runner)

    def test_registration_is_limited_to_five_sixty_one_minute_runs(self):
        registration = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "prototypes"
            / "register_patchright_fruit_pipeline.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn('$taskName = "PatchrightFruitPipeline-61Min-5Runs"', registration)
        self.assertIn("$runCount = 5", registration)
        self.assertIn("$firstRun.AddMinutes(61 * $index)", registration)
        self.assertIn("New-ScheduledTaskTrigger -Once", registration)
        self.assertNotIn("New-ScheduledTaskTrigger `\n        -Daily", registration)

    def test_unmapped_product_runs_sellers_first(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            store = FullFruitStore(output_dir)
            store.ensure_files()
            _write_csv(
                store.products_path,
                PRODUCT_FIELDS,
                [{field: "VI1" if field == "vendor_item_id" else "" for field in PRODUCT_FIELDS}],
            )

            action, _reason = choose_action(output_dir)

            self.assertEqual(action, "sellers")

    def test_no_seller_work_moves_to_category(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            FullFruitStore(output_dir).ensure_files()

            action, _reason = choose_action(output_dir)

            self.assertEqual(action, "category")

    def test_finished_categories_and_sellers_complete_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            FullFruitStore(output_dir).ensure_files()
            state = {
                "version": 1,
                "phase": "sellers",
                "status": "running",
                "category_index": 12,
                "page_number": 1,
                "next_offset": 0,
                "consecutive_empty_pages": 0,
                "raw_products_seen": 0,
                "unique_products": 0,
                "category_statuses": {
                    category_id: "completed"
                    for category_id in (
                        "194284", "194288", "194294", "194300", "194306", "194315",
                        "194320", "194326", "194331", "194337", "194358", "194368",
                    )
                },
                "completed_categories": [
                    "194284", "194288", "194294", "194300", "194306", "194315",
                    "194320", "194326", "194331", "194337", "194358", "194368",
                ],
            }
            _write_json(output_dir / "fruit_collection_state.json", state)

            action, _reason = choose_action(output_dir)

            self.assertEqual(action, "complete")

    def test_failed_mapping_stops_before_next_category(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            store = FullFruitStore(output_dir)
            store.ensure_files()
            _write_json(store.failed_mappings_path, [{"vendor_item_id": "VI1"}])

            action, _reason = choose_action(output_dir)

            self.assertEqual(action, "halted")

    def test_scheduled_action_reopens_http_503_after_three_hours(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            store = FullFruitStore(output_dir)
            store.ensure_files()
            _write_csv(
                store.products_path,
                PRODUCT_FIELDS,
                [{field: "VI1" if field == "vendor_item_id" else "" for field in PRODUCT_FIELDS}],
            )
            _write_json(
                output_dir / "fruit_seller_control.json",
                {
                    "version": 1,
                    "status": "halted",
                    "updated_at": "2026-09-08 05:44:48",
                    "reason": "상품-판매자 연결 HTTP 503",
                },
            )

            action, _reason = choose_scheduled_action(
                output_dir,
                now=time.mktime(
                    time.strptime("2026-09-08 08:44:48", "%Y-%m-%d %H:%M:%S")
                ),
            )

            self.assertEqual(action, "sellers")

    def test_eighty_minute_registration_preserves_twenty_two_future_runs(self):
        registration = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "prototypes"
            / "register_patchright_80min_continuation.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn('$taskName = "PatchrightFruitPipeline-80Min"', registration)
        self.assertIn('$firstRun = [datetime]"2026-09-08T08:47:00"', registration)
        self.assertIn("$runCount = 22", registration)
        self.assertIn("$firstRun.AddMinutes(80 * $index)", registration)
        self.assertIn('"PatchrightFruitPipeline-61Min-Continuation"', registration)

    def test_eighty_minute_extension_covers_estimated_completion(self):
        extension = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "prototypes"
            / "extend_patchright_80min_completion.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn('$taskName = "PatchrightFruitPipeline-80Min"', extension)
        self.assertIn('$finalRun = [datetime]"2026-09-10T12:47:00"', extension)
        self.assertIn("$nextRun = $lastRun.AddMinutes(80)", extension)
        self.assertIn("Set-ScheduledTask", extension)
        self.assertIn("$added.Count -ne 18", extension)


if __name__ == "__main__":
    unittest.main()
