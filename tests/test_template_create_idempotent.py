#!/usr/bin/env python3
"""CREATE survives a partial run and does not mint a second Remnawave user.

SHM runs this template as one SSH command. Spool retries FAIL with no retry
cap. If the remote shell dies after POST /api/users and before the storage
PUT, the next run used to call POST again, get A019, and exit 22 from
curl --fail-with-body. The task then stayed FAIL forever.

A repeated CREATE may write SHM storage only after it has proved the panel
user is us_<user_service_id> with a positive numeric id. HTTP 400 A019 is
not that proof. The existing UUID, subscription and access fields stay as
they are. SSH timeout before SHM #137 can still be reported as SUCCESS and
is not retried by this script.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
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


USERNAME = "us_6819"
US_ID = "6819"
USER_PATH = f"/api/users/by-username/{USERNAME}"
SUB_PATH = f"/api/subscriptions/by-username/{USERNAME}"
SQUADS_PATH = "/api/internal-squads"
CREATE_PATH = "/api/users"
STORAGE_PATH = f"/shm/v1/storage/manage/vpn_mrzb_{US_ID}"
SQUAD_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
STABLE_SQUAD = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
FRESH_UUID = "33333333-3333-4333-8333-333333333333"
ALREADY = (
    "User us_6819 already exists (id=6819); "
    "leaving UUID, subscription and access unchanged"
)
A019_MESSAGE = "User username already exists"


def _stable_user() -> Dict[str, Any]:
    return {
        "id": 6819,
        "uuid": "11111111-1111-4111-8111-111111111111",
        "shortUuid": "stable-short",
        "subscriptionUuid": "22222222-2222-4222-8222-222222222222",
        "username": USERNAME,
        "status": "DISABLED",
        "expireAt": "2031-05-01T00:00:00.000Z",
        "trafficLimitBytes": 12345,
        "trafficLimitStrategy": "MONTH",
        "hwidDeviceLimit": 4,
        "activeInternalSquads": [STABLE_SQUAD],
        "externalSquadUuid": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    }


def _a019_body() -> Dict[str, Any]:
    return {
        "timestamp": "2026-11-04T00:00:00.000Z",
        "path": "/api/users",
        "message": A019_MESSAGE,
        "errorCode": "A019",
    }


def _created_user(payload: Dict[str, Any]) -> Dict[str, Any]:
    user: Dict[str, Any] = {
        "id": 50,
        "uuid": FRESH_UUID,
        "shortUuid": "fresh-short",
        "subscriptionUuid": "44444444-4444-4444-8444-444444444444",
        "username": payload.get("username"),
        "status": payload.get("status"),
        "expireAt": payload.get("expireAt"),
        "trafficLimitBytes": payload.get("trafficLimitBytes"),
        "trafficLimitStrategy": payload.get("trafficLimitStrategy"),
        "activeInternalSquads": payload.get("activeInternalSquads"),
        "externalSquadUuid": payload.get("externalSquadUuid"),
    }
    if "hwidDeviceLimit" in payload:
        user["hwidDeviceLimit"] = payload["hwidDeviceLimit"]
    return user


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.users: Dict[str, Dict[str, Any]] = {}
        self.calls: List[Tuple[str, str]] = []
        self.payloads: List[Any] = []
        self.storage: Dict[str, Any] = {}
        self.post_mode = "create"
        self.lookup_mode = "store"
        self.lookup_status = 200
        self.miss_lookups = 0
        self.subscription_status = 200
        self.subscription_username = None
        self.subscription_short = None
        self.shm_status = 200

    def reset(self) -> None:
        with self.lock:
            self.users.clear()
            self.calls.clear()
            self.payloads.clear()
            self.storage.clear()
            self.post_mode = "create"
            self.lookup_mode = "store"
            self.lookup_status = 200
            self.miss_lookups = 0
            self.subscription_status = 200
            self.subscription_username = None
            self.subscription_short = None
            self.shm_status = 200

    def seed(self, user: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            self.users[str(user["username"])] = copy.deepcopy(user)
        return copy.deepcopy(user)

    def snapshot_users(self) -> Dict[str, Dict[str, Any]]:
        with self.lock:
            return copy.deepcopy(self.users)

    def snapshot_calls(self) -> List[Tuple[str, str]]:
        with self.lock:
            return list(self.calls)

    def snapshot_storage(self) -> Dict[str, Any]:
        with self.lock:
            return copy.deepcopy(self.storage)

    def snapshot_payloads(self) -> List[Any]:
        with self.lock:
            return copy.deepcopy(self.payloads)

    def note(self, method: str, path: str) -> None:
        self.calls.append((method, path))


STATE = State()


def _dumps(body: Dict[str, Any]) -> bytes:
    return json.dumps(body).encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or "0")
        if length:
            return self.rfile.read(length)
        return b""

    def _reply(self, status: int, body: bytes, *, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _lookup(self) -> Tuple[int, bytes]:
        with STATE.lock:
            if STATE.miss_lookups > 0:
                STATE.miss_lookups -= 1
                # A numeric id in a 404 body must not count as the user.
                return 404, _dumps(
                    {
                        "message": "User with specified params not found",
                        "errorCode": "A063",
                        "response": {"id": 6819, "username": USERNAME},
                    }
                )
            mode = STATE.lookup_mode
            status = STATE.lookup_status
            user = copy.deepcopy(STATE.users.get(USERNAME))
        if mode == "malformed":
            return 200, b'{"response":'
        if mode == "status":
            return status, _dumps({"message": "lookup failed", "errorCode": "A026"})
        if mode == "mismatch":
            body = _stable_user()
            body["username"] = "us_other"
            return 200, _dumps({"response": body})
        if mode == "invalid_id":
            body = _stable_user()
            body["id"] = None
            return 200, _dumps({"response": body})
        if mode == "string_id":
            body = _stable_user()
            body["id"] = "6819"
            return 200, _dumps({"response": body})
        if mode == "missing_username":
            body = _stable_user()
            body.pop("username", None)
            return 200, _dumps({"response": body})
        if mode == "always_404":
            return 404, _dumps(
                {
                    "message": "User with specified params not found",
                    "errorCode": "A063",
                    "response": _stable_user(),
                }
            )
        if mode == "a025_404":
            return 404, _dumps({"message": "User not found", "errorCode": "A025"})
        if mode == "html_404":
            return 404, b"<html>not found</html>"
        if mode == "a063_wrong_message":
            return 404, _dumps(
                {"message": "User not found", "errorCode": "A063"}
            )
        if user is None:
            return 404, _dumps(
                {
                    "message": "User with specified params not found",
                    "errorCode": "A063",
                }
            )
        return 200, _dumps({"response": user})

    def _subscription(self) -> Tuple[int, bytes]:
        with STATE.lock:
            status = STATE.subscription_status
            user = copy.deepcopy(STATE.users.get(USERNAME))
            username_override = STATE.subscription_username
            short_override = STATE.subscription_short
        if status != 200:
            return status, _dumps({"message": "subscription unavailable", "errorCode": "A063"})
        if user is None:
            return 404, _dumps(
                {
                    "message": "User with specified params not found",
                    "errorCode": "A063",
                }
            )
        username = username_override or user["username"]
        short_uuid = short_override or user["shortUuid"]
        return 200, _dumps(
            {
                "response": {
                    "subscriptionUrl": f"https://sub.test/{short_uuid}",
                    "user": {
                        "username": username,
                        "id": user["id"],
                        "uuid": user["uuid"],
                        "shortUuid": short_uuid,
                    },
                }
            }
        )

    def _insert_stable(self) -> None:
        STATE.users.setdefault(USERNAME, _stable_user())

    def _create(self, payload: Any, variant: str) -> Tuple[int, bytes]:
        with STATE.lock:
            if not isinstance(payload, dict):
                return 400, _dumps({"message": "bad payload", "errorCode": "E000"})
            existing = STATE.users.get(USERNAME)
            if existing is not None:
                return 400, _dumps(_a019_body())
            user = _created_user(payload)
            STATE.users[USERNAME] = user
            stored = copy.deepcopy(user)
        if variant == "id_only":
            return 201, _dumps({"response": {"id": stored["id"]}})
        if variant == "no_id":
            return 201, _dumps({"response": {}})
        if variant == "wrong_username":
            lied = dict(stored)
            lied["username"] = "us_other"
            return 201, _dumps({"response": lied})
        if variant == "malformed":
            return 201, b'{"response":'
        return 201, _dumps({"response": stored})

    def _post_outcome(self, payload: Any) -> Tuple[int, bytes]:
        with STATE.lock:
            mode = STATE.post_mode
        if mode == "a019":
            return 400, _dumps(_a019_body())
        if mode == "a019_insert":
            with STATE.lock:
                self._insert_stable()
            return 400, _dumps(_a019_body())
        if mode == "a020_insert":
            with STATE.lock:
                self._insert_stable()
            return 400, _dumps(
                {
                    "message": "User short UUID already exists",
                    "errorCode": "A020",
                    "path": "/api/users",
                }
            )
        if mode == "validation_insert":
            with STATE.lock:
                self._insert_stable()
            return 400, _dumps(
                {
                    "statusCode": 400,
                    "message": "User username already exists A019",
                    "errorCode": "E000",
                    "errors": [{"message": "A019"}],
                }
            )
        if mode == "wrong_message_insert":
            with STATE.lock:
                self._insert_stable()
            body = _a019_body()
            body["message"] = "User username already exists."
            return 400, _dumps(body)
        if mode == "message_only_insert":
            with STATE.lock:
                self._insert_stable()
            return 400, _dumps({"message": A019_MESSAGE, "path": "/api/users"})
        if mode == "code_only_insert":
            with STATE.lock:
                self._insert_stable()
            return 400, _dumps({"errorCode": "A019", "path": "/api/users"})
        if mode == "nested_a019_insert":
            with STATE.lock:
                self._insert_stable()
            return 400, _dumps(
                {
                    "message": "Validation failed",
                    "errorCode": "E000",
                    "errors": [{"errorCode": "A019", "message": A019_MESSAGE}],
                }
            )
        if mode == "http_500_a019_insert":
            with STATE.lock:
                self._insert_stable()
            return 500, _dumps(_a019_body())
        if mode == "create_id_only":
            return self._create(payload, "id_only")
        if mode == "create_no_id":
            return self._create(payload, "no_id")
        if mode == "create_wrong_username":
            return self._create(payload, "wrong_username")
        if mode == "create_malformed":
            return self._create(payload, "malformed")
        return self._create(payload, "full")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        with STATE.lock:
            STATE.note("GET", parsed.path)
        if parsed.path == SQUADS_PATH:
            self._reply(
                200,
                _dumps(
                    {
                        "response": {
                            "internalSquads": [
                                {
                                    "name": "Default-Squad",
                                    "uuid": SQUAD_UUID,
                                    "info": {"membersCount": 3},
                                }
                            ]
                        }
                    }
                ),
            )
            return
        if parsed.path == USER_PATH:
            status, body = self._lookup()
            self._reply(status, body)
            return
        if parsed.path == SUB_PATH:
            status, body = self._subscription()
            self._reply(status, body)
            return
        self._reply(500, _dumps({"message": "unexpected get", "path": parsed.path}))

    def do_POST(self) -> None:  # noqa: N802
        raw = self._read_body()
        parsed = urllib.parse.urlparse(self.path)
        payload: Any
        try:
            payload = json.loads(raw.decode() or "null")
        except json.JSONDecodeError:
            payload = None
        with STATE.lock:
            STATE.note("POST", parsed.path)
            if parsed.path == CREATE_PATH:
                STATE.payloads.append(payload)
            drop = STATE.post_mode in {"drop", "create_then_drop"}
            create_then_drop = STATE.post_mode == "create_then_drop"
        if parsed.path != CREATE_PATH:
            self._reply(500, _dumps({"message": "unexpected post"}))
            return
        if create_then_drop and isinstance(payload, dict):
            with STATE.lock:
                if USERNAME not in STATE.users:
                    STATE.users[USERNAME] = _created_user(payload)
        if drop:
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        status, body = self._post_outcome(payload)
        self._reply(status, body)

    def do_PUT(self) -> None:  # noqa: N802
        raw = self._read_body()
        parsed = urllib.parse.urlparse(self.path)
        with STATE.lock:
            STATE.note("PUT", parsed.path)
            shm_status = STATE.shm_status
        if parsed.path != STORAGE_PATH:
            self._reply(500, _dumps({"message": "unexpected put"}))
            return
        if shm_status != 200:
            self._reply(shm_status, _dumps({"message": "storage rejected"}))
            return
        with STATE.lock:
            STATE.storage[parsed.path] = json.loads(raw.decode())
        self._reply(200, _dumps({"ok": True}))

    def do_PATCH(self) -> None:  # noqa: N802
        self._read_body()
        parsed = urllib.parse.urlparse(self.path)
        with STATE.lock:
            STATE.note("PATCH", parsed.path)
        self._reply(500, _dumps({"message": "unexpected patch"}))

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        with STATE.lock:
            STATE.note("DELETE", parsed.path)
        self._reply(500, _dumps({"message": "unexpected delete"}))


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
        "{{ us.id }}": US_ID,
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


def _expected_subscription(user: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "subscriptionUrl": f"https://sub.test/{user['shortUuid']}",
        "subscription_url": f"https://sub.test/{user['shortUuid']}",
        "user": {
            "username": user["username"],
            "id": user["id"],
            "uuid": user["uuid"],
            "shortUuid": user["shortUuid"],
        },
    }


class CreateIdempotencyTests(DisableEnvProxiesMixin, unittest.TestCase):
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
        event: str = "CREATE",
        *,
        panel_url: Optional[str] = None,
        api_url: Optional[str] = None,
        path_prefix: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
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
            "calls": STATE.snapshot_calls(),
            "users": STATE.snapshot_users(),
            "storage": STATE.snapshot_storage(),
            "payloads": STATE.snapshot_payloads(),
        }

    def _posts(self, result: Dict[str, Any]) -> List[Tuple[str, str]]:
        return [call for call in result["calls"] if call == ("POST", CREATE_PATH)]

    def _mutations(self, result: Dict[str, Any]) -> List[Tuple[str, str]]:
        return [
            call
            for call in result["calls"]
            if call[0] in {"PATCH", "DELETE"}
            or (call[0] == "POST" and call[1] != CREATE_PATH)
        ]

    def test_fresh_create_posts_once_and_stores_subscription(self) -> None:
        result = self._run()
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["stderr"], "")
        self.assertIn("Created user us_6819 (id=50)", result["stdout"])
        self.assertNotIn("already exists", result["stdout"])
        self.assertIn("done", result["stdout"])
        self.assertEqual(self._posts(result), [("POST", CREATE_PATH)])
        self.assertEqual(self._mutations(result), [])
        self.assertEqual(
            [call for call in result["calls"] if call == ("GET", SQUADS_PATH)],
            [("GET", SQUADS_PATH)],
        )
        payload = result["payloads"][0]
        self.assertEqual(payload["username"], USERNAME)
        self.assertEqual(payload["status"], "ACTIVE")
        self.assertEqual(payload["expireAt"], "2026-12-31T23:59:59Z")
        self.assertEqual(payload["activeInternalSquads"], [SQUAD_UUID])
        user = result["users"][USERNAME]
        self.assertEqual(user["uuid"], FRESH_UUID)
        self.assertEqual(user["expireAt"], payload["expireAt"])
        self.assertEqual(user["activeInternalSquads"], [SQUAD_UUID])
        self.assertEqual(result["storage"][STORAGE_PATH], _expected_subscription(user))

    def test_rerun_after_create_does_not_touch_the_panel_user(self) -> None:
        first = self._run()
        self.assertEqual(first["code"], 0, first["stderr"])
        created = copy.deepcopy(first["users"])
        stored = copy.deepcopy(first["storage"])
        STATE.calls.clear()
        STATE.payloads.clear()

        second = self._run()
        self.assertEqual(second["code"], 0, second["stderr"])
        self.assertEqual(second["stderr"], "")
        self.assertIn("already exists (id=50)", second["stdout"])
        self.assertNotIn("Created user", second["stdout"])
        self.assertEqual(self._posts(second), [])
        self.assertEqual(self._mutations(second), [])
        self.assertNotIn(("GET", SQUADS_PATH), second["calls"])
        self.assertEqual(second["users"], created)
        self.assertEqual(second["storage"], stored)

    def test_existing_user_without_storage_is_saved_unchanged(self) -> None:
        before = STATE.seed(_stable_user())
        result = self._run()
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["stderr"], "")
        self.assertIn(ALREADY, result["stdout"])
        self.assertNotIn("Created user", result["stdout"])
        self.assertEqual(self._posts(result), [])
        self.assertEqual(self._mutations(result), [])
        self.assertNotIn(("GET", SQUADS_PATH), result["calls"])
        self.assertEqual(result["users"][USERNAME], before)
        self.assertEqual(result["storage"][STORAGE_PATH], _expected_subscription(before))
        self.assertNotIn("2026-12-31", json.dumps(result["storage"]))

    def test_lookup_404_then_a019_adopts_without_applying_the_payload(self) -> None:
        """Parallel CREATE: both lookups see absence, one POST wins, the other gets A019."""
        STATE.post_mode = "a019_insert"
        result = self._run()
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["stderr"], "")
        self.assertIn(ALREADY, result["stdout"])
        self.assertNotIn("Created user", result["stdout"])
        self.assertEqual(self._posts(result), [("POST", CREATE_PATH)])
        self.assertEqual(self._mutations(result), [])
        payload = result["payloads"][0]
        user = result["users"][USERNAME]
        self.assertEqual(payload["username"], USERNAME)
        self.assertEqual(payload["expireAt"], "2026-12-31T23:59:59Z")
        self.assertEqual(payload["activeInternalSquads"], [SQUAD_UUID])
        self.assertEqual(user, _stable_user())
        self.assertEqual(result["storage"][STORAGE_PATH], _expected_subscription(user))

    def test_only_a063_404_means_the_user_is_absent(self) -> None:
        cases = {
            "a025": "a025_404",
            "html": "html_404",
            "a063_wrong_message": "a063_wrong_message",
        }
        for label, mode in cases.items():
            with self.subTest(label=label):
                STATE.reset()
                STATE.lookup_mode = mode
                result = self._run()
                self.assertNotEqual(result["code"], 0)
                self.assertIn("HTTP 404 is not A063", result["stderr"])
                self.assertEqual(self._posts(result), [])
                self.assertEqual(result["storage"], {})
                self.assertEqual(result["users"], {})
                self.assertNotIn("done", result["stdout"])

    def test_http_404_body_is_not_the_user(self) -> None:
        STATE.lookup_mode = "always_404"
        result = self._run()
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(self._posts(result), [("POST", CREATE_PATH)])
        self.assertEqual(result["users"][USERNAME]["uuid"], FRESH_UUID)
        self.assertIn("Created user us_6819 (id=50)", result["stdout"])

    def test_a019_without_a_matching_user_is_fatal(self) -> None:
        cases = {
            "still_absent": ("a019", "store", "did not find that user"),
            "username_mismatch": ("a019", "mismatch", "different username"),
            "invalid_id": ("a019", "invalid_id", "without a valid user id"),
            "string_id": ("a019", "string_id", "without a valid user id"),
            "missing_username": ("a019", "missing_username", "without a valid user id"),
        }
        for label, (post_mode, lookup_mode, needle) in cases.items():
            with self.subTest(label=label):
                STATE.reset()
                # First lookup is the real 404 that allows POST. The bad
                # identity is the lookup performed after A019.
                STATE.miss_lookups = 1
                STATE.post_mode = post_mode
                STATE.lookup_mode = lookup_mode
                result = self._run()
                self.assertNotEqual(result["code"], 0)
                self.assertIn(needle, result["stderr"])
                self.assertNotIn("done", result["stdout"])
                self.assertNotIn("already exists", result["stdout"])
                self.assertEqual(result["storage"], {})
                self.assertEqual(self._posts(result), [("POST", CREATE_PATH)])
                self.assertEqual(self._mutations(result), [])

    def test_other_a019_shaped_responses_do_not_adopt(self) -> None:
        # The panel writes the user and then returns a response that only
        # looks like A019. Adopting that body would exit 0. It must not.
        modes = {
            "a020": "a020_insert",
            "validation_text": "validation_insert",
            "wrong_message": "wrong_message_insert",
            "message_only": "message_only_insert",
            "code_only": "code_only_insert",
            "nested_code": "nested_a019_insert",
            "http_500": "http_500_a019_insert",
        }
        for label, mode in modes.items():
            with self.subTest(label=label):
                STATE.reset()
                STATE.post_mode = mode
                result = self._run()
                self.assertNotEqual(result["code"], 0, result["stdout"])
                self.assertNotIn("done", result["stdout"])
                self.assertNotIn("already exists", result["stdout"])
                self.assertEqual(result["storage"], {})
                self.assertEqual(result["users"][USERNAME], _stable_user())
                self.assertEqual(self._mutations(result), [])
                if label == "http_500":
                    self.assertIn("HTTP 500", result["stderr"])
                elif label == "a020":
                    self.assertIn("A020", result["stderr"])

    def test_post_curl_failure_is_fatal_even_when_the_body_is_a019(self) -> None:
        before = STATE.seed(_stable_user())
        STATE.miss_lookups = 1
        real_curl = shutil.which("curl")
        self.assertIsNotNone(real_curl)
        with tempfile.TemporaryDirectory() as tmp:
            curl_path = Path(tmp) / "curl"
            curl_path.write_text(
                "#!/bin/bash\n"
                "method=''\n"
                "prev=''\n"
                "for arg in \"$@\"; do\n"
                "  if [[ \"${prev}\" == '-X' ]]; then method=\"${arg}\"; fi\n"
                "  prev=\"${arg}\"\n"
                "done\n"
                "url=\"${@: -1}\"\n"
                "if [[ \"${method}\" == 'POST' && \"${url}\" == */api/users ]]; then\n"
                "  outfile=''\n"
                "  prev=''\n"
                "  for arg in \"$@\"; do\n"
                "    if [[ \"${prev}\" == '-o' ]]; then outfile=\"${arg}\"; fi\n"
                "    prev=\"${arg}\"\n"
                "  done\n"
                "  printf '%s' '{"
                "\"message\":\"User username already exists\","
                "\"errorCode\":\"A019\","
                "\"path\":\"/api/users\"}' > \"${outfile}\"\n"
                "  printf '%s' '400'\n"
                "  exit 22\n"
                "fi\n"
                f"exec {real_curl} \"$@\"\n",
                encoding="utf-8",
            )
            curl_path.chmod(0o755)
            result = self._run(path_prefix=tmp)
        self.assertNotEqual(result["code"], 0)
        self.assertIn("curl exit 22", result["stderr"])
        self.assertNotIn("done", result["stdout"])
        self.assertNotIn("already exists", result["stdout"])
        self.assertEqual(result["storage"], {})
        self.assertNotIn(("PUT", STORAGE_PATH), result["calls"])
        self.assertEqual(result["users"][USERNAME], before)
        self.assertNotIn(("POST", CREATE_PATH), result["calls"])

    def test_create_response_without_id_retries_without_a_second_user(self) -> None:
        STATE.post_mode = "create_no_id"
        failed = self._run()
        self.assertNotEqual(failed["code"], 0)
        self.assertIn("no valid user id", failed["stderr"])
        self.assertEqual(failed["storage"], {})
        self.assertEqual(failed["users"][USERNAME]["uuid"], FRESH_UUID)
        created = copy.deepcopy(failed["users"])
        STATE.post_mode = "create"
        STATE.calls.clear()
        STATE.payloads.clear()

        recovered = self._run()
        self.assertEqual(recovered["code"], 0, recovered["stderr"])
        self.assertIn("already exists (id=50)", recovered["stdout"])
        self.assertEqual(self._posts(recovered), [])
        self.assertEqual(self._mutations(recovered), [])
        self.assertEqual(recovered["users"], created)
        self.assertEqual(
            recovered["storage"][STORAGE_PATH],
            _expected_subscription(created[USERNAME]),
        )

    def test_malformed_create_response_retries_the_same_user(self) -> None:
        STATE.post_mode = "create_malformed"
        failed = self._run()
        self.assertNotEqual(failed["code"], 0)
        self.assertIn("malformed JSON", failed["stderr"])
        self.assertEqual(failed["storage"], {})
        created = copy.deepcopy(failed["users"])
        STATE.post_mode = "create"
        STATE.calls.clear()

        recovered = self._run()
        self.assertEqual(recovered["code"], 0, recovered["stderr"])
        self.assertEqual(self._posts(recovered), [])
        self.assertEqual(recovered["users"], created)
        self.assertIn(STORAGE_PATH, recovered["storage"])

    def test_create_response_with_only_an_id_is_accepted(self) -> None:
        STATE.post_mode = "create_id_only"
        result = self._run()
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertIn("Created user us_6819 (id=50)", result["stdout"])
        self.assertEqual(result["users"][USERNAME]["uuid"], FRESH_UUID)
        self.assertIn(STORAGE_PATH, result["storage"])

    def test_create_response_for_a_different_username_is_not_stored(self) -> None:
        STATE.post_mode = "create_wrong_username"
        failed = self._run()
        self.assertNotEqual(failed["code"], 0)
        self.assertIn("different username", failed["stderr"])
        self.assertEqual(failed["storage"], {})
        created = copy.deepcopy(failed["users"])
        STATE.post_mode = "create"
        STATE.calls.clear()

        recovered = self._run()
        self.assertEqual(recovered["code"], 0, recovered["stderr"])
        self.assertEqual(self._posts(recovered), [])
        self.assertEqual(recovered["users"], created)
        self.assertEqual(recovered["users"][USERNAME]["username"], USERNAME)

    def test_subscription_failure_then_retry_does_not_create_again(self) -> None:
        before = STATE.seed(_stable_user())
        STATE.subscription_status = 500
        failed = self._run()
        self.assertNotEqual(failed["code"], 0)
        self.assertIn("HTTP 500", failed["stderr"])
        self.assertEqual(failed["storage"], {})
        self.assertEqual(self._posts(failed), [])
        self.assertEqual(failed["users"][USERNAME], before)
        STATE.subscription_status = 200
        STATE.calls.clear()

        recovered = self._run()
        self.assertEqual(recovered["code"], 0, recovered["stderr"])
        self.assertEqual(self._posts(recovered), [])
        self.assertEqual(recovered["users"][USERNAME], before)
        self.assertEqual(recovered["storage"][STORAGE_PATH], _expected_subscription(before))

    def test_storage_put_failure_then_retry_does_not_create_again(self) -> None:
        before = STATE.seed(_stable_user())
        STATE.shm_status = 500
        failed = self._run()
        self.assertNotEqual(failed["code"], 0)
        self.assertIn("curl exit 22", failed["stderr"])
        self.assertEqual(failed["storage"], {})
        self.assertIn(("PUT", STORAGE_PATH), failed["calls"])
        self.assertEqual(self._posts(failed), [])
        self.assertEqual(failed["users"][USERNAME], before)
        STATE.shm_status = 200
        STATE.calls.clear()

        recovered = self._run()
        self.assertEqual(recovered["code"], 0, recovered["stderr"])
        self.assertEqual(self._posts(recovered), [])
        self.assertEqual(recovered["users"][USERNAME], before)
        self.assertIn(STORAGE_PATH, recovered["storage"])

    def test_lost_post_response_retries_the_created_user(self) -> None:
        STATE.post_mode = "create_then_drop"
        failed = self._run()
        self.assertNotEqual(failed["code"], 0)
        self.assertRegex(failed["stderr"], r"curl exit [1-9][0-9]*")
        self.assertEqual(failed["storage"], {})
        self.assertEqual(failed["users"][USERNAME]["uuid"], FRESH_UUID)
        created = copy.deepcopy(failed["users"])
        STATE.post_mode = "create"
        STATE.calls.clear()
        STATE.payloads.clear()

        recovered = self._run()
        self.assertEqual(recovered["code"], 0, recovered["stderr"])
        self.assertIn("already exists (id=50)", recovered["stdout"])
        self.assertEqual(self._posts(recovered), [])
        self.assertEqual(self._mutations(recovered), [])
        self.assertEqual(recovered["users"], created)
        self.assertEqual(
            recovered["storage"][STORAGE_PATH]["user"]["shortUuid"],
            "fresh-short",
        )

    def test_rerun_after_successful_put_keeps_storage_identity(self) -> None:
        first = self._run()
        self.assertEqual(first["code"], 0, first["stderr"])
        stored = copy.deepcopy(first["storage"])
        created = copy.deepcopy(first["users"])
        STATE.calls.clear()
        STATE.payloads.clear()

        second = self._run()
        self.assertEqual(second["code"], 0, second["stderr"])
        self.assertEqual(self._posts(second), [])
        self.assertEqual(self._mutations(second), [])
        self.assertEqual(second["users"], created)
        self.assertEqual(second["storage"], stored)
        self.assertEqual(
            second["storage"][STORAGE_PATH]["user"]["shortUuid"],
            stored[STORAGE_PATH]["user"]["shortUuid"],
        )
        self.assertEqual(
            second["storage"][STORAGE_PATH]["subscriptionUrl"],
            stored[STORAGE_PATH]["subscriptionUrl"],
        )

    def test_rerun_does_not_replace_storage_when_subscription_identity_differs(self) -> None:
        before = STATE.seed(_stable_user())
        first = self._run()
        self.assertEqual(first["code"], 0, first["stderr"])
        stored = copy.deepcopy(first["storage"])
        STATE.calls.clear()
        STATE.subscription_short = "other-short"
        STATE.subscription_username = "us_other"

        rejected = self._run()
        self.assertNotEqual(rejected["code"], 0)
        self.assertIn("does not belong to this user", rejected["stderr"])
        self.assertEqual(self._posts(rejected), [])
        self.assertEqual(self._mutations(rejected), [])
        self.assertNotIn(("PUT", STORAGE_PATH), rejected["calls"])
        self.assertEqual(rejected["users"][USERNAME], before)
        self.assertEqual(rejected["storage"], stored)

        STATE.calls.clear()
        STATE.subscription_username = None
        mismatched = self._run()
        self.assertNotEqual(mismatched["code"], 0)
        self.assertIn("shortUuid", mismatched["stderr"])
        self.assertNotIn(("PUT", STORAGE_PATH), mismatched["calls"])
        self.assertEqual(mismatched["users"][USERNAME], before)
        self.assertEqual(mismatched["storage"], stored)

    def test_dropped_post_does_not_store_and_a_later_run_can_create(self) -> None:
        STATE.post_mode = "drop"
        failed = self._run()
        self.assertNotEqual(failed["code"], 0)
        self.assertRegex(failed["stderr"], r"curl exit [1-9][0-9]*")
        self.assertEqual(failed["users"], {})
        self.assertEqual(failed["storage"], {})
        STATE.post_mode = "create"
        STATE.calls.clear()
        STATE.payloads.clear()

        recovered = self._run()
        self.assertEqual(recovered["code"], 0, recovered["stderr"])
        self.assertEqual(len(self._posts(recovered)), 1)
        self.assertEqual(recovered["users"][USERNAME]["uuid"], FRESH_UUID)
        self.assertIn(STORAGE_PATH, recovered["storage"])

    def test_lookup_transport_error_does_not_create_or_store(self) -> None:
        dead = f"http://127.0.0.1:{_closed_port()}"
        live = f"http://127.0.0.1:{self.port}"
        result = self._run(panel_url=dead, api_url=live)
        self.assertNotEqual(result["code"], 0)
        self.assertRegex(result["stderr"], r"curl exit [1-9][0-9]*")
        self.assertNotIn("done", result["stdout"])
        self.assertEqual(result["calls"], [])
        self.assertEqual(result["users"], {})
        self.assertEqual(result["storage"], {})

    def test_lookup_http_errors_do_not_create(self) -> None:
        for status in (401, 409, 500, 204):
            with self.subTest(status=status):
                STATE.reset()
                STATE.lookup_mode = "status"
                STATE.lookup_status = status
                result = self._run()
                self.assertNotEqual(result["code"], 0)
                self.assertIn(f"HTTP {status}", result["stderr"])
                self.assertEqual(self._posts(result), [])
                self.assertEqual(result["storage"], {})
                self.assertEqual(result["users"], {})
                self.assertNotIn("done", result["stdout"])

    def test_lookup_200_that_is_not_this_user_does_not_create(self) -> None:
        cases = {
            "mismatch": ("mismatch", "different username"),
            "invalid_id": ("invalid_id", "without a valid user id"),
            "string_id": ("string_id", "without a valid user id"),
            "missing_username": ("missing_username", "without a valid user id"),
            "malformed": ("malformed", "malformed JSON"),
        }
        for label, (mode, needle) in cases.items():
            with self.subTest(label=label):
                STATE.reset()
                STATE.lookup_mode = mode
                result = self._run()
                self.assertNotEqual(result["code"], 0)
                self.assertIn(needle, result["stderr"])
                self.assertEqual(self._posts(result), [])
                self.assertEqual(result["storage"], {})
                self.assertNotIn(("GET", SQUADS_PATH), result["calls"])
                self.assertNotIn("done", result["stdout"])

    def test_other_events_do_not_adopt_a_missing_user(self) -> None:
        for event in ("ACTIVATE", "BLOCK", "PROLONGATE"):
            with self.subTest(event=event):
                STATE.reset()
                result = self._run(event)
                self.assertNotEqual(result["code"], 0)
                self.assertIn("User not found: us_6819", result["stderr"])
                self.assertEqual(result["storage"], {})
                self.assertEqual(self._posts(result), [])
                self.assertNotIn("done", result["stdout"])

    def test_create_adoption_is_not_used_by_other_events(self) -> None:
        text = TEMPLATE.read_text(encoding="utf-8")
        create = text.split("  CREATE)", 1)[1].split("  ACTIVATE)", 1)[0]
        self.assertIn("_lookup_user_for_create", create)
        self.assertIn("_post_new_user", create)
        self.assertIn("_save_create_subscription", create)
        self.assertNotIn("_http_post '/api/users'", create)
        self.assertNotIn("_http_patch", create)
        for event, end in (
            ("  ACTIVATE)", "  BLOCK)"),
            ("  BLOCK)", "  REMOVE)"),
            ("  REMOVE)", "  PROLONGATE)"),
            ("  PROLONGATE)", "  UPDATE)"),
            ("  UPDATE)", "  *)"),
        ):
            branch = text.split(event, 1)[1].split(end, 1)[0]
            self.assertNotIn("_lookup_user_for_create", branch)
            self.assertNotIn("_post_new_user", branch)
            self.assertNotIn("_save_create_subscription", branch)
            self.assertNotIn("_adopt_after_username_conflict", branch)


if __name__ == "__main__":
    unittest.main()
