#!/usr/bin/env python3
"""TEST-only local Remnawave API headers and on-host token fallback."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "templates/shm-remnawave.template.sh").read_text()
PREFIX = SOURCE.split("\n# Helpers\n", 1)[0]
LOCAL_API = "http://127.0.0.1:3000"


def invoke(prefix, home):
    # No network: a Bash curl function echoes its argv into a private test pipe.
    command = prefix + "\ncurl() { printf '<%s>\\n' \"$@\"; }\n_http_get /api/users\n"
    env = os.environ.copy()
    env["HOME"] = str(home)
    return subprocess.run(
        ["bash", "-c", command], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=9,
    )


def render(url, token):
    value = PREFIX.replace(
        'PANEL_URL="{{ server.settings.remnawave.api }}"',
        'PANEL_URL="' + url + '"',
    ).replace(
        'REMNAWAVE_API_TOKEN="{{ server.settings.remnawave.token }}"',
        'REMNAWAVE_API_TOKEN="' + token + '"',
    )
    return value


class TestLocalApi(unittest.TestCase):
    def test_prod_url_does_not_use_local_headers_or_file(self):
        with tempfile.TemporaryDirectory() as d:
            result = invoke(render("https://panel.example.invalid", "prod-dummy"), Path(d))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Authorization: Bearer prod-dummy", result.stdout)
            self.assertNotIn("X-Forwarded-Proto", result.stdout)
            self.assertNotIn("X-Forwarded-For", result.stdout)

    def test_loopback_api_has_two_forwarded_headers(self):
        with tempfile.TemporaryDirectory() as d:
            result = invoke(render(LOCAL_API, "test-dummy"), Path(d))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("X-Forwarded-Proto: https", result.stdout)
            self.assertIn("X-Forwarded-For: 127.0.0.1", result.stdout)
            self.assertIn(LOCAL_API + "/api/users", result.stdout)

    def test_loopback_missing_token_reads_local_owner_only_file(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            token = home / ".config/remnawave/lifecycle.token"
            token.parent.mkdir(parents=True, mode=0o700)
            token.write_text("dummy-test-token")
            token.chmod(0o600)
            result = invoke(render(LOCAL_API, "null"), home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Authorization: Bearer dummy-test-token", result.stdout)
            self.assertIn("X-Forwarded-Proto: https", result.stdout)

    def test_missing_or_world_readable_local_token_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            source = render(LOCAL_API, "null")
            missing = invoke(source, home)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("credential file not accessible", missing.stderr)
            target = home / ".config/remnawave/lifecycle.token"
            target.parent.mkdir(parents=True, mode=0o700)
            target.write_text("dummy-test-token")
            target.chmod(0o644)
            readable = invoke(source, home)
            self.assertNotEqual(readable.returncode, 0)
            self.assertIn("owner-only 0600", readable.stderr)

    def test_panel_direct_curl_paths_receive_same_headers(self):
        direct = [
            l for l in SOURCE.splitlines()
            if '-H "$(_auth_header)" "DOLLAR{REMNAWAVE_LOCAL_API_HEADERS[@]}"' in
            l.replace(chr(36) + "{", "DOLLAR{")
        ]
        self.assertEqual(len(direct), 8)  # 4 shared helpers + 4 direct Remnawave calls
        self.assertEqual(sum(l.startswith("      -H ") for l in direct), 4)


if __name__ == "__main__":
    unittest.main()
