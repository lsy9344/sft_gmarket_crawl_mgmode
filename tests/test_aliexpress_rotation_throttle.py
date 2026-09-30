"""Ali 회선 교체 스로틀(2026-09-29) — 예산 폐지 후 남은 유일한 회선 규율.

배경: 회선 소모 예산(교체 횟수 상한·대기)은 2026-09-29 사용자 결정으로 폐지했다.
회선이 많이 나와도 수집은 계속 진행한다. 대신 새 세션을 여는 행위가 연쇄되면
차단이 악화되므로(쿠팡 2026-09-28·29 실측과 같은 급속 순환 패턴), 교체 사이
최소 간격만 둔다. 이 파일은 그 스로틀만 검증한다.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

try:
    from tests.test_aliexpress_block_resume import (
        CrawlHarness,
        ScriptedPage,
        make_cfg,
        make_mtop,
        make_products,
    )
except ImportError:  # unittest discover -s tests 실행 시
    from test_aliexpress_block_resume import (  # type: ignore[no-redef]
        CrawlHarness,
        ScriptedPage,
        make_cfg,
        make_mtop,
        make_products,
    )


class RotationThrottleTest(unittest.TestCase):
    def test_rapid_session_swap_waits_min_interval(self):
        """직전 세션 오픈 후 최소 간격 내 교체는 대기 후 이루어진다."""
        with tempfile.TemporaryDirectory() as tmp:
            page = ScriptedPage(
                listing_map={1: make_products("th", 1)},
                pdp_payloads={"th1": make_mtop("S1", "회사1")},
                pdp_plan={"th1": ["nodata", "ok"]},  # 1차 미수신 → 즉시 회선 교체
            )
            logs: list[str] = []
            h = CrawlHarness(page, use_proxy=True)
            cfg = make_cfg(Path(tmp), rotation_min_interval_seconds=0.3)
            started = time.monotonic()
            _, summary = h.crawl(cfg, on_log=logs.append)
            elapsed = time.monotonic() - started

            self.assertEqual(summary.termination_reason, "success", summary.error)
            self.assertEqual(summary.collected_items, 1)
            joined = "\n".join(logs)
            self.assertIn("회선 순환 스로틀", joined)
            self.assertGreaterEqual(elapsed, 0.3, "스로틀 대기만큼 시간이 흘러야 한다")

    def test_first_session_open_is_not_delayed(self):
        """첫 세션 오픈에는 간격을 적용하지 않는다 — 즉시 시작."""
        with tempfile.TemporaryDirectory() as tmp:
            page = ScriptedPage(
                listing_map={1: make_products("fst", 1)},
                pdp_payloads={"fst1": make_mtop("S1", "회사1")},
            )
            logs: list[str] = []
            h = CrawlHarness(page, use_proxy=True)
            cfg = make_cfg(Path(tmp), rotation_min_interval_seconds=30)
            started = time.monotonic()
            _, summary = h.crawl(cfg, on_log=logs.append)
            elapsed = time.monotonic() - started

            self.assertEqual(summary.termination_reason, "success", summary.error)
            self.assertEqual(summary.collected_items, 1)
            self.assertLess(elapsed, 10, "교체 없는 수집은 스로틀에 걸리지 않는다")
            self.assertNotIn("회선 순환 스로틀", "\n".join(logs))


if __name__ == "__main__":
    unittest.main()
