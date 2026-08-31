"""쿠팡 차단 쿨다운 게이트(blockguard) 단위 테스트 — 네트워크·브라우저 없음."""

import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app.core.base import Control
from app.core.coupang import blockguard
from app.core.coupang.search_crawler import SearchCrawler, SearchRunConfig


def _write_state(
    tmp: Path, blocked_ts: float, reference: str = "Reference #18.x"
) -> None:
    import json

    (tmp / blockguard.BLOCK_STATE_FILENAME).write_text(
        json.dumps(
            {
                "blocked_at": "2026-08-30 17:33:07",
                "blocked_ts": blocked_ts,
                "reason": "test",
                "reference": reference,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class RecordAndCooldownTest(unittest.TestCase):
    def test_record_block_writes_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now()  # noqa: DTZ005 - 테스트 내 naive 시각 고정 목적
            path = blockguard.record_block(
                Path(tmp),
                reason="테스트 차단",
                reference="Reference #18.9ca52b17.1788078779.26800a86",
                now=now,
            )
            self.assertIsNotNone(path)
            state = blockguard.read_block_state(Path(tmp))
            self.assertEqual(state["blocked_ts"], now.timestamp())
            self.assertIn("Reference #", state["reference"])

    def test_cooldown_remaining_within_and_after(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now()  # noqa: DTZ005 - 테스트 내 naive 시각 고정 목적
            blockguard.record_block(Path(tmp), now=now)
            within = blockguard.cooldown_remaining_seconds(
                Path(tmp), cooldown_hours=12.0, now=now + timedelta(hours=5)
            )
            self.assertAlmostEqual(within, 7 * 3600, delta=5)
            expired = blockguard.cooldown_remaining_seconds(
                Path(tmp), cooldown_hours=12.0, now=now + timedelta(hours=13)
            )
            self.assertEqual(expired, 0.0)

    def test_missing_corrupt_and_future_state_yield_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self.assertEqual(blockguard.cooldown_remaining_seconds(tmp), 0.0)
            (tmp / blockguard.BLOCK_STATE_FILENAME).write_text(
                "not json", encoding="utf-8"
            )
            self.assertEqual(blockguard.cooldown_remaining_seconds(tmp), 0.0)
            # blocked_ts 가 미래 → 시계 오차로 간주하고 무시
            _write_state(tmp, time.time() + 600)
            self.assertEqual(blockguard.cooldown_remaining_seconds(tmp), 0.0)

    def test_format_remaining(self):
        self.assertEqual(blockguard.format_remaining(2 * 3600 + 15 * 60), "2시간 15분")
        self.assertEqual(blockguard.format_remaining(5 * 60), "5분")
        self.assertEqual(blockguard.format_remaining(0), "0분")

    def test_record_failure_is_best_effort(self):
        # 파일 대신 디렉터리를 만들어 write_text 가 실패하도록 유도
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / blockguard.BLOCK_STATE_FILENAME
            state_file.mkdir()
            self.assertIsNone(blockguard.record_block(Path(tmp)))


class CooldownGateTest(unittest.TestCase):
    """엔진 시작 단계 쿨다운 게이트 — 브라우저 생성 없이 거부되는지 검증."""

    class _BoomBrowser:
        def new_page(self):  # pragma: no cover - 호출되면 테스트 실패 신호
            raise AssertionError("쿨다운 중에는 브라우저가 생성되면 안 됩니다")

    def test_gate_refuses_run_within_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now()  # noqa: DTZ005 - 테스트 내 naive 시각 고정 목적
            blockguard.record_block(Path(tmp), now=now, reason="이전 차단")
            config = SearchRunConfig(
                output_dir=Path(tmp),
                output_prefix="gate_test",
                keyword="뷰티",
                warmup_time=0,
                page_delay_min=0,
                page_delay_max=0,
                delay_min=0,
                delay_max=0,
            )
            crawler = SearchCrawler(
                config=config,
                control=Control(),
                browser_factory=lambda: self._BoomBrowser(),
            )
            logs: list[str] = []
            crawler._on_log = logs.append
            summary = crawler.run()
            self.assertEqual(summary.termination_reason, "block_cooldown")
            self.assertIn("쿨다운", summary.error)
            self.assertTrue(logs)

    def test_gate_allows_run_after_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 쿨다운 만료된 기록 — 성공/실패와 무관하게 실행은 시도되어야 한다
            _write_state(Path(tmp), time.time() - 13 * 3600)
            config = SearchRunConfig(
                output_dir=Path(tmp),
                output_prefix="gate_test",
                keyword="뷰티",
                warmup_time=0,
                page_delay_min=0,
                page_delay_max=0,
                delay_min=0,
                delay_max=0,
            )

            class _StubBrowser:
                def new_page(self):
                    raise RuntimeError("브라우저 진입까지 도달했다 (게이트 통과)")

            crawler = SearchCrawler(
                config=config,
                control=Control(),
                browser_factory=lambda: _StubBrowser(),
            )
            summary = crawler.run()
            # 게이트는 통과 — 브라우저 stub 예외가 엔진 오류로 기록된다
            self.assertNotEqual(summary.termination_reason, "block_cooldown")


if __name__ == "__main__":
    unittest.main()
