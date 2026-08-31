#!/usr/bin/env python3
"""Tests for scoped proxy environment helper."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.proxy_env import (  # noqa: E402
    PROXY_ENV_KEYS,
    disable_env_proxies,
    snapshot_proxy_env,
)


class DisableEnvProxiesTests(unittest.TestCase):
    def test_restores_present_and_absent_keys(self) -> None:
        marker = "test-proxy-restore-marker-do-not-use"
        saved_before = snapshot_proxy_env()

        def _restore_original() -> None:
            for key in PROXY_ENV_KEYS:
                os.environ.pop(key, None)
            for key, value in saved_before.items():
                if value is not None:
                    os.environ[key] = value

        self.addCleanup(_restore_original)
        os.environ["HTTP_PROXY"] = marker
        os.environ.pop("https_proxy", None)

        with disable_env_proxies() as guard:
            during = snapshot_proxy_env()
            self.assertTrue(all(value is None for value in during.values()))
            self.assertNotIn("HTTP_PROXY", os.environ)
            self.assertNotEqual(os.environ.get("NO_PROXY"), "*")

        guard.assert_restored()
        self.assertEqual(os.environ.get("HTTP_PROXY"), marker)
        self.assertNotIn("https_proxy", os.environ)
        _restore_original()
        self.assertEqual(snapshot_proxy_env(), saved_before)

    def test_does_not_set_no_proxy_star(self) -> None:
        with disable_env_proxies():
            self.assertIsNone(os.environ.get("NO_PROXY"))
            self.assertIsNone(os.environ.get("no_proxy"))


if __name__ == "__main__":
    unittest.main()
