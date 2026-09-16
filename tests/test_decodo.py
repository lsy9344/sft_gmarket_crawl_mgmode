"""Decodo 설정·스티키 세션 조립 테스트 — 네트워크 없음.

실제 회선 연결 시험은 DECODO_LIVE_TEST=1 일 때만 실행한다
(저장된 Decodo 계정 또는 DECODO_USERNAME/DECODO_PASSWORD).
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from app.core import decodo


class NormalizeUserIdTest(unittest.TestCase):
    def test_strips_user_prefix_and_sticky_tail(self):
        self.assertEqual(decodo.normalize_user_id("sp3id"), "sp3id")
        self.assertEqual(decodo.normalize_user_id("user-sp3id"), "sp3id")
        self.assertEqual(
            decodo.normalize_user_id(
                "user-sp3id-session-abc-sessionduration-1440-country-kr"
            ),
            "sp3id",
        )
        self.assertEqual(decodo.normalize_user_id("  user-sp3id  "), "sp3id")


class StickyProxyTest(unittest.TestCase):
    def test_none_without_credentials(self):
        self.assertIsNone(decodo.sticky_proxy_dict(decodo.DecodoSettings()))
        self.assertFalse(decodo.credentials_ready(decodo.DecodoSettings()))

    def test_builds_sticky_username(self):
        s = decodo.DecodoSettings(username="user-sp3id", password="secret")
        proxy = decodo.sticky_proxy_dict(s, session_id="t12")
        self.assertIsNotNone(proxy)
        assert proxy is not None
        self.assertEqual(proxy["server"], "http://gate.decodo.com:7000")
        self.assertEqual(proxy["password"], "secret")
        self.assertEqual(
            proxy["username"],
            "user-sp3id-session-t12-sessionduration-1440-country-kr",
        )
        self.assertNotIn("secret", decodo.proxy_summary(proxy))
        self.assertTrue(decodo.credentials_ready(s))

    def test_session_id_strips_unsafe_chars(self):
        s = decodo.DecodoSettings(username="abc", password="pw")
        proxy = decodo.sticky_proxy_dict(s, session_id="t1-foo.bar")
        assert proxy is not None
        self.assertIn("-session-t1foobar-", proxy["username"])


class SettingsRoundTripTest(unittest.TestCase):
    def test_save_load_and_corrupt_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "decodo_settings.json")
            saved = decodo.save_settings(
                decodo.DecodoSettings(username="user-ab", password="pw1"),
                path,
            )
            loaded = decodo.load_settings(saved)
            self.assertEqual(loaded.username, "ab")
            self.assertEqual(loaded.password, "pw1")
            self.assertTrue(loaded.saved_at)

            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            fallback = decodo.load_settings(path)
            self.assertEqual(fallback.username, "")
            self.assertEqual(fallback.password, "")

            missing = decodo.load_settings(os.path.join(tmp, "nope.json"))
            self.assertEqual(missing.username, "")


class ExitIpParseTest(unittest.TestCase):
    def test_korea_from_nested_country_object(self):
        info = decodo.parse_exit_ip({
            "proxy": {"ip": "203.234.36.98"},
            "country": {"code": "KR", "name": "South Korea"},
        })
        self.assertEqual(info.ip, "203.234.36.98")
        self.assertTrue(info.is_korea)

    def test_korea_from_country_code_string(self):
        info = decodo.parse_exit_ip({"ip": "1.1.1.1", "country": "kr"})
        self.assertTrue(info.is_korea)

    def test_vietnam_is_not_korea(self):
        info = decodo.parse_exit_ip({
            "proxy": {"ip": "14.0.0.1"},
            "country": {"code": "VN", "name": "Vietnam"},
        })
        self.assertFalse(info.is_korea)

    def test_north_korea_is_not_korea(self):
        info = decodo.parse_exit_ip({
            "ip": "175.45.176.1",
            "country": {"code": "KP", "name": "North Korea"},
        })
        self.assertFalse(info.is_korea)

    def test_rejects_empty_payload(self):
        with self.assertRaises(decodo.DecodoError):
            decodo.parse_exit_ip({})
        with self.assertRaises(decodo.DecodoError):
            decodo.parse_exit_ip("nope")


class _FakeExitResponse:
    def __init__(self, status: int = 200, text: str = ""):
        self.status_code = status
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise decodo.requests.RequestException(f"HTTP {self.status_code}")


class _FakeExitSession:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def get(self, url, proxies=None, auth=None, timeout=None):
        self.calls.append({
            "url": url, "proxies": proxies, "auth": auth, "timeout": timeout,
        })
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def close(self) -> None:
        pass


class ProxyUrlTest(unittest.TestCase):
    def test_puts_credentials_on_the_proxy_not_the_target(self):
        url = decodo.proxy_url({
            "server": "http://gate.decodo.com:7000",
            "username": "user-sp-session-t1-sessionduration-1440-country-kr",
            "password": "s3cret!",
        })
        self.assertEqual(
            url,
            "http://user-sp-session-t1-sessionduration-1440-country-kr:s3cret%21"
            "@gate.decodo.com:7000",
        )

    def test_empty_server_is_blank(self):
        self.assertEqual(decodo.proxy_url({}), "")


class FetchExitIpTest(unittest.TestCase):
    def test_reads_korea_through_proxy_auth(self):
        payload = json.dumps({
            "proxy": {"ip": "183.105.1.2"},
            "country": {"code": "KR", "name": "South Korea"},
        })
        sess = _FakeExitSession(_FakeExitResponse(text=payload))
        proxy = {
            "server": "http://gate.decodo.com:7000",
            "username": "user-sp-session-t1-sessionduration-1440-country-kr",
            "password": "secret",
        }
        info = decodo.fetch_exit_ip(proxy, session=sess)
        self.assertTrue(info.is_korea)
        self.assertEqual(info.ip, "183.105.1.2")
        self.assertEqual(sess.calls[0]["url"], decodo.EXIT_IP_URL)
        self.assertIsNone(sess.calls[0]["auth"])
        proxy_url = sess.calls[0]["proxies"]["http"]
        self.assertIn("@gate.decodo.com:7000", proxy_url)
        self.assertIn(proxy["username"], proxy_url)
        self.assertIn("secret", proxy_url)

    def test_407_is_auth_error(self):
        sess = _FakeExitSession(_FakeExitResponse(status=407, text="denied"))
        with self.assertRaises(decodo.DecodoError) as ctx:
            decodo.fetch_exit_ip(
                {"server": "http://gate.decodo.com:7000",
                 "username": "u", "password": "p"},
                session=sess,
            )
        self.assertIn("407", str(ctx.exception))

    def test_missing_proxy_raises(self):
        with self.assertRaises(decodo.DecodoError):
            decodo.fetch_exit_ip({})


@unittest.skipUnless(
    os.environ.get("DECODO_LIVE_TEST") == "1",
    "실제 Decodo 연결 시험 — DECODO_LIVE_TEST=1 필요",
)
class LiveDecodoExitIpTest(unittest.TestCase):
    def test_sticky_session_exit_is_korea(self):
        user = os.environ.get("DECODO_USERNAME", "").strip()
        password = os.environ.get("DECODO_PASSWORD", "").strip()
        if user and password:
            settings = decodo.DecodoSettings(username=user, password=password)
        else:
            settings = decodo.load_settings()
        if not decodo.credentials_ready(settings):
            self.skipTest("Decodo 계정이 없습니다")
        proxy = decodo.sticky_proxy_dict(settings, session_id="livetest1")
        self.assertIsNotNone(proxy)
        assert proxy is not None
        info = decodo.fetch_exit_ip(proxy)
        self.assertTrue(
            info.is_korea,
            f"출발 국가가 한국이 아닙니다: {info.country_code} {info.country_name} ip={info.ip}",
        )
