"""app.utils.helpers 순수 함수 회귀 테스트."""

from __future__ import annotations

import unittest

from app.utils.helpers import (
    contains_bot_challenge,
    estimate_seconds,
    extract_goodscodes,
    format_clock,
    format_duration,
    sanitize_filename,
)


class SanitizeFilenameTest(unittest.TestCase):
    def test_replaces_windows_forbidden_chars(self) -> None:
        self.assertEqual(sanitize_filename("생필품/육아"), "생필품_육아")
        self.assertEqual(sanitize_filename('a:b*c?d"e<f>g|h\\i'), "a_b_c_d_e_f_g_h_i")

    def test_leaves_safe_name_untouched(self) -> None:
        self.assertEqual(sanitize_filename("가공식품"), "가공식품")


class ExtractGoodscodesTest(unittest.TestCase):
    def test_dedups_preserving_order(self) -> None:
        html = 'a goodscode=111 b goodscode=222 goodscode=111 c'
        self.assertEqual(extract_goodscodes(html), ["111", "222"])

    def test_empty_html(self) -> None:
        self.assertEqual(extract_goodscodes(""), [])
        self.assertEqual(extract_goodscodes(None), [])  # type: ignore[arg-type]


class BotChallengeTest(unittest.TestCase):
    def test_detects_known_keyword(self) -> None:
        self.assertTrue(contains_bot_challenge("잠시만 기다리십시오"))

    def test_normal_html_not_flagged(self) -> None:
        self.assertFalse(contains_bot_challenge("<html>goodscode=123</html>"))


class TimeFormattingTest(unittest.TestCase):
    def test_format_duration(self) -> None:
        self.assertEqual(format_duration(843), "14분 3초")
        self.assertEqual(format_duration(3661), "1시간 1분 1초")
        self.assertEqual(format_duration(0), "0초")

    def test_format_clock(self) -> None:
        self.assertEqual(format_clock(754), "12:34")
        self.assertEqual(format_clock(3661), "1:01:01")

    def test_estimate_seconds(self) -> None:
        self.assertAlmostEqual(estimate_seconds(1205), 1205 * 0.7)
        self.assertEqual(estimate_seconds(-5), 0.0)


if __name__ == "__main__":
    unittest.main()
