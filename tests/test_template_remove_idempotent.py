#!/usr/bin/env python3
"""REMOVE is idempotent only for a completed HTTP 404 lookup.

Success is HTTP 200 plus a numeric response.id (revoke + delete), or an exact
HTTP 404 from a curl transfer that exited 0. Transport failures, HTTP 5xx,
other statuses, and HTTP 200 without that id stay fatal. ACTIVATE, BLOCK and
PROLONGATE still use the strict lookup.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "shm-remnawave.template.sh"

from tests.proxy_env import DisableEnvProxiesMixin  # noqa: E402


ABSENT_LOG = "User us_71 already absent; REMOVE is already satisfied"
USER_PATH = "/api/users/by-username/us_71"
REVOKE_PATH = "/api/users/77/actions/revoke"
DELETE_PATH = "/api/users/77"
SHM_KEY_PATH = "/shm/v1/storage/manage/vpn_mrzb_71"


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: List[Dict[str, str]] = []
        self.user_status = 200
        self.user_body = b'{"response":{"id":77}}'

    def reset(self, *, status: int = 200, body: bytes = b'{"response":{"id":77}}') -> None:
        with self.lock:
            self.calls.clear()
            self.user_status = status
            self.user_body = body

    def snapshot(self) -> Tuple[int, bytes]:
        with self.lock:
            return self.user_status, self.user_body


STATE = State()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    def _record(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        if length:
            self.rfile.read(length)
        parsed = urllib.parse.urlparse(self.path)
        with STATE.lock:
            STATE.calls.append({"method": self.command, "path": parsed.path})

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._record()
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/users/by-username/"):
            status, body = STATE.snapshot()
            self._reply(status, body)
            return
        self._reply(500, b'{"message":"unexpected get"}')

    def do_POST(self) -> None:  # noqa: N802
        self._record()
        self._reply(200, b'{"response":{"ok":true}}')

    def do_DELETE(self) -> None:  # noqa: N802
        self._record()
        self._reply(200, b'{"response":{"ok":true}}')

    def do_PATCH(self) -> None:  # noqa: N802
        self._record()
        self._reply(200, b'{"response":{"ok":true}}')


def _render(event: str, panel_url: str, api_url: str) -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    replacements = {
        "{{ event_name }}": event,
        "{{ user.gen_session.id }}": "test-session",
        "{{ config.api.url }}": api_url,
        "{{ server.settings.remnawave.api }}": panel_url,
        "{{ server.settings.remnawave.token }}": "test-token",
        "{{ server.settings.remnawave.default_internal_squad_name }}": "Default-Squad",
        "{{ us.service.settings.remnawave.internal_squad_name }}": "",
        "{{ toJson(us.service.settings.remnawave.internal_squad_pool_names) }}": "",
        "{{ us.service.settings.remnawave.external_squad_name }}": "",
        "{{ us.service.settings.remnawave.traffic_limit_bytes }}": "",
        "{{ us.service.settings.remnawave.traffic_limit_strategy }}": "",
        "{{ us.service.settings.remnawave.hwid_device_limit }}": "",
        "{{ server.settings.remnawave.shm_tz }}": "UTC",
        "{{ server.settings.remnawave.expire_safety_minutes }}": "0",
        "{{ us.id }}": "71",
        "{{ server.settings.remnawave.sanitize_username }}": "false",
        "{{ us.expire }}": "2026-12-31 23:59:59",
        "{{ toJson(user.login) }}": json.dumps("alice"),
        "{{ toJson(user.full_name) }}": json.dumps("Alice Example"),
        "{{ toJson(user.settings.telegram.login) }}": json.dumps("alice_tg"),
    }
    for needle, value in replacements.items():
        if needle not in text:
            raise AssertionError(f"placeholder missing from template: {needle}")
        text = text.replace(needle, value)
    leftover = re.findall(r"\{\{[^}]+\}\}", text)
    if leftover:
        raise AssertionError(f"unreplaced placeholders: {leftover}")
    return text


def _closed_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _calls() -> List[Tuple[str, str]]:
    with STATE.lock:
        return [(item["method"], item["path"]) for item in STATE.calls]


class RemoveIdempotencyTests(DisableEnvProxiesMixin, unittest.TestCase):
    httpd: ThreadingHTTPServer
    port: int

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = int(cls.httpd.server_address[1])
        thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        super().tearDownClass()

    def setUp(self) -> None:
        STATE.reset()

    def _run(
        self,
        event: str,
        *,
        panel_url: Optional[str] = None,
        api_url: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
        path_prefix: Optional[str] = None,
    ) -> Dict[str, Any]:
        base = f"http://127.0.0.1:{self.port}"
        rendered = _render(event, panel_url or base, api_url or base)
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "rendered.sh")
            Path(script).write_text(rendered, encoding="utf-8")
            env = os.environ.copy()
            if extra_env:
                env.update(extra_env)
            if path_prefix:
                env["PATH"] = path_prefix + os.pathsep + env.get("PATH", "")
            proc = subprocess.run(
                ["bash", script],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                timeout=20,
            )
        return {
            "code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "calls": _calls(),
        }

    def test_remove_existing_user_revokes_and_deletes(self) -> None:
        result = self._run("REMOVE")
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["stderr"], "")
        self.assertIn("done", result["stdout"])
        self.assertNotIn(ABSENT_LOG, result["stdout"])
        self.assertEqual(
            result["calls"],
            [
                ("GET", USER_PATH),
                ("POST", REVOKE_PATH),
                ("DELETE", DELETE_PATH),
                ("DELETE", SHM_KEY_PATH),
            ],
        )

    def test_remove_http_404_is_already_satisfied(self) -> None:
        # Body includes a numeric id on purpose: absence is the HTTP status,
        # not a phrase or a missing field in the payload.
        STATE.reset(
            status=404,
            body=b'{"message":"User not found","statusCode":404,"response":{"id":77}}',
        )
        result = self._run("REMOVE")
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["stderr"], "")
        self.assertIn(ABSENT_LOG, result["stdout"])
        self.assertIn("done", result["stdout"])
        self.assertEqual(
            result["calls"],
            [
                ("GET", USER_PATH),
                ("DELETE", SHM_KEY_PATH),
            ],
        )

    def test_remove_http_error_statuses_fail(self) -> None:
        body = b'{"message":"User not found","response":{"id":77}}'
        for status in (500, 503, 401, 204):
            with self.subTest(status=status):
                STATE.reset(status=status, body=body)
                result = self._run("REMOVE")
                self.assertNotEqual(result["code"], 0)
                self.assertIn(f"HTTP {status}", result["stderr"])
                self.assertNotIn(ABSENT_LOG, result["stdout"])
                self.assertNotIn("done", result["stdout"])
                self.assertEqual(result["calls"], [("GET", USER_PATH)])

    def test_remove_network_error_fails(self) -> None:
        port = _closed_port()
        dead = f"http://127.0.0.1:{port}"
        result = self._run("REMOVE", panel_url=dead, api_url=dead)
        self.assertNotEqual(result["code"], 0)
        self.assertRegex(result["stderr"], r"curl exit [1-9][0-9]*")
        self.assertNotIn(ABSENT_LOG, result["stdout"])
        self.assertNotIn("done", result["stdout"])
        self.assertEqual(result["calls"], [])

    def test_remove_curl_exit_is_fatal_even_if_it_prints_404(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            curl_path = Path(tmp) / "curl"
            args_log = Path(tmp) / "curl-args.log"
            curl_path.write_text(
                "#!/bin/bash\n"
                "printf '%s\\n' \"$*\" >> \"${CURL_ARGS_LOG}\"\n"
                "printf '%s' \"${FAKE_CURL_STDOUT}\"\n"
                "exit \"${FAKE_CURL_EXIT}\"\n",
                encoding="utf-8",
            )
            curl_path.chmod(0o755)
            result = self._run(
                "REMOVE",
                path_prefix=tmp,
                extra_env={
                    "CURL_ARGS_LOG": str(args_log),
                    "FAKE_CURL_STDOUT": "404",
                    "FAKE_CURL_EXIT": "7",
                },
            )
            recorded = args_log.read_text(encoding="utf-8")
        self.assertNotEqual(result["code"], 0)
        self.assertIn("curl exit 7", result["stderr"])
        self.assertNotIn(ABSENT_LOG, result["stdout"])
        self.assertNotIn("done", result["stdout"])
        self.assertIn("%{http_code}", recorded)
        self.assertIn(USER_PATH, recorded)
        self.assertEqual(result["calls"], [])

    def test_remove_http_000_is_not_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            curl_path = Path(tmp) / "curl"
            curl_path.write_text(
                "#!/bin/bash\n"
                "printf '%s' \"${FAKE_CURL_STDOUT}\"\n"
                "exit \"${FAKE_CURL_EXIT}\"\n",
                encoding="utf-8",
            )
            curl_path.chmod(0o755)
            result = self._run(
                "REMOVE",
                path_prefix=tmp,
                extra_env={
                    "FAKE_CURL_STDOUT": "000",
                    "FAKE_CURL_EXIT": "0",
                },
            )
        self.assertNotEqual(result["code"], 0)
        self.assertIn("HTTP 000", result["stderr"])
        self.assertNotIn(ABSENT_LOG, result["stdout"])
        self.assertEqual(result["calls"], [])

    def test_remove_http_200_malformed_json_fails(self) -> None:
        STATE.reset(status=200, body=b'{"response":')
        result = self._run("REMOVE")
        self.assertNotEqual(result["code"], 0)
        self.assertIn("malformed JSON", result["stderr"])
        self.assertNotIn(ABSENT_LOG, result["stdout"])
        self.assertNotIn("done", result["stdout"])
        self.assertEqual(result["calls"], [("GET", USER_PATH)])

    def test_remove_http_200_without_user_id_fails(self) -> None:
        bodies = (
            b'{"message":"User not found"}',
            b'{"response":{"id":null}}',
            b'{"response":{}}',
            b'{"id":77}',
        )
        for body in bodies:
            with self.subTest(body=body):
                STATE.reset(status=200, body=body)
                result = self._run("REMOVE")
                self.assertNotEqual(result["code"], 0)
                self.assertIn("without a valid user id", result["stderr"])
                self.assertNotIn(ABSENT_LOG, result["stdout"])
                self.assertNotIn("done", result["stdout"])
                self.assertEqual(result["calls"], [("GET", USER_PATH)])

    def test_other_lifecycle_events_still_fail_when_user_is_absent(self) -> None:
        STATE.reset(
            status=404,
            body=b'{"message":"User not found","statusCode":404}',
        )
        for event in ("ACTIVATE", "BLOCK", "PROLONGATE"):
            with self.subTest(event=event):
                STATE.reset(
                    status=404,
                    body=b'{"message":"User not found","statusCode":404}',
                )
                result = self._run(event)
                self.assertNotEqual(result["code"], 0)
                self.assertIn("User not found: us_71", result["stderr"])
                self.assertNotIn(ABSENT_LOG, result["stdout"])
                self.assertNotIn("done", result["stdout"])
                self.assertEqual(result["calls"], [("GET", USER_PATH)])

    def test_strict_lookup_stays_on_non_remove_events(self) -> None:
        text = TEMPLATE.read_text(encoding="utf-8")
        remove = text.split("REMOVE)", 1)[1].split("PROLONGATE)", 1)[0]
        self.assertIn("_lookup_user_id_for_remove", remove)
        self.assertNotIn("_user_id_by_username", remove)
        for event, end in (
            ("ACTIVATE)", "BLOCK)"),
            ("BLOCK)", "REMOVE)"),
            ("PROLONGATE)", "UPDATE)"),
        ):
            branch = text.split(event, 1)[1].split(end, 1)[0]
            self.assertIn('user_id="$(_user_id_by_username "${USERNAME_SANITIZED}")"', branch)
            self.assertNotIn("_lookup_user_id_for_remove", branch)
        definition = text.split("_user_id_by_username()", 1)[1].split(
            "_lookup_user_id_for_remove()", 1
        )[0]
        self.assertIn('fail "User not found: ${username}"', definition)


if __name__ == "__main__":
    unittest.main()
