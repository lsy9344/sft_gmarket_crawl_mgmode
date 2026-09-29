"""쿠팡 수집 직전 환경 사전점검(env_probe) — 네트워크 없이 curl_cffi 모의."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.core.coupang.env_probe import CHROME_IMPERSONATE, probe_environment

_PROXY = {"server": "http://proxy.example:7000", "username": "u", "password": "p"}


class _Response:
    def __init__(self, status_code: int):
        self.status_code = status_code


def _get_returning(direct: int, proxy: int):
    def fake_get(url, **kwargs):
        return _Response(proxy if kwargs.get("proxies") else direct)

    return fake_get


class ProbeEnvironmentTest(unittest.TestCase):
    def test_pass_reports_both_statuses(self):
        with patch(
            "app.core.coupang.env_probe.curl_requests.get",
            side_effect=_get_returning(200, 200),
        ) as get:
            result = probe_environment(_PROXY)
        self.assertEqual(result.direct_status, 200)
        self.assertEqual(result.proxy_status, 200)
        self.assertFalse(result.direct_blocked)
        self.assertFalse(result.proxy_blocked)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(
            {c.kwargs.get("impersonate") for c in get.call_args_list},
            {CHROME_IMPERSONATE},
        )

    def test_proxy_403_is_proxy_blocked(self):
        with patch(
            "app.core.coupang.env_probe.curl_requests.get",
            side_effect=_get_returning(200, 403),
        ):
            result = probe_environment(_PROXY)
        self.assertFalse(result.direct_blocked)
        self.assertTrue(result.proxy_blocked)

    def test_direct_403_is_direct_blocked(self):
        with patch(
            "app.core.coupang.env_probe.curl_requests.get",
            side_effect=_get_returning(403, 200),
        ):
            result = probe_environment(_PROXY)
        self.assertTrue(result.direct_blocked)
        self.assertFalse(result.proxy_blocked)

    def test_request_failure_is_unknown_not_blocked(self):
        def boom(url, **kwargs):
            raise TimeoutError("read timed out")

        with patch(
            "app.core.coupang.env_probe.curl_requests.get", side_effect=boom
        ):
            result = probe_environment(_PROXY)
        self.assertIsNone(result.direct_status)
        self.assertIsNone(result.proxy_status)
        self.assertFalse(result.direct_blocked)
        self.assertFalse(result.proxy_blocked)
        self.assertIn("TimeoutError", result.direct_error)
        self.assertIn("TimeoutError", result.proxy_error)

    def test_without_proxy_only_direct_is_probed(self):
        with patch(
            "app.core.coupang.env_probe.curl_requests.get",
            side_effect=_get_returning(200, 403),
        ) as get:
            result = probe_environment(None)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(result.direct_status, 200)
        self.assertIsNone(result.proxy_status)

    def test_direct_disabled_skips_direct_request(self):
        with patch(
            "app.core.coupang.env_probe.curl_requests.get",
            side_effect=_get_returning(403, 200),
        ) as get:
            result = probe_environment(_PROXY, direct=False)
        self.assertEqual(get.call_count, 1)
        self.assertIsNone(result.direct_status)
        self.assertFalse(result.direct_blocked)
        self.assertEqual(result.proxy_status, 200)

    def test_proxy_request_uses_decodo_url(self):
        with patch(
            "app.core.coupang.env_probe.curl_requests.get",
            side_effect=_get_returning(200, 200),
        ) as get:
            probe_environment(_PROXY)
        proxy_calls = [c for c in get.call_args_list if c.kwargs.get("proxies")]
        self.assertEqual(len(proxy_calls), 1)
        url = proxy_calls[0].kwargs["proxies"]["https"]
        self.assertIn("u:p@proxy.example:7000", url)


if __name__ == "__main__":
    unittest.main()
