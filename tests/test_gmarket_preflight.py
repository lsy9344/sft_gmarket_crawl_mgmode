"""Gmarket Patchright 런타임 preflight 회귀 테스트."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.gmarket_preflight import (
    PINNED_PATCHRIGHT_VERSION,
    check_gmarket_runtime,
    chromium_installed,
)


class GmarketPreflightTest(unittest.TestCase):
    def test_exact_revision_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrong = root / "chromium-1" / "chrome-linux64"
            wrong.mkdir(parents=True)
            (wrong / "chrome").write_bytes(b"fake")
            self.assertFalse(chromium_installed(root, "1228"))

            expected = root / "chromium-1228" / "chrome-linux64"
            expected.mkdir(parents=True)
            (expected / "chrome").write_bytes(b"fake")
            with patch("app.core.gmarket_preflight.sys.platform", "linux"), patch(
                "app.core.gmarket_preflight.platform.machine", return_value="x86_64"
            ):
                self.assertTrue(chromium_installed(root, "1228"))

    def test_missing_package_is_actionable(self) -> None:
        from importlib import metadata

        with patch(
            "app.core.gmarket_preflight.metadata.version",
            side_effect=metadata.PackageNotFoundError,
        ):
            result = check_gmarket_runtime()
        self.assertFalse(result.ok)
        self.assertIn("SellerCollector.exe --setup-runtime", result.message)

    def test_damaged_package_metadata_is_actionable(self) -> None:
        for error in (OSError("metadata unreadable"), ValueError("invalid metadata")):
            with self.subTest(error=type(error).__name__), patch(
                "app.core.gmarket_preflight.metadata.version",
                side_effect=error,
            ):
                result = check_gmarket_runtime()
                self.assertFalse(result.ok)
                self.assertIn("메타데이터", result.message)
                self.assertIn("SellerCollector.exe --setup-runtime", result.message)

    def test_wrong_package_version_rejected(self) -> None:
        with patch("app.core.gmarket_preflight.metadata.version", return_value="0.0.1"):
            result = check_gmarket_runtime()
        self.assertFalse(result.ok)
        self.assertIn(PINNED_PATCHRIGHT_VERSION, result.message)

    def test_ready_runtime_passes(self) -> None:
        with (
            patch(
                "app.core.gmarket_preflight.metadata.version",
                return_value=PINNED_PATCHRIGHT_VERSION,
            ),
            patch("app.core.gmarket_preflight.expected_chromium_revision", return_value="1228"),
            patch("app.core.gmarket_preflight.browsers_dir", return_value=Path("/runtime")),
            patch("app.core.gmarket_preflight.chromium_installed", return_value=True),
        ):
            result = check_gmarket_runtime()
        self.assertTrue(result.ok)


if __name__ == "__main__":
    unittest.main()
