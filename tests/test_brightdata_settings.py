"""Bright Data 계정 설정 모듈(brightdata) 테스트 — 네트워크 없음.

검증 항목:
- 설정 저장/로드 라운드트립, 손상·부재 파일 폴백, sanitize(공백/포트)
- 토큰 해석 우선순위: 설정 파일(UI 입력) → 환경변수 → 기존 토큰 파일
- Unlocker 존/국가 기본값 폴백, ISP 프록시 dict 구성·완비 검사
- 계정 API(잔액/존 비밀번호) 파싱 — fake session 으로 응답 형식별 검증
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from app.core import brightdata, config


class _FakeAccountResponse:
    def __init__(self, status: int = 200, text: str = "") -> None:
        self.status_code = status
        self.text = text


class _FakeAccountSession:
    """get(url, headers, timeout) 만 제공하는 fake requests.Session."""

    def __init__(self, response: _FakeAccountResponse) -> None:
        self.response = response
        self.calls: list[dict] = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        return self.response

    def close(self) -> None:
        pass


class SettingsRoundTripTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "brightdata_settings.json")

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_returns_defaults(self):
        s = brightdata.load_settings(self.path)
        self.assertEqual(s.api_token, "")
        self.assertFalse(s.isp_enabled)
        self.assertEqual(s.saved_at, "")

    def test_corrupt_file_returns_defaults(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        s = brightdata.load_settings(self.path)
        self.assertEqual(s.api_token, "")
        self.assertEqual(s.isp_zone, "")

    def test_non_dict_json_returns_defaults(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('["array"]')
        self.assertEqual(brightdata.load_settings(self.path).api_token, "")

    def test_save_load_roundtrip(self):
        s = brightdata.BrightDataSettings(
            api_token="tok123",
            unlocker_zone="my_zone",
            country="KR",
            isp_enabled=True,
            isp_customer_id="hl_test",
            isp_zone="my_isp",
            isp_password="pw",
        )
        brightdata.save_settings(s, self.path)
        loaded = brightdata.load_settings(self.path)
        self.assertEqual(loaded.api_token, "tok123")
        self.assertEqual(loaded.unlocker_zone, "my_zone")
        self.assertEqual(loaded.country, "KR")
        self.assertTrue(loaded.isp_enabled)
        self.assertEqual(loaded.isp_customer_id, "hl_test")
        self.assertEqual(loaded.isp_zone, "my_isp")
        self.assertEqual(loaded.isp_password, "pw")
        self.assertTrue(loaded.saved_at)  # 저장 시각 기록

    def test_save_sanitizes_whitespace_and_port(self):
        s = brightdata.BrightDataSettings(
            api_token="  tok  ", unlocker_zone=" zone ", isp_port="abc",
        )
        brightdata.save_settings(s, self.path)
        loaded = brightdata.load_settings(self.path)
        self.assertEqual(loaded.api_token, "tok")
        self.assertEqual(loaded.unlocker_zone, "zone")
        self.assertEqual(loaded.isp_port, brightdata.DEFAULT_ISP_PORT)

    def test_isp_port_out_of_range_falls_back(self):
        s = brightdata.BrightDataSettings(isp_port=99999).sanitized()
        self.assertEqual(s.isp_port, brightdata.DEFAULT_ISP_PORT)
        s2 = brightdata.BrightDataSettings(isp_port="3128").sanitized()
        self.assertEqual(s2.isp_port, 3128)


class ResolveTokenTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "brightdata_settings.json")
        # 외부 상태 격리 — 환경변수와 기존 토큰 파일을 모두 무력화
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("BRIGHTDATA_API_TOKEN", None)
        self._legacy = mock.patch.object(config, "brightdata_api_token", lambda: "")
        self._legacy.start()

    def tearDown(self):
        self._legacy.stop()
        self._env.stop()
        self._tmp.cleanup()

    def test_settings_token_wins_over_env_and_legacy(self):
        brightdata.save_settings(
            brightdata.BrightDataSettings(api_token="from_ui"), self.path)
        os.environ["BRIGHTDATA_API_TOKEN"] = "from_env"
        with mock.patch.object(config, "brightdata_api_token", lambda: "from_file"):
            s = brightdata.load_settings(self.path)
            self.assertEqual(brightdata.resolve_api_token(s), "from_ui")

    def test_env_falls_back_before_legacy_file(self):
        os.environ["BRIGHTDATA_API_TOKEN"] = "from_env"
        with mock.patch.object(config, "brightdata_api_token", lambda: "from_file"):
            self.assertEqual(
                brightdata.resolve_api_token(brightdata.BrightDataSettings()),
                "from_env")

    def test_legacy_file_is_last_resort(self):
        with mock.patch.object(config, "brightdata_api_token", lambda: "from_file"):
            self.assertEqual(
                brightdata.resolve_api_token(brightdata.BrightDataSettings()),
                "from_file")

    def test_empty_everywhere_returns_empty(self):
        self.assertEqual(
            brightdata.resolve_api_token(brightdata.BrightDataSettings()), "")

    def test_resolve_unlocker_defaults_to_config(self):
        token, zone, country = brightdata.resolve_unlocker(
            brightdata.BrightDataSettings(api_token="t"))
        self.assertEqual((token, zone, country),
                         ("t", config.BRIGHTDATA_ZONE, config.BRIGHTDATA_COUNTRY))

    def test_resolve_unlocker_uses_settings_zone(self):
        s = brightdata.BrightDataSettings(
            api_token="t", unlocker_zone="custom", country="us")
        _, zone, country = brightdata.resolve_unlocker(s)
        self.assertEqual((zone, country), ("custom", "us"))


class IspProxyDictTest(unittest.TestCase):
    def test_disabled_returns_none(self):
        s = brightdata.BrightDataSettings(
            isp_enabled=False, isp_customer_id="c", isp_zone="z", isp_password="p")
        self.assertIsNone(brightdata.isp_proxy_dict(s))

    def test_incomplete_credentials_return_none(self):
        base = {"isp_enabled": True}
        self.assertIsNone(brightdata.isp_proxy_dict(
            brightdata.BrightDataSettings(**base, isp_customer_id="", isp_zone="z",
                                          isp_password="p")))
        self.assertIsNone(brightdata.isp_proxy_dict(
            brightdata.BrightDataSettings(**base, isp_customer_id="c", isp_zone="",
                                          isp_password="p")))
        self.assertIsNone(brightdata.isp_proxy_dict(
            brightdata.BrightDataSettings(**base, isp_customer_id="c", isp_zone="z",
                                          isp_password="")))

    def test_complete_credentials_build_playwright_dict(self):
        s = brightdata.BrightDataSettings(
            isp_enabled=True, isp_customer_id="hl_x", isp_zone="gm_isp",
            isp_password="secret", isp_port=22225)
        proxy = brightdata.isp_proxy_dict(s)
        self.assertEqual(proxy["server"], "http://brd.superproxy.io:22225")
        self.assertEqual(proxy["username"], "brd-customer-hl_x-zone-gm_isp")
        self.assertEqual(proxy["password"], "secret")

    def test_summary_hides_password(self):
        proxy = brightdata.isp_proxy_dict(brightdata.BrightDataSettings(
            isp_enabled=True, isp_customer_id="hl_x", isp_zone="z", isp_password="p"))
        self.assertNotIn("p", brightdata.isp_proxy_summary(proxy))
        self.assertIn("hl_x", brightdata.isp_proxy_summary(proxy))


class MaskedTokenTest(unittest.TestCase):
    def test_masks_all_but_last_four(self):
        self.assertEqual(brightdata.masked_token("abcdefghijklmnop"), "…mnop")

    def test_short_or_empty(self):
        self.assertEqual(brightdata.masked_token("abc"), "****")
        self.assertEqual(brightdata.masked_token(""), "****")


class FetchBalanceTest(unittest.TestCase):
    def test_parses_balance_response(self):
        sess = _FakeAccountSession(_FakeAccountResponse(
            200, json.dumps({"balance": 5.84, "pending_balance": 0.27})))
        data = brightdata.fetch_balance("tok", session=sess)
        self.assertEqual(data["balance"], 5.84)
        self.assertEqual(data["pending_balance"], 0.27)
        self.assertEqual(sess.calls[0]["url"],
                         brightdata.BRIGHTDATA_BALANCE_URL)
        self.assertEqual(sess.calls[0]["headers"]["Authorization"], "Bearer tok")

    def test_rejected_token_raises_with_status(self):
        sess = _FakeAccountSession(_FakeAccountResponse(401, "unauthorized"))
        with self.assertRaises(brightdata.BrightDataAPIError) as ctx:
            brightdata.fetch_balance("bad", session=sess)
        self.assertEqual(ctx.exception.status, 401)

    def test_server_error_raises(self):
        sess = _FakeAccountSession(_FakeAccountResponse(500, "boom"))
        with self.assertRaises(brightdata.BrightDataAPIError):
            brightdata.fetch_balance("tok", session=sess)

    def test_non_json_body_raises(self):
        sess = _FakeAccountSession(_FakeAccountResponse(200, "<html>ok</html>"))
        with self.assertRaises(brightdata.BrightDataAPIError):
            brightdata.fetch_balance("tok", session=sess)

    def test_empty_token_raises_without_network(self):
        with self.assertRaises(brightdata.BrightDataAPIError):
            brightdata.fetch_balance("  ")


class FetchZonePasswordTest(unittest.TestCase):
    def test_json_list_response(self):
        sess = _FakeAccountSession(_FakeAccountResponse(200, '["pw1"]'))
        pw = brightdata.fetch_zone_password("tok", "gm_isp", session=sess)
        self.assertEqual(pw, "pw1")
        self.assertIn("zone=gm_isp", sess.calls[0]["url"])

    def test_json_dict_response(self):
        sess = _FakeAccountSession(_FakeAccountResponse(200, '{"password":"pw2"}'))
        self.assertEqual(brightdata.fetch_zone_password("tok", "z", session=sess), "pw2")

    def test_plain_text_response(self):
        sess = _FakeAccountSession(_FakeAccountResponse(200, "pw3\n"))
        self.assertEqual(brightdata.fetch_zone_password("tok", "z", session=sess), "pw3")

    def test_empty_body_raises(self):
        sess = _FakeAccountSession(_FakeAccountResponse(200, ""))
        with self.assertRaises(brightdata.BrightDataAPIError):
            brightdata.fetch_zone_password("tok", "z", session=sess)

    def test_empty_zone_raises_without_network(self):
        with self.assertRaises(brightdata.BrightDataAPIError):
            brightdata.fetch_zone_password("tok", "  ")


class DefaultPathTest(unittest.TestCase):
    def test_path_is_under_output_dir(self):
        path = brightdata.default_settings_path()
        self.assertIn("output", path)
        self.assertTrue(path.endswith(brightdata.SETTINGS_FILENAME))
        self.assertEqual(brightdata.SETTINGS_FILENAME, "brightdata_settings.json")


if __name__ == "__main__":
    unittest.main()
