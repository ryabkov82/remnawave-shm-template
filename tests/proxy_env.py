"""Scoped disable of HTTP(S) proxy environment variables for localhost mocks.

urllib does not honor wildcard ``no_proxy`` entries such as ``127.*``.
Tests that speak to 127.0.0.1 must unset proxy vars for the duration of
the HTTP fixture only, then restore the exact previous environment.
"""

from __future__ import annotations

import os
from typing import Dict, Optional


PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)


def snapshot_proxy_env() -> Dict[str, Optional[str]]:
    """Return each proxy key mapped to its value, or None if unset."""
    return {key: os.environ.get(key) for key in PROXY_ENV_KEYS}


class disable_env_proxies:
    """Context manager: unset proxy vars, then restore the prior state.

    Does not set ``NO_PROXY=*``. After teardown, keys that were absent
    stay absent; keys that were present get their original values back.
    """

    def __init__(self) -> None:
        self.saved: Dict[str, str] = {}

    def __enter__(self) -> "disable_env_proxies":
        self.saved = {
            key: os.environ[key] for key in PROXY_ENV_KEYS if key in os.environ
        }
        for key in PROXY_ENV_KEYS:
            os.environ.pop(key, None)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        for key in PROXY_ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update(self.saved)

    def assert_restored(self) -> None:
        current = snapshot_proxy_env()
        expected: Dict[str, Optional[str]] = {key: None for key in PROXY_ENV_KEYS}
        expected.update(self.saved)
        if current != expected:
            raise AssertionError(
                "proxy environment not restored: "
                f"got {current!r}, expected {expected!r}"
            )


class DisableEnvProxiesMixin:
    """setUpClass/tearDownClass mixin for test classes that mock HTTP on localhost."""

    _proxy_guard: disable_env_proxies

    @classmethod
    def setUpClass(cls) -> None:
        cls._proxy_guard = disable_env_proxies()
        cls._proxy_guard.__enter__()
        try:
            super().setUpClass()
        except Exception:
            cls._proxy_guard.__exit__(None, None, None)
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            super().tearDownClass()
        finally:
            cls._proxy_guard.__exit__(None, None, None)
            cls._proxy_guard.assert_restored()
