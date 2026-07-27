"""강화 테스트: AC-22 table-driven, AC-23 preflight, CSV injection, sentinel in responses."""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.base import Control
from app.core.coupang.crawler import CoupangCrawler
from app.core.coupang.exporter import CoupangExporter, _csv_safe
from app.core.coupang.preflight import (
    PINNED_BROWSER_CHANNEL,
    PINNED_BROWSER_VERSION,
    PINNED_CAMOUFOX_VERSION,
    PreflightStatus,
    _check_browser,
    _check_geoip,
    _check_package_version,
    check_runtime,
)
from app.models.coupang_records import (
    CoupangRunConfig,
    CoupangRunSummary,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "coupang_crawl"))
from coupang_omp_crawler import determine_exit_code


def _make_config(tmp_dir, **kwargs):
    defaults = {
        "output_dir": Path(tmp_dir),
        "output_prefix": "test",
        "max_scroll_pages": 3,
        "batch_size": 5,
        "warmup_time": 0,
        "delay_min": 0,
        "delay_max": 0,
    }
    defaults.update(kwargs)
    return CoupangRunConfig(**defaults)


class FakeMouse:
    def move(self, x, y):
        pass

    def wheel(self, x, y):
        pass


class FakePage:
    def __init__(self, promotion_pages=None, vendors_response=None, review_responses=None):
        self.mouse = FakeMouse()
        self._handlers = []
        self._promotion_pages = promotion_pages or []
        self._promotion_call = 0
        self._vendors_response = vendors_response
        self._review_responses = review_responses or {}

    def on(self, event, handler):
        self._handlers.append(handler)

    def goto(self, url, **kwargs):
        if "/np/omp" in str(url):
            req = MagicMock()
            req.url = "https://www.coupang.com/np/omp/api/getPromotion"
            req.post_data = json.dumps({"query": {"feedId": "f", "continuationToken": "seemore=CGs="}})
            for h in self._handlers:
                h(req)

    def evaluate(self, script, *args):
        if "getPromotion" in script:
            if self._promotion_call < len(self._promotion_pages):
                resp = self._promotion_pages[self._promotion_call]
                self._promotion_call += 1
                return resp
            return {"status": 200, "body": json.dumps({"ret": "0", "data": {"promotionData": [], "token": None}})}
        if "individualInfo" in script:
            return self._vendors_response or {"status": 200, "body": json.dumps({"code": 200, "data": {"products": []}})}
        if "getStoreReview" in script:
            vid = args[0] if args else ""
            return self._review_responses.get(vid, {"status": 500, "body": ""})
        return {}


class FakeBrowser:
    def __init__(self, page):
        self._page = page
        self.closed = False

    def new_page(self):
        return self._page

    def close(self):
        self.closed = True


def _promo(items, token=None):
    return {"status": 200, "body": json.dumps({"ret": "0", "data": {"promotionData": items, "token": token}})}


def _items(n, start=1):
    return [{"vendorItemId": f"VI{start+i}", "itemId": f"I{start+i}", "title": f"T{start+i}", "categoryId": "1"} for i in range(n)]


def _products(viids):
    return [{"productId": f"P{i}", "itemId": f"I{i}", "vendorItemId": viid,
             "storeInfoArea": {"vendorId": f"V{i}", "storeId": 1, "displayName": f"S{i}"}}
            for i, viid in enumerate(viids)]


def _review(name="상호", **kw):
    return {"status": 200, "body": json.dumps({
        "name": name, "repPersonName": "대표", "businessNumber": "123-45-67890",
        "repPhoneNum": "02-000-0000", "repEmail": "t@t.com",
        "repAddr1": "서울", "repAddr2": "동", "eCommerceReportNumber": "2024-001",
        "qualitySellerBadgeDto": kw.get("badge"), "ratingCount": 10, "thumbUpRatio": 90.0,
    })}


class ExitTruthTableTest(unittest.TestCase):
    """AC-22: table-driven test of termination → file/exit/UI behavior.

    Uses production determine_exit_code() from coupang_omp_crawler.py directly.
    """

    def _run(self, page, tmp_dir, **kw):
        config = _make_config(tmp_dir, **kw)
        crawler = CoupangCrawler(config=config, control=Control(), browser_factory=lambda: FakeBrowser(page))
        return crawler.run()

    def test_success_with_records(self):
        """token_exhausted + records → final files, exit 0, UI success."""
        items = _items(2)
        prods = _products(["VI1", "VI2"])
        reviews = {"V0": _review(), "V1": _review("상호1")}
        page = FakePage([_promo(items, None)],
                        {"status": 200, "body": json.dumps({"code": 200, "data": {"products": prods}})},
                        reviews)
        with tempfile.TemporaryDirectory() as tmp:
            s = self._run(page, tmp)
        self.assertTrue(s.records)
        self.assertIsNotNone(s.json_path)
        self.assertNotIn("_partial", s.json_path)
        self.assertIsNone(s.error)
        self.assertFalse(s.cancelled)
        self.assertEqual(determine_exit_code(s), 0)

    def test_no_items_empty_final_files(self):
        """no_items → empty final files, exit 2, UI failure."""
        page = FakePage([_promo([], None)])
        with tempfile.TemporaryDirectory() as tmp:
            s = self._run(page, tmp)
            self.assertEqual(s.termination_reason, "no_items")
            self.assertIsNotNone(s.json_path)
            self.assertNotIn("_partial", s.json_path)
            with open(s.json_path, "r", encoding="utf-8") as f:
                self.assertEqual(json.load(f), [])
            self.assertEqual(determine_exit_code(s), 2)

    def test_cancelled_with_records_partial(self):
        """cancelled + records → _partial files, exit 130."""
        items = _items(2)
        prods = _products(["VI1", "VI2"])
        reviews = {"V0": _review(), "V1": _review("상호1")}
        page = FakePage([_promo(items, None)],
                        {"status": 200, "body": json.dumps({"code": 200, "data": {"products": prods}})},
                        reviews)
        control = Control()
        config = _make_config("/unused")

        call_count = [0]
        orig_eval = page.evaluate

        def cancel_after_first(script, *args):
            r = orig_eval(script, *args)
            if "getStoreReview" in script:
                call_count[0] += 1
                if call_count[0] == 1:
                    control.request_cancel()
            return r

        page.evaluate = cancel_after_first

        with tempfile.TemporaryDirectory() as tmp:
            config.output_dir = Path(tmp)
            crawler = CoupangCrawler(config=config, control=control, browser_factory=lambda: FakeBrowser(page))
            s = crawler.run()

        self.assertTrue(s.cancelled)
        self.assertIsNotNone(s.json_path)
        self.assertIn("_partial", s.json_path)
        self.assertEqual(determine_exit_code(s), 130)

    def test_cancelled_no_records(self):
        """cancelled + no records → no files, exit 130."""
        page = FakePage([_promo(_items(2), "tok")])
        control = Control()
        control.request_cancel()

        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            crawler = CoupangCrawler(config=config, control=control, browser_factory=lambda: FakeBrowser(page))
            s = crawler.run()

        self.assertTrue(s.cancelled)
        self.assertFalse(s.records)
        self.assertEqual(determine_exit_code(s), 130)

    def test_api_error_first_page_no_records(self):
        """API error on first page + no records → exit 1 (error priority)."""
        page = FakePage([{"error": "network failure"}])
        with tempfile.TemporaryDirectory() as tmp:
            s = self._run(page, tmp)
        self.assertEqual(s.termination_reason, "error")
        self.assertIsNotNone(s.error)
        self.assertFalse(s.records)
        self.assertEqual(determine_exit_code(s), 1)

    def test_api_error_mid_feed_with_records(self):
        """Mid-feed error + prior records → partial files, exit 1."""
        pages = [_promo(_items(2), "tok1"), {"error": "network failure"}]
        prods = _products(["VI1", "VI2"])
        reviews = {"V0": _review(), "V1": _review("상호1")}
        page = FakePage(pages,
                        {"status": 200, "body": json.dumps({"code": 200, "data": {"products": prods}})},
                        reviews)
        with tempfile.TemporaryDirectory() as tmp:
            s = self._run(page, tmp)
        self.assertEqual(s.termination_reason, "error")
        self.assertIsNotNone(s.error)
        self.assertTrue(s.records)
        self.assertIsNotNone(s.json_path)
        self.assertIn("_partial", s.json_path)
        self.assertEqual(determine_exit_code(s), 1)

    def test_page_limit_with_records(self):
        """page_limit + records → final files, exit 0, UI warning."""
        pages = [_promo(_items(3, start=i * 3 + 1), f"tok{i}") for i in range(5)]
        all_viids = [f"VI{i}" for i in range(1, 7)]
        prods = _products(all_viids)
        reviews = {f"V{i}": _review(f"S{i}") for i in range(6)}
        page = FakePage(pages,
                        {"status": 200, "body": json.dumps({"code": 200, "data": {"products": prods}})},
                        reviews)
        with tempfile.TemporaryDirectory() as tmp:
            s = self._run(page, tmp, max_scroll_pages=2)
        self.assertEqual(s.termination_reason, "page_limit")
        self.assertTrue(s.records)
        self.assertIsNotNone(s.json_path)
        self.assertNotIn("_partial", s.json_path)
        self.assertEqual(determine_exit_code(s), 0)

    def test_csv_save_failure_exit_3(self):
        """CSV write failure after JSON success → save_error set, exit 3."""
        items = _items(1)
        prods = _products(["VI1"])
        reviews = {"V0": _review()}
        page = FakePage([_promo(items, None)],
                        {"status": 200, "body": json.dumps({"code": 200, "data": {"products": prods}})},
                        reviews)
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            crawler = CoupangCrawler(config=config, control=Control(), browser_factory=lambda: FakeBrowser(page))
            with patch.object(CoupangExporter, "_atomic_write_csv", side_effect=OSError("disk full")):
                s = crawler.run()

        self.assertIsNotNone(s.json_path)
        self.assertIsNone(s.csv_path)
        self.assertIsNotNone(s.save_error)
        self.assertEqual(determine_exit_code(s), 3)

    def test_csv_save_failure_ui_detection(self):
        """UI detects save failure via structured save_error field."""
        s = CoupangRunSummary()
        s.records = [{"vendor_id": "V1"}]
        s.json_path = "/tmp/test.json"
        s.csv_path = None
        s.save_error = "CSV 쓰기 실패 (JSON 저장됨: /tmp/test.json)"
        s.termination_reason = "token_exhausted"
        self.assertIsNotNone(s.save_error)
        self.assertEqual(determine_exit_code(s), 3)


class PreflightTest(unittest.TestCase):
    """AC-23: preflight distinguishes package/version/browser/GeoIP/freshness states."""

    def _make_cache(self, tmp, browser=False, geoip=False, geoip_age_days=0,
                    browser_version=None, channel=None, config_json=True,
                    corrupt_version_json=False, no_executable=False):
        """Create realistic camoufox cache structure."""
        install_dir = Path(tmp)
        if browser:
            ver = browser_version or f"{PINNED_BROWSER_VERSION}-924f3109"
            ch = channel or PINNED_BROWSER_CHANNEL
            ver_dir = install_dir / "browsers" / ch / ver
            ver_dir.mkdir(parents=True)
            if corrupt_version_json:
                (ver_dir / "version.json").write_text("{}")
            else:
                parts = ver.split("-", 1)
                vj = {"version": parts[0] if parts else ver, "build": parts[1] if len(parts) > 1 else "x"}
                if browser_version and browser_version != f"{PINNED_BROWSER_VERSION}-924f3109":
                    vj = {"version": "999.0.0", "build": "abc"}
                else:
                    vj = {"version": "152.0.4", "build": "beta.28"}
                (ver_dir / "version.json").write_text(json.dumps(vj))
            if not no_executable:
                import platform as _plat
                _launch = {"Windows": "camoufox.exe", "Darwin": "Camoufox.app/Contents/MacOS/camoufox", "Linux": "camoufox-bin"}
                exe_name = _launch.get(_plat.system(), "camoufox-bin")
                exe = ver_dir / exe_name
                exe.parent.mkdir(parents=True, exist_ok=True)
                exe.write_text("#!/bin/sh\n")
                exe.chmod(0o755)
            if config_json:
                config = {
                    "active_version": f"browsers/{ch}/{ver}",
                    "channel": f"{ch}/stable",
                    "pinned": PINNED_BROWSER_VERSION,
                }
                (install_dir / "config.json").write_text(json.dumps(config))
        if geoip:
            mmdb_dir = install_dir / "geoip" / "mmdb"
            mmdb_dir.mkdir(parents=True)
            ipv4_file = mmdb_dir / "maxmind geolite2-ipv4.mmdb"
            ipv6_file = mmdb_dir / "maxmind geolite2-ipv6.mmdb"
            ipv4_file.write_bytes(b"\x00" * 100)
            ipv6_file.write_bytes(b"\x00" * 100)
            (install_dir / "geoip" / "config.yml").write_text("name: MaxMind GeoLite2\n")
            if geoip_age_days > 0:
                old_time = time.time() - (geoip_age_days * 86400)
                os.utime(ipv4_file, (old_time, old_time))
                os.utime(ipv6_file, (old_time, old_time))
        return install_dir

    def test_package_missing(self):
        with patch("app.core.coupang.preflight._check_package_version") as mock:
            from app.core.coupang.preflight import PreflightResult
            mock.return_value = PreflightResult(
                PreflightStatus.PACKAGE_MISSING, "missing")
            with patch("app.core.coupang.preflight._get_install_dir", return_value=None):
                result = check_runtime()
        self.assertEqual(result.status, PreflightStatus.PACKAGE_MISSING)

    def test_package_version_mismatch(self):
        """camoufox installed but wrong version → PACKAGE_VERSION_MISMATCH."""
        fake_mod = MagicMock()
        with (
            patch.dict("sys.modules", {"camoufox": fake_mod}),
            patch("app.core.coupang.preflight._get_camoufox_version", return_value="0.4.0"),
        ):
            result = _check_package_version()
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PreflightStatus.PACKAGE_VERSION_MISMATCH)
        self.assertIn("0.4.0", result.message)

    def test_package_version_ok(self):
        """camoufox with correct version → None (pass)."""
        fake_mod = MagicMock()
        with (
            patch.dict("sys.modules", {"camoufox": fake_mod}),
            patch(
                "app.core.coupang.preflight._get_camoufox_version",
                return_value=PINNED_CAMOUFOX_VERSION,
            ),
        ):
            result = _check_package_version()
        self.assertIsNone(result)

    def test_browser_missing_no_data_dir(self):
        """Data dir path returns None."""
        with (
            patch("app.core.coupang.preflight._get_install_dir", return_value=None),
            patch("app.core.coupang.preflight._check_package_version", return_value=None),
        ):
            result = check_runtime()
        self.assertEqual(result.status, PreflightStatus.BROWSER_MISSING)

    def test_browser_missing_empty_browsers(self):
        """browsers/ dir doesn't exist → BROWSER_MISSING."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp)
            result = _check_browser(install_dir)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PreflightStatus.BROWSER_MISSING)

    def test_browser_version_mismatch(self):
        """Browser installed but wrong version → BROWSER_VERSION_MISMATCH."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = self._make_cache(tmp, browser=True, browser_version="999.0.0-abc")
            result = _check_browser(install_dir)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PreflightStatus.BROWSER_VERSION_MISMATCH)
        self.assertIn("999.0.0", result.message)

    def test_browser_wrong_channel(self):
        """Browser in wrong channel → BROWSER_VERSION_MISMATCH."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = self._make_cache(tmp, browser=True, channel="beta")
            result = _check_browser(install_dir)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PreflightStatus.BROWSER_VERSION_MISMATCH)

    def test_browser_ok(self):
        """Correct channel + version → None (pass)."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = self._make_cache(tmp, browser=True)
            result = _check_browser(install_dir)
        self.assertIsNone(result)

    def test_geoip_missing(self):
        """No .mmdb files → GEOIP_MISSING."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = self._make_cache(tmp, browser=True, geoip=False)
            result = _check_geoip(install_dir)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PreflightStatus.GEOIP_MISSING)

    def test_geoip_stale(self):
        """GeoIP older than 30 days → GEOIP_STALE."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = self._make_cache(tmp, geoip=True, geoip_age_days=45)
            result = _check_geoip(install_dir)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PreflightStatus.GEOIP_STALE)
        self.assertIn("45", result.message)

    def test_geoip_fresh_ok(self):
        """GeoIP within 30 days → None (pass)."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = self._make_cache(tmp, geoip=True, geoip_age_days=5)
            result = _check_geoip(install_dir)
        self.assertIsNone(result)

    def test_geoip_no_cross_provider_fallback(self):
        """HIGH-2 regression: leftover GeoLite2 files must not satisfy a different provider."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp)
            mmdb_dir = install_dir / "geoip" / "mmdb"
            mmdb_dir.mkdir(parents=True)
            (install_dir / "geoip" / "config.yml").write_text("name: GeoIP AIO by daijro\n")
            (mmdb_dir / "maxmind geolite2-ipv4.mmdb").write_bytes(b"\x00" * 100)
            result = _check_geoip(install_dir)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PreflightStatus.GEOIP_MISSING)

    def test_geoip_split_ipv6_missing(self):
        """HIGH-2: split provider with IPv6 missing must fail."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp)
            mmdb_dir = install_dir / "geoip" / "mmdb"
            mmdb_dir.mkdir(parents=True)
            (install_dir / "geoip" / "config.yml").write_text("name: MaxMind GeoLite2\n")
            (mmdb_dir / "maxmind geolite2-ipv4.mmdb").write_bytes(b"\x00" * 100)
            result = _check_geoip(install_dir)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PreflightStatus.GEOIP_MISSING)

    def test_geoip_split_ipv6_stale(self):
        """HIGH-2: split provider with stale IPv6 must report GEOIP_STALE."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp)
            mmdb_dir = install_dir / "geoip" / "mmdb"
            mmdb_dir.mkdir(parents=True)
            (install_dir / "geoip" / "config.yml").write_text("name: MaxMind GeoLite2\n")
            ipv4 = mmdb_dir / "maxmind geolite2-ipv4.mmdb"
            ipv6 = mmdb_dir / "maxmind geolite2-ipv6.mmdb"
            ipv4.write_bytes(b"\x00" * 100)
            ipv6.write_bytes(b"\x00" * 100)
            old_time = time.time() - (45 * 86400)
            os.utime(ipv6, (old_time, old_time))
            result = _check_geoip(install_dir)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PreflightStatus.GEOIP_STALE)

    def test_geoip_combined_provider_wrong_ipv4_only(self):
        """HIGH-2: combined provider with only an ipv4 file must fail."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp)
            mmdb_dir = install_dir / "geoip" / "mmdb"
            mmdb_dir.mkdir(parents=True)
            (install_dir / "geoip" / "config.yml").write_text("name: GeoIP AIO by daijro\n")
            (mmdb_dir / "geoip aio by daijro-ipv4.mmdb").write_bytes(b"\x00" * 100)
            result = _check_geoip(install_dir)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PreflightStatus.GEOIP_MISSING)

    def test_geoip_split_provider_fake_combined(self):
        """HIGH-2: split provider with only a combined file must fail."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp)
            mmdb_dir = install_dir / "geoip" / "mmdb"
            mmdb_dir.mkdir(parents=True)
            (install_dir / "geoip" / "config.yml").write_text("name: MaxMind GeoLite2\n")
            (mmdb_dir / "maxmind geolite2-combined.mmdb").write_bytes(b"\x00" * 100)
            result = _check_geoip(install_dir)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PreflightStatus.GEOIP_MISSING)

    def test_geoip_combined_provider_valid(self):
        """HIGH-2: combined provider with valid combined file passes."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp)
            mmdb_dir = install_dir / "geoip" / "mmdb"
            mmdb_dir.mkdir(parents=True)
            (install_dir / "geoip" / "config.yml").write_text("name: GeoIP AIO by daijro\n")
            (mmdb_dir / "geoip aio by daijro-combined.mmdb").write_bytes(b"\x00" * 100)
            result = _check_geoip(install_dir)
        self.assertIsNone(result)

    def test_ok_all_present(self):
        """All conditions met → OK."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = self._make_cache(tmp, browser=True, geoip=True)
            with (
                patch("app.core.coupang.preflight._check_package_version", return_value=None),
                patch("app.core.coupang.preflight._get_install_dir", return_value=install_dir),
            ):
                result = check_runtime()
        self.assertEqual(result.status, PreflightStatus.OK)

    def test_full_check_wrong_browser_version(self):
        """Full check_runtime with wrong browser version → BROWSER_VERSION_MISMATCH."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = self._make_cache(tmp, browser=True, geoip=True, browser_version="100.0.0-x")
            with (
                patch("app.core.coupang.preflight._check_package_version", return_value=None),
                patch("app.core.coupang.preflight._get_install_dir", return_value=install_dir),
            ):
                result = check_runtime()
        self.assertEqual(result.status, PreflightStatus.BROWSER_VERSION_MISMATCH)

    def test_full_check_stale_geoip(self):
        """Full check_runtime with stale GeoIP → GEOIP_STALE."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = self._make_cache(tmp, browser=True, geoip=True, geoip_age_days=60)
            with (
                patch("app.core.coupang.preflight._check_package_version", return_value=None),
                patch("app.core.coupang.preflight._get_install_dir", return_value=install_dir),
            ):
                result = check_runtime()
        self.assertEqual(result.status, PreflightStatus.GEOIP_STALE)

    def test_real_environment(self):
        """Integration: check_runtime() returns a valid status on this machine."""
        result = check_runtime()
        self.assertIn(result.status, list(PreflightStatus))

    def test_corrupt_version_json_rejected(self):
        """version.json without required keys → BROWSER_VERSION_MISMATCH."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = self._make_cache(tmp, browser=True, corrupt_version_json=True)
            result = _check_browser(install_dir)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PreflightStatus.BROWSER_VERSION_MISMATCH)

    def test_missing_executable_rejected(self):
        """No executable in browser dir → BROWSER_VERSION_MISMATCH."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = self._make_cache(tmp, browser=True, no_executable=True)
            result = _check_browser(install_dir)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PreflightStatus.BROWSER_VERSION_MISMATCH)
        self.assertIn("실행 파일", result.message)

    def test_empty_active_version_with_valid_cache_rejected(self):
        """HIGH-1 regression: empty active_version must not fall through to dir scan."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp)
            ver_dir = install_dir / "browsers" / "official" / f"{PINNED_BROWSER_VERSION}-924f3109"
            ver_dir.mkdir(parents=True)
            (ver_dir / "version.json").write_text(json.dumps({"version": "152.0.4", "build": "beta.28"}))
            import platform as _plat
            _exe_name = {"Windows": "camoufox.exe", "Darwin": "Camoufox.app/Contents/MacOS/camoufox", "Linux": "camoufox-bin"}.get(_plat.system(), "camoufox-bin")
            exe = ver_dir / _exe_name
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
            config = {"active_version": "", "channel": "official/stable", "pinned": PINNED_BROWSER_VERSION}
            (install_dir / "config.json").write_text(json.dumps(config))
            result = _check_browser(install_dir)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PreflightStatus.BROWSER_MISSING)

    def test_version_prefix_attack_rejected(self):
        """HIGH-1 regression: '152.0.4-beta.28-evil' must not pass exact match."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp)
            ver_dir = install_dir / "browsers" / "official" / "152.0.4-beta.28-evil"
            ver_dir.mkdir(parents=True)
            (ver_dir / "version.json").write_text(json.dumps({"version": "152.0.4", "build": "beta.28-evil"}))
            import platform as _plat
            _exe_name = {"Windows": "camoufox.exe", "Darwin": "Camoufox.app/Contents/MacOS/camoufox", "Linux": "camoufox-bin"}.get(_plat.system(), "camoufox-bin")
            exe = ver_dir / _exe_name
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
            config = {
                "active_version": "browsers/official/152.0.4-beta.28-evil",
                "channel": "official/stable",
                "pinned": PINNED_BROWSER_VERSION,
            }
            (install_dir / "config.json").write_text(json.dumps(config))
            result = _check_browser(install_dir)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PreflightStatus.BROWSER_VERSION_MISMATCH)

    def test_wrong_pinned_in_config_rejected(self):
        """HIGH-1 regression: config.json pinned != PINNED_BROWSER_VERSION must fail."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp)
            ver_dir = install_dir / "browsers" / "official" / "999.0.0"
            ver_dir.mkdir(parents=True)
            (ver_dir / "version.json").write_text(json.dumps({"version": "999.0.0", "build": "x"}))
            import platform as _plat
            _exe_name = {"Windows": "camoufox.exe", "Darwin": "Camoufox.app/Contents/MacOS/camoufox", "Linux": "camoufox-bin"}.get(_plat.system(), "camoufox-bin")
            exe = ver_dir / _exe_name
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
            config = {
                "active_version": "browsers/official/999.0.0",
                "channel": "official/stable",
                "pinned": "999.0.0",
            }
            (install_dir / "config.json").write_text(json.dumps(config))
            result = _check_browser(install_dir)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PreflightStatus.BROWSER_VERSION_MISMATCH)

    def test_path_traversal_rejected(self):
        """HIGH-1: active_version with ../ traversal must be rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp)
            escape_dir = install_dir / "escape"
            escape_dir.mkdir()
            (escape_dir / "version.json").write_text(json.dumps({"version": "152.0.4", "build": "beta.28"}))
            (install_dir / "browsers" / "official").mkdir(parents=True)
            config = {
                "active_version": "browsers/official/../../escape",
                "channel": "official/stable",
                "pinned": PINNED_BROWSER_VERSION,
            }
            (install_dir / "config.json").write_text(json.dumps(config))
            result = _check_browser(install_dir)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PreflightStatus.BROWSER_VERSION_MISMATCH)
        self.assertIn("형식이 잘못되었습니다", result.message)

    def test_symlink_escape_rejected(self):
        """HIGH-1: symlinked active_version dir must be rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp)
            real_dir = Path(tmp) / "real_target"
            real_dir.mkdir()
            official_dir = install_dir / "browsers" / "official"
            official_dir.mkdir(parents=True)
            link = official_dir / "linked_version"
            link.symlink_to(real_dir)
            config = {
                "active_version": "browsers/official/linked_version",
                "channel": "official/stable",
                "pinned": PINNED_BROWSER_VERSION,
            }
            (install_dir / "config.json").write_text(json.dumps(config))
            result = _check_browser(install_dir)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PreflightStatus.BROWSER_VERSION_MISMATCH)
        self.assertIn("심볼릭 링크", result.message)

    def test_wrong_platform_executable_rejected(self):
        """HIGH-1: only the current OS executable is accepted."""
        import platform as _plat
        if _plat.system() == "Linux":
            wrong_exe = "camoufox.exe"
        else:
            wrong_exe = "camoufox-bin"
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp)
            ver_dir = install_dir / "browsers" / "official" / "152.0.4-beta.28"
            ver_dir.mkdir(parents=True)
            (ver_dir / "version.json").write_text(json.dumps({"version": "152.0.4", "build": "beta.28"}))
            exe = ver_dir / wrong_exe
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
            config = {
                "active_version": "browsers/official/152.0.4-beta.28",
                "channel": "official/stable",
                "pinned": PINNED_BROWSER_VERSION,
            }
            (install_dir / "config.json").write_text(json.dumps(config))
            result = _check_browser(install_dir)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PreflightStatus.BROWSER_VERSION_MISMATCH)
        self.assertIn("실행 파일", result.message)

    def test_official_dir_symlink_escape_rejected(self):
        """HIGH-1: browsers/official itself being a symlink must be rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp)
            external = Path(tmp) / "external"
            active_dir = external / "active"
            active_dir.mkdir(parents=True)
            (active_dir / "version.json").write_text(json.dumps({"version": "152.0.4", "build": "beta.28"}))
            import platform as _plat
            _exe = {"Windows": "camoufox.exe", "Darwin": "Camoufox.app/Contents/MacOS/camoufox", "Linux": "camoufox-bin"}.get(_plat.system(), "camoufox-bin")
            exe = active_dir / _exe
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
            browsers_dir = install_dir / "browsers"
            browsers_dir.mkdir()
            (browsers_dir / "official").symlink_to(external)
            config = {
                "active_version": "browsers/official/active",
                "channel": "official/stable",
                "pinned": PINNED_BROWSER_VERSION,
            }
            (install_dir / "config.json").write_text(json.dumps(config))
            result = _check_browser(install_dir)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PreflightStatus.BROWSER_VERSION_MISMATCH)
        self.assertIn("심볼릭 링크", result.message)

    def test_malformed_active_version_type(self):
        """MEDIUM-3: non-string active_version must return PreflightResult, not raise."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp)
            (install_dir / "browsers" / "official").mkdir(parents=True)
            config = {"active_version": 123, "channel": "official/stable", "pinned": PINNED_BROWSER_VERSION}
            (install_dir / "config.json").write_text(json.dumps(config))
            result = _check_browser(install_dir)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PreflightStatus.BROWSER_MISSING)

    def test_geoip_near_match_split_rejected(self):
        """HIGH-2: 'maxmind geolite2-evil-ipv4.mmdb' must not satisfy exact path."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp)
            mmdb_dir = install_dir / "geoip" / "mmdb"
            mmdb_dir.mkdir(parents=True)
            (install_dir / "geoip" / "config.yml").write_text("name: MaxMind GeoLite2\n")
            (mmdb_dir / "maxmind geolite2-evil-ipv4.mmdb").write_bytes(b"\x00" * 100)
            (mmdb_dir / "maxmind geolite2-evil-ipv6.mmdb").write_bytes(b"\x00" * 100)
            result = _check_geoip(install_dir)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PreflightStatus.GEOIP_MISSING)

    def test_geoip_near_match_combined_rejected(self):
        """HIGH-2: 'geoip aio by daijro-evil-combined.mmdb' must not satisfy exact path."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp)
            mmdb_dir = install_dir / "geoip" / "mmdb"
            mmdb_dir.mkdir(parents=True)
            (install_dir / "geoip" / "config.yml").write_text("name: GeoIP AIO by daijro\n")
            (mmdb_dir / "geoip aio by daijro-evil-combined.mmdb").write_bytes(b"\x00" * 100)
            result = _check_geoip(install_dir)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PreflightStatus.GEOIP_MISSING)

    def test_geoip_duplicate_yaml_name_last_wins(self):
        """HIGH-3: duplicate YAML name key — last value wins per YAML spec."""
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp)
            mmdb_dir = install_dir / "geoip" / "mmdb"
            mmdb_dir.mkdir(parents=True)
            (install_dir / "geoip" / "config.yml").write_text(
                "name: GeoIP AIO by daijro\nname: MaxMind GeoLite2\n"
            )
            (mmdb_dir / "maxmind geolite2-ipv4.mmdb").write_bytes(b"\x00" * 100)
            (mmdb_dir / "maxmind geolite2-ipv6.mmdb").write_bytes(b"\x00" * 100)
            result = _check_geoip(install_dir)
        self.assertIsNone(result)


class BrowserCloseFailureTest(unittest.TestCase):
    """HIGH-3: browser close failure → cleanup_error set, exit 1."""

    def test_close_failure_after_success_sets_cleanup_error(self):
        """Normal run + close failure → cleanup_error, exit 1."""
        items = _items(1)
        prods = _products(["VI1"])
        reviews = {"V0": _review()}
        page = FakePage([_promo(items, None)],
                        {"status": 200, "body": json.dumps({"code": 200, "data": {"products": prods}})},
                        reviews)

        class ExplodingBrowser:
            def __init__(self, p):
                self._page = p
            def new_page(self):
                return self._page
            def close(self):
                raise RuntimeError("close boom")

        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            crawler = CoupangCrawler(config=config, control=Control(),
                                     browser_factory=lambda: ExplodingBrowser(page))
            s = crawler.run()

        self.assertIsNotNone(s.cleanup_error)
        self.assertIn("close boom", s.cleanup_error)
        self.assertTrue(s.records)
        self.assertEqual(determine_exit_code(s), 1)

    def test_close_failure_after_cancel_sets_cleanup_error(self):
        """Cancel + close failure → cleanup_error takes priority, exit 1."""
        page = FakePage([_promo([], None)])
        control = Control()
        control.request_cancel()

        class ExplodingBrowser:
            def __init__(self, p):
                self._page = p
            def new_page(self):
                return self._page
            def close(self):
                raise RuntimeError("close boom")

        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            crawler = CoupangCrawler(config=config, control=control,
                                     browser_factory=lambda: ExplodingBrowser(page))
            s = crawler.run()

        self.assertTrue(s.cancelled)
        self.assertIsNotNone(s.cleanup_error)
        self.assertEqual(determine_exit_code(s), 1)


class StatsSnapshotTest(unittest.TestCase):
    """M1: stats signal must send immutable snapshots, not mutable summary."""

    def test_stats_snapshots_are_independent(self):
        """Each stats emission is a snapshot; mutating summary later doesn't affect it."""
        items = _items(6)
        prods = _products([f"VI{i}" for i in range(1, 7)])
        reviews = {f"V{i}": _review(f"S{i}") for i in range(6)}
        page = FakePage([_promo(items, None)],
                        {"status": 200, "body": json.dumps({"code": 200, "data": {"products": prods}})},
                        reviews)

        snapshots = []

        def capture_stats(s):
            snapshots.append(s)

        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            crawler = CoupangCrawler(config=config, control=Control(),
                                     browser_factory=lambda: FakeBrowser(page),
                                     on_stats=capture_stats)
            final = crawler.run()

        self.assertTrue(len(snapshots) >= 2)
        first = snapshots[0]
        self.assertLess(first.business_info_success, final.business_info_success)
        self.assertIsNot(first, final)


class SetupToolVerifyTest(unittest.TestCase):
    """Setup tool verify_postcondition tests — uses same _check_browser/_check_geoip as app."""

    def test_verify_pass_all_good(self):
        """All conditions met → verify_postcondition returns True."""
        from scripts.setup_coupang_runtime import verify_postcondition
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp)
            mmdb = install_dir / "test.mmdb"
            mmdb.write_bytes(b"\x00")
            with (
                patch("importlib.metadata.version", return_value=PINNED_CAMOUFOX_VERSION),
                patch("scripts.setup_coupang_runtime._get_install_dir", return_value=install_dir),
                patch("scripts.setup_coupang_runtime._check_browser", return_value=None),
                patch("scripts.setup_coupang_runtime._check_geoip", return_value=None),
                patch(
                    "scripts.setup_coupang_runtime._find_browser_version",
                    return_value=f"{PINNED_BROWSER_VERSION}-924f3109",
                ),
                patch("scripts.setup_coupang_runtime._find_geoip_ipv4", return_value=mmdb),
            ):
                result = verify_postcondition()
        self.assertTrue(result)

    def test_verify_fail_wrong_browser(self):
        """Wrong browser version → False."""
        from app.core.coupang.preflight import PreflightResult
        from scripts.setup_coupang_runtime import verify_postcondition
        err = PreflightResult(PreflightStatus.BROWSER_VERSION_MISMATCH, "bad version")
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp)
            mmdb = install_dir / "test.mmdb"
            mmdb.write_bytes(b"\x00")
            with (
                patch("importlib.metadata.version", return_value=PINNED_CAMOUFOX_VERSION),
                patch("scripts.setup_coupang_runtime._get_install_dir", return_value=install_dir),
                patch("scripts.setup_coupang_runtime._check_browser", return_value=err),
                patch("scripts.setup_coupang_runtime._check_geoip", return_value=None),
                patch("scripts.setup_coupang_runtime._find_geoip_ipv4", return_value=mmdb),
            ):
                result = verify_postcondition()
        self.assertFalse(result)

    def test_verify_fail_stale_geoip(self):
        """Stale GeoIP → False."""
        from app.core.coupang.preflight import PreflightResult
        from scripts.setup_coupang_runtime import verify_postcondition
        err = PreflightResult(PreflightStatus.GEOIP_STALE, "stale")
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp)
            with (
                patch("importlib.metadata.version", return_value=PINNED_CAMOUFOX_VERSION),
                patch("scripts.setup_coupang_runtime._get_install_dir", return_value=install_dir),
                patch("scripts.setup_coupang_runtime._check_browser", return_value=None),
                patch("scripts.setup_coupang_runtime._check_geoip", return_value=err),
            ):
                result = verify_postcondition()
        self.assertFalse(result)

    def test_verify_fail_no_geoip(self):
        """No GeoIP → False."""
        from app.core.coupang.preflight import PreflightResult
        from scripts.setup_coupang_runtime import verify_postcondition
        err = PreflightResult(PreflightStatus.GEOIP_MISSING, "missing")
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp)
            with (
                patch("importlib.metadata.version", return_value=PINNED_CAMOUFOX_VERSION),
                patch("scripts.setup_coupang_runtime._get_install_dir", return_value=install_dir),
                patch("scripts.setup_coupang_runtime._check_browser", return_value=None),
                patch("scripts.setup_coupang_runtime._check_geoip", return_value=err),
            ):
                result = verify_postcondition()
        self.assertFalse(result)


class CsvFormulaInjectionTest(unittest.TestCase):
    """CSV formula injection defense."""

    def test_csv_safe_escapes_formula_triggers(self):
        self.assertEqual(_csv_safe("=SUM(A1)"), "'=SUM(A1)")
        self.assertEqual(_csv_safe("+cmd"), "'+cmd")
        self.assertEqual(_csv_safe("-cmd"), "'-cmd")
        self.assertEqual(_csv_safe("@import"), "'@import")
        self.assertEqual(_csv_safe("normal"), "normal")
        self.assertEqual(_csv_safe(""), "")
        self.assertEqual(_csv_safe(None), "")

    def test_exporter_escapes_in_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            exporter = CoupangExporter(config)
            records = [{"vendor_id": "V1", "company_name": "=MALICIOUS()",
                        "url": "", "store_name": "", "ceo_name": "",
                        "business_number": "", "phone": "", "email": "",
                        "address": "", "ecommerce_report_number": "",
                        "power_seller": False, "power_seller_title": "",
                        "rating_count": 0, "thumb_up_ratio": 0}]
            _, csv_path = exporter.save(records)
            with open(csv_path, "r", encoding="utf-8-sig") as f:
                content = f.read()
            self.assertIn("'=MALICIOUS()", content)
            self.assertNotIn("\n=MALICIOUS()", content)


class SaveOutcomeRegressionTest(unittest.TestCase):
    """6차 검토: outcome/save_error 계약 회귀 테스트."""

    def test_error_containing_save_text_is_not_save_error(self):
        """General error with '저장 실패' in message → exit 1, not 3."""
        s = CoupangRunSummary()
        s.records = [{"vendor_id": "V1"}]
        s.error = "브라우저 저장 실패 알림 문자열"
        s.termination_reason = "error"
        self.assertEqual(determine_exit_code(s), 1)

    def test_run_error_and_save_error_both_preserved(self):
        """Existing run error + save failure → both fields preserved, exit 3."""
        items = _items(1)
        prods = _products(["VI1"])
        reviews = {"V0": _review()}
        pages = [_promo(items, "tok1"), {"error": "network failure"}]
        page = FakePage(pages,
                        {"status": 200, "body": json.dumps({"code": 200, "data": {"products": prods}})},
                        reviews)
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            crawler = CoupangCrawler(config=config, control=Control(), browser_factory=lambda: FakeBrowser(page))
            with patch.object(CoupangExporter, "_atomic_write_csv", side_effect=ValueError("CSV_CAUSE")):
                s = crawler.run()

            self.assertIn("부분 데이터만 수집됨", s.error)
            self.assertIsNotNone(s.save_error)
            self.assertIn("CSV_CAUSE", s.save_error)
            self.assertIsNotNone(s.json_path)
            self.assertTrue(Path(s.json_path).is_file())
            tmp_files = list(Path(tmp).glob("*.tmp"))
            self.assertEqual(tmp_files, [])
            self.assertEqual(determine_exit_code(s), 3)

    def test_maxminddb_import_error_fails_preflight(self):
        """maxminddb ImportError → PACKAGE_VERSION_MISMATCH."""
        import sys
        fake_camoufox = MagicMock()
        orig_modules = {}
        for key in list(sys.modules.keys()):
            if key == "maxminddb" or key.startswith("maxminddb."):
                orig_modules[key] = sys.modules.pop(key)
        sys.modules["maxminddb"] = None
        try:
            with (
                patch.dict("sys.modules", {"camoufox": fake_camoufox}),
                patch(
                    "app.core.coupang.preflight._get_camoufox_version",
                    return_value=PINNED_CAMOUFOX_VERSION,
                ),
            ):
                result = _check_package_version()
        finally:
            del sys.modules["maxminddb"]
            sys.modules.update(orig_modules)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PreflightStatus.PACKAGE_VERSION_MISMATCH)

    def test_csv_value_error_exit_3_json_preserved(self):
        """CSV ValueError (non-OSError) → exit 3, JSON file on disk preserved."""
        items = _items(1)
        prods = _products(["VI1"])
        reviews = {"V0": _review()}
        page = FakePage([_promo(items, None)],
                        {"status": 200, "body": json.dumps({"code": 200, "data": {"products": prods}})},
                        reviews)
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            crawler = CoupangCrawler(config=config, control=Control(), browser_factory=lambda: FakeBrowser(page))
            with patch.object(CoupangExporter, "_atomic_write_csv", side_effect=ValueError("encode fail")):
                s = crawler.run()

            self.assertIsNotNone(s.json_path)
            self.assertTrue(Path(s.json_path).is_file())
            self.assertIsNone(s.csv_path)
            self.assertIsNotNone(s.save_error)
            self.assertIn("encode fail", s.save_error)
            self.assertEqual(determine_exit_code(s), 3)


# Child program (fresh subprocess): keep the REAL camoufox import graph but
# inject a chosen error for the `maxminddb` import via a MetaPathFinder that
# raises from find_spec. Reproduces the native-load failure that camoufox
# 0.5.4 surfaces (camoufox → async_api → utils → geolocation → import
# maxminddb); geolocation only catches ImportError, so an OSError there
# propagates out of `import camoufox`. argv: <ImportError|OSError> <sentinel>.
_MAXMINDDB_INJECT_CHILD = r'''
import sys, io, json, contextlib, importlib.abc

INJECT = sys.argv[1]
SENTINEL = sys.argv[2]


class _Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name == "maxminddb" or name.startswith("maxminddb."):
            if INJECT == "OSError":
                raise OSError(SENTINEL)
            raise ImportError(SENTINEL)
        return None


for _k in list(sys.modules):
    if (_k == "maxminddb" or _k.startswith("maxminddb.")
            or _k == "camoufox" or _k.startswith("camoufox.")):
        del sys.modules[_k]
sys.meta_path.insert(0, _Blocker())

import app.core.coupang.preflight as pf
# Pin the reported version so the package check reaches the maxminddb gate
# regardless of the actually-installed camoufox version.
pf._get_camoufox_version = lambda: pf.PINNED_CAMOUFOX_VERSION

out = {}

try:
    r = pf._check_package_version()
    out["pkg_status"] = r.status.value if r is not None else None
    out["pkg_msg"] = r.message if r is not None else ""
except BaseException as e:
    out["pkg_exc"] = type(e).__name__

try:
    r2 = pf.check_runtime()
    out["runtime_status"] = r2.status.value if r2 is not None else None
except BaseException as e:
    out["runtime_exc"] = type(e).__name__

try:
    from scripts.setup_coupang_runtime import verify_postcondition
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = verify_postcondition()
    out["verify_ok"] = bool(ok)
    out["verify_stdout"] = buf.getvalue()
except BaseException as e:
    out["verify_exc"] = type(e).__name__

try:
    from scripts.setup_coupang_runtime import main as setup_main
    _old_argv = sys.argv
    sys.argv = ["setup", "--verify-only"]
    buf2 = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf2):
            rc = setup_main()
    finally:
        sys.argv = _old_argv
    out["main_rc"] = rc
    out["main_stdout"] = buf2.getvalue()
except BaseException as e:
    out["main_exc"] = type(e).__name__

sys.stdout.write("RESULT_JSON:" + json.dumps(out) + "\n")
'''


def _camoufox_installed():
    import importlib.util
    try:
        return importlib.util.find_spec("camoufox") is not None
    except (ImportError, OSError, AttributeError, ValueError):
        return False


@unittest.skipUnless(
    _camoufox_installed(),
    "camoufox not installed — native-load regression needs the real import graph",
)
class MaxminddbNativeLoadRegressionTest(unittest.TestCase):
    """HIGH: maxminddb native-load errors must convert to structured fail-closed
    results across preflight, check_runtime, setup verify, and setup main — never
    propagate as an unhandled exception."""

    def _run_child(self, inject: str):
        import subprocess
        project_root = str(Path(__file__).resolve().parents[1])
        proc = subprocess.run(
            [sys.executable, "-c", _MAXMINDDB_INJECT_CHILD, inject, f"NATIVE_{inject}_SENTINEL"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        marker = "RESULT_JSON:"
        line = next(
            (ln for ln in proc.stdout.splitlines() if ln.startswith(marker)),
            None,
        )
        self.assertIsNotNone(
            line,
            f"child produced no RESULT_JSON.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}",
        )
        return json.loads(line[len(marker):])

    def _assert_blocked(self, inject: str):
        sentinel = f"NATIVE_{inject}_SENTINEL"
        out = self._run_child(inject)

        # Nothing may propagate as an unhandled exception (AC-23 fail-closed).
        self.assertNotIn("pkg_exc", out, f"_check_package_version raised: {out.get('pkg_exc')}")
        self.assertNotIn("runtime_exc", out, f"check_runtime raised: {out.get('runtime_exc')}")
        self.assertNotIn("verify_exc", out, f"verify_postcondition raised: {out.get('verify_exc')}")
        self.assertNotIn("main_exc", out, f"setup main raised: {out.get('main_exc')}")

        # Structured PACKAGE_VERSION_MISMATCH with the sentinel surfaced.
        self.assertEqual(out.get("pkg_status"), PreflightStatus.PACKAGE_VERSION_MISMATCH.value)
        self.assertIn(sentinel, out.get("pkg_msg", ""))
        self.assertEqual(out.get("runtime_status"), PreflightStatus.PACKAGE_VERSION_MISMATCH.value)

        # setup verify → False, and the sentinel proves the PACKAGE check drove
        # it (parity isolation: not some incidental browser/GeoIP failure).
        self.assertFalse(out.get("verify_ok"))
        self.assertIn(sentinel, out.get("verify_stdout", ""))

        # setup main(--verify-only) → exit 1, again driven by the package check.
        self.assertEqual(out.get("main_rc"), 1)
        self.assertIn(sentinel, out.get("main_stdout", ""))

    def test_import_error_blocked(self):
        self._assert_blocked("ImportError")

    def test_oserror_blocked(self):
        self._assert_blocked("OSError")


class SecretSentinelInResponseTest(unittest.TestCase):
    """AC-18: request template/cookie content must not leak to logs or exports."""

    def test_template_sentinel_not_in_logs_or_export(self):
        logs = []
        items = _items(1)
        prods = _products(["VI1"])
        reviews = {"V0": _review()}

        page = FakePage([_promo(items, None)],
                        {"status": 200, "body": json.dumps({"code": 200, "data": {"products": prods}})},
                        reviews)

        def goto_with_sentinel(url, **kwargs):
            if "/np/omp" in str(url):
                req = MagicMock()
                req.url = "https://www.coupang.com/np/omp/api/getPromotion"
                req.post_data = json.dumps({
                    "query": {"feedId": "f", "continuationToken": "seemore=CGs="},
                    "SECRET_COOKIE_SENTINEL": "abck_12345",
                    "SECRET_TEMPLATE_SENTINEL": "device_fingerprint_xyz",
                })
                for h in page._handlers:
                    h(req)

        page.goto = goto_with_sentinel

        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            crawler = CoupangCrawler(
                config=config, control=Control(),
                browser_factory=lambda: FakeBrowser(page),
                on_log=lambda msg: logs.append(msg),
            )
            summary = crawler.run()

            all_logs = "\n".join(logs)
            self.assertNotIn("SECRET_COOKIE_SENTINEL", all_logs)
            self.assertNotIn("SECRET_TEMPLATE_SENTINEL", all_logs)
            self.assertNotIn("abck_12345", all_logs)
            self.assertNotIn("device_fingerprint_xyz", all_logs)

            if summary.json_path:
                with open(summary.json_path, "r", encoding="utf-8") as f:
                    exported = f.read()
                self.assertNotIn("SECRET_COOKIE_SENTINEL", exported)
                self.assertNotIn("SECRET_TEMPLATE_SENTINEL", exported)


class ThreadSafetyExtendedTest(unittest.TestCase):
    """AC-06 extended: all crawler operations happen in worker thread, not main."""

    def test_all_ops_in_worker_thread(self):
        """browser_factory + evaluate + exporter all run on same non-main thread."""
        import threading
        from unittest.mock import patch

        items = _items(1)
        prods = _products(["VI1"])
        reviews = {"V0": _review()}
        page = FakePage([_promo(items, None)],
                        {"status": 200, "body": json.dumps({"code": 200, "data": {"products": prods}})},
                        reviews)

        main_tid = threading.current_thread().ident
        factory_threads = set()
        eval_threads = set()
        export_threads = set()

        orig_eval = page.evaluate

        def tracking_eval(script, *args):
            eval_threads.add(threading.current_thread().ident)
            return orig_eval(script, *args)

        page.evaluate = tracking_eval

        def tracking_factory():
            factory_threads.add(threading.current_thread().ident)
            return FakeBrowser(page)

        orig_save = CoupangExporter.save

        def tracking_save(self_exporter, records, partial=False):
            export_threads.add(threading.current_thread().ident)
            return orig_save(self_exporter, records, partial=partial)

        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            result = [None]

            def run():
                c = CoupangCrawler(config=config, control=Control(), browser_factory=tracking_factory)
                with patch.object(CoupangExporter, "save", tracking_save):
                    result[0] = c.run()

            t = threading.Thread(target=run)
            t.start()
            t.join(timeout=10)

        self.assertTrue(factory_threads, "browser_factory was never called")
        self.assertTrue(eval_threads, "evaluate was never called")
        self.assertTrue(export_threads, "exporter.save was never called")

        for tid in factory_threads:
            self.assertNotEqual(tid, main_tid, "browser_factory ran on main thread")
        for tid in eval_threads:
            self.assertNotEqual(tid, main_tid, "evaluate ran on main thread")
        for tid in export_threads:
            self.assertNotEqual(tid, main_tid, "exporter ran on main thread")

        all_threads = factory_threads | eval_threads | export_threads
        self.assertEqual(len(all_threads), 1, "operations ran on multiple threads")


if __name__ == "__main__":
    unittest.main()
