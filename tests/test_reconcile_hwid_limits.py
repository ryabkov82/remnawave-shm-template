#!/usr/bin/env python3
"""Tests for scripts/reconcile_hwid_limits.py."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

from tests.proxy_env import DisableEnvProxiesMixin, snapshot_proxy_env  # noqa: E402

import reconcile_hwid_limits as rec  # noqa: E402


SHM_PASSWORD = "super-secret-shm-password-xyz"
RW_TOKEN = "super-secret-remnawave-token-abc"


class ParseAndClassifyTests(unittest.TestCase):
    def test_parse_tests_do_not_touch_proxy_env(self) -> None:
        before = snapshot_proxy_env()
        rec.parse_shm_hwid_setting(None)
        rec.classify_hwid(target=None, current=0)
        self.assertEqual(snapshot_proxy_env(), before)

    def test_absent_and_null_target_none(self) -> None:
        self.assertIsNone(rec.parse_shm_hwid_setting(None))
        self.assertIsNone(rec.parse_shm_hwid_setting(""))
        self.assertIsNone(rec.parse_shm_hwid_setting("null"))
        self.assertIsNone(rec.parse_shm_hwid_setting("  null  "))

    def test_zero_target_zero(self) -> None:
        self.assertEqual(rec.parse_shm_hwid_setting(0), 0)
        self.assertEqual(rec.parse_shm_hwid_setting("0"), 0)

    def test_positive_integer_target(self) -> None:
        self.assertEqual(rec.parse_shm_hwid_setting(3), 3)
        self.assertEqual(rec.parse_shm_hwid_setting("3"), 3)

    def test_invalid_and_negative_raise(self) -> None:
        for raw in (-1, "-1", 1.5, "1.5", "abc", True, 3.0, "1e2"):
            with self.subTest(raw=raw):
                with self.assertRaises(rec.HwidSettingError):
                    rec.parse_shm_hwid_setting(raw)

    def test_current_zero_target_null_needs_reset(self) -> None:
        self.assertEqual(
            rec.classify_hwid(target=None, current=0),
            rec.CLASS_RESET_TO_PANEL_DEFAULT,
        )

    def test_current_null_target_null_already_correct(self) -> None:
        self.assertEqual(
            rec.classify_hwid(target=None, current=None),
            rec.CLASS_ALREADY_CORRECT,
        )

    def test_current_five_target_null_is_not_equivalent(self) -> None:
        self.assertEqual(
            rec.classify_hwid(target=None, current=5),
            rec.CLASS_RESET_TO_PANEL_DEFAULT,
        )
        self.assertNotEqual(
            rec.classify_hwid(target=None, current=5),
            rec.CLASS_ALREADY_CORRECT,
        )

    def test_current_null_target_three_sets_explicit(self) -> None:
        self.assertEqual(
            rec.classify_hwid(target=3, current=None),
            rec.CLASS_SET_EXPLICIT,
        )

    def test_current_zero_target_zero_already_correct(self) -> None:
        self.assertEqual(
            rec.classify_hwid(target=0, current=0),
            rec.CLASS_ALREADY_CORRECT,
        )

    def test_current_three_target_zero_needs_disable(self) -> None:
        self.assertEqual(
            rec.classify_hwid(target=0, current=3),
            rec.CLASS_DISABLE,
        )

    def test_patch_payload_json_null_not_string_or_zero(self) -> None:
        payload = rec.build_hwid_patch_payload(7, None)
        self.assertEqual(payload, {"id": 7, "hwidDeviceLimit": None})
        raw = rec.encode_hwid_patch_body(7, None)
        self.assertIn(b'"hwidDeviceLimit":null', raw)
        self.assertNotIn(b'"hwidDeviceLimit":0', raw)
        self.assertNotIn(b'"hwidDeviceLimit":"null"', raw)
        decoded = json.loads(raw.decode("utf-8"))
        self.assertIsNone(decoded["hwidDeviceLimit"])

    def test_patch_payload_zero_and_positive(self) -> None:
        self.assertEqual(
            rec.build_hwid_patch_payload(7, 0),
            {"id": 7, "hwidDeviceLimit": 0},
        )
        self.assertEqual(
            rec.build_hwid_patch_payload(7, 3),
            {"id": 7, "hwidDeviceLimit": 3},
        )

    def test_parse_apply_usernames_dedupes_and_rejects_empty(self) -> None:
        self.assertEqual(
            rec.parse_apply_usernames([" us_1 ", "us_2", "us_1"]),
            ("us_1", "us_2"),
        )
        with self.assertRaises(rec.FatalError):
            rec.parse_apply_usernames(["us_1", "  "])

    def test_select_apply_rows_is_allow_list_and_mutation_class(self) -> None:
        reset = rec.classify_row(
            user_service_id=1,
            category="vpn-mz-test",
            service_id=9,
            raw_hwid=None,
            fetch_kind="ok",
            remnawave_user_id=11,
            current_hwid=0,
            error_message=None,
        )
        already = rec.classify_row(
            user_service_id=2,
            category="vpn-mz-test",
            service_id=9,
            raw_hwid=None,
            fetch_kind="ok",
            remnawave_user_id=12,
            current_hwid=None,
            error_message=None,
        )
        missing = rec.classify_row(
            user_service_id=3,
            category="vpn-mz-test",
            service_id=9,
            raw_hwid=None,
            fetch_kind="missing",
            remnawave_user_id=None,
            current_hwid=None,
            error_message=None,
        )
        other_reset = rec.classify_row(
            user_service_id=4,
            category="vpn-mz-test",
            service_id=9,
            raw_hwid=None,
            fetch_kind="ok",
            remnawave_user_id=14,
            current_hwid=0,
            error_message=None,
        )
        selected = rec.select_apply_rows(
            [reset, already, missing, other_reset],
            ("us_1", "us_2", "us_3"),
        )
        self.assertEqual([row.username for row in selected], ["us_1"])
        self.assertNotIn("us_4", [row.username for row in selected])

    def test_live_state_matches_plan_detects_drift(self) -> None:
        row = rec.classify_row(
            user_service_id=1,
            category="vpn-mz-test",
            service_id=9,
            raw_hwid=None,
            fetch_kind="ok",
            remnawave_user_id=11,
            current_hwid=0,
            error_message=None,
        )
        self.assertIsNone(
            rec.live_state_matches_plan(
                row,
                live_user_id=11,
                live_current=0,
                fetch_kind="ok",
                fetch_error=None,
            )
        )
        err = rec.live_state_matches_plan(
            row,
            live_user_id=11,
            live_current=5,
            fetch_kind="ok",
            fetch_error=None,
        )
        self.assertIsNotNone(err)
        self.assertIn("current hwidDeviceLimit drift", err or "")

    def test_invalid_row_has_unresolved_target_not_panel_default(self) -> None:
        row = rec.classify_row(
            user_service_id=1,
            category="vpn-mz-test",
            service_id=9,
            raw_hwid=-1,
            fetch_kind="ok",
            remnawave_user_id=11,
            current_hwid=0,
            error_message=None,
        )
        self.assertEqual(row.classification, rec.CLASS_INVALID)
        self.assertIs(row.target_hwid_device_limit, rec.TARGET_UNRESOLVED)
        self.assertNotIn(row.classification, rec.APPLY_CLASSES)
        report = row.to_report_dict()
        self.assertFalse(report["target_resolved"])
        self.assertNotIn("target_hwid_device_limit", report)
        serialized = json.dumps(report)
        self.assertNotIn("target_hwid_device_limit", serialized)

    def test_valid_null_target_is_json_null_and_resolved(self) -> None:
        row = rec.classify_row(
            user_service_id=2,
            category="vpn-mz-test",
            service_id=9,
            raw_hwid=None,
            fetch_kind="ok",
            remnawave_user_id=12,
            current_hwid=0,
            error_message=None,
        )
        self.assertEqual(row.classification, rec.CLASS_RESET_TO_PANEL_DEFAULT)
        self.assertIsNone(row.target_hwid_device_limit)
        self.assertIsNot(row.target_hwid_device_limit, rec.TARGET_UNRESOLVED)
        report = row.to_report_dict()
        self.assertTrue(report["target_resolved"])
        self.assertIn("target_hwid_device_limit", report)
        self.assertIsNone(report["target_hwid_device_limit"])
        self.assertIn('"target_hwid_device_limit": null', json.dumps(report))

    def test_extract_hwid_from_service_shapes(self) -> None:
        self.assertIsNone(
            rec.extract_hwid_raw_from_service({"config": {"remnawave": {}}})
        )
        self.assertEqual(
            rec.extract_hwid_raw_from_service(
                {"config": {"remnawave": {"hwid_device_limit": 3}}}
            ),
            3,
        )
        self.assertEqual(
            rec.extract_hwid_raw_from_service(
                {"settings": {"remnawave": {"hwid_device_limit": 0}}}
            ),
            0,
        )
        self.assertIsNone(
            rec.extract_hwid_raw_from_service(
                {"settings": {"remnawave": {"hwid_device_limit": None}}}
            )
        )


class MockState:
    def __init__(self) -> None:
        self.shm_services: List[Dict[str, Any]] = []
        self.catalog: List[Dict[str, Any]] = []
        self.users: Dict[str, Dict[str, Any]] = {}
        self.patches: List[Dict[str, Any]] = []
        self.patch_raws: List[bytes] = []
        self.auth_bodies: List[Dict[str, Any]] = []
        self.patch_fail_usernames: set = set()
        self.patch_omit_hwid_usernames: set = set()
        self.get_lag_usernames: Dict[str, int] = {}
        self.get_user_hits: Dict[str, int] = {}
        self.drift_hwid_after_plan: Dict[str, Any] = {}
        self.lock = threading.Lock()

    def reset(self) -> None:
        with self.lock:
            self.shm_services.clear()
            self.catalog.clear()
            self.users.clear()
            self.patches.clear()
            self.patch_raws.clear()
            self.auth_bodies.clear()
            self.patch_fail_usernames.clear()
            self.patch_omit_hwid_usernames.clear()
            self.get_lag_usernames.clear()
            self.get_user_hits.clear()
            self.drift_hwid_after_plan.clear()


STATE = MockState()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    def _read_raw(self) -> bytes:
        length = int(self.headers.get("Content-Length") or "0")
        return self.rfile.read(length) if length else b""

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _username_for_id(self, user_id: Any) -> str:
        with STATE.lock:
            for username, user in STATE.users.items():
                if user.get("id") == user_id:
                    return username
        return ""

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/shm/user/auth.cgi":
            body = json.loads(self._read_raw().decode("utf-8") or "null") or {}
            with STATE.lock:
                STATE.auth_bodies.append(body)
            self._send(200, {"session_id": "test-session"})
            return
        if parsed.path == "/api/users/bulk/update":
            self._send(500, {"message": "bulk update must not be used"})
            return
        self._send(404, {"message": "not found"})

    def do_PATCH(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/users":
            self._send(404, {"message": "not found"})
            return
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {RW_TOKEN}":
            self._send(401, {"message": "unauthorized"})
            return
        raw = self._read_raw()
        body = json.loads(raw.decode("utf-8")) if raw else {}
        with STATE.lock:
            STATE.patch_raws.append(raw)
            STATE.patches.append(body)
        user_id = body.get("id")
        target = body.get("hwidDeviceLimit") if "hwidDeviceLimit" in body else "MISSING"
        username = self._username_for_id(user_id)
        with STATE.lock:
            if username in STATE.patch_fail_usernames:
                self._send(500, {"message": "forced patch failure"})
                return
            user = STATE.users.get(username)
            if user is None:
                self._send(404, {"message": "User not found"})
                return
            user["hwidDeviceLimit"] = target
            omit = username in STATE.patch_omit_hwid_usernames
        if omit:
            self._send(200, {"response": {"id": user_id}})
            return
        self._send(
            200,
            {"response": {"id": user_id, "hwidDeviceLimit": target}},
        )

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/shm/v1/admin/user/service":
            if self.headers.get("session-id") != "test-session":
                self._send(401, {"message": "unauthorized"})
                return
            limit = int((qs.get("limit") or ["250"])[0])
            offset = int((qs.get("offset") or ["0"])[0])
            with STATE.lock:
                items = list(STATE.shm_services)
            page = items[offset : offset + limit]
            self._send(
                200,
                {
                    "data": page,
                    "items": len(items),
                    "limit": limit,
                    "offset": offset,
                },
            )
            return

        if parsed.path == "/shm/v1/admin/service":
            if self.headers.get("session-id") != "test-session":
                self._send(401, {"message": "unauthorized"})
                return
            limit = int((qs.get("limit") or ["250"])[0])
            offset = int((qs.get("offset") or ["0"])[0])
            with STATE.lock:
                items = list(STATE.catalog)
            page = items[offset : offset + limit]
            self._send(
                200,
                {
                    "data": page,
                    "items": len(items),
                    "limit": limit,
                    "offset": offset,
                },
            )
            return

        if parsed.path.startswith("/api/users/by-username/"):
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {RW_TOKEN}":
                self._send(401, {"message": "unauthorized"})
                return
            username = urllib.parse.unquote(
                parsed.path[len("/api/users/by-username/") :]
            )
            with STATE.lock:
                STATE.get_user_hits[username] = (
                    STATE.get_user_hits.get(username, 0) + 1
                )
                user = STATE.users.get(username)
                lag = STATE.get_lag_usernames.get(username, 0)
                patched = any(
                    p.get("id") == (user or {}).get("id") for p in STATE.patches
                )
                post_patch_gets = 0
                if patched and user is not None:
                    key = f"post:{username}"
                    STATE.get_user_hits[key] = STATE.get_user_hits.get(key, 0) + 1
                    post_patch_gets = STATE.get_user_hits[key]
                hits = STATE.get_user_hits.get(username, 0)
                if (
                    user is not None
                    and not patched
                    and username in STATE.drift_hwid_after_plan
                    and hits >= 2
                ):
                    user["hwidDeviceLimit"] = STATE.drift_hwid_after_plan[username]
            if user is None:
                self._send(404, {"message": "User not found"})
                return
            effective = user.get("hwidDeviceLimit")
            if patched and lag and post_patch_gets <= lag:
                effective = user.get("_pre_hwid", 0)
            self._send(
                200,
                {
                    "response": {
                        "id": user["id"],
                        "hwidDeviceLimit": effective,
                    }
                },
            )
            return

        self._send(404, {"message": "not found"})


def start_server() -> Tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


class ReconcileHwidTests(DisableEnvProxiesMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.server, cls.base_url = start_server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        super().tearDownClass()

    def setUp(self) -> None:
        STATE.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _cfg(self, output: str, **kwargs: Any) -> rec.ReconcileConfig:
        values = dict(
            shm_base_url=self.base_url,
            shm_login="admin",
            shm_password=SHM_PASSWORD,
            remnawave_panel_url=self.base_url,
            remnawave_token=RW_TOKEN,
            output=output,
            page_size=250,
            request_delay_ms=0,
            apply=False,
            confirm=None,
            apply_usernames=(),
            http_timeout=5,
            verify_retry_attempts=5,
            verify_retry_delay_sec=0,
        )
        values.update(kwargs)
        return rec.ReconcileConfig(**values)

    def _add_catalog(
        self, service_id: int, hwid: Any = "ABSENT", category: str = "vpn-mz-test"
    ) -> None:
        remnawave: Dict[str, Any] = {"internal_squad_name": "Default-Squad"}
        if hwid != "ABSENT":
            remnawave["hwid_device_limit"] = hwid
        STATE.catalog.append(
            {
                "service_id": service_id,
                "category": category,
                "config": {"remnawave": remnawave},
            }
        )

    def _add_user_service(
        self,
        user_service_id: int,
        service_id: int,
        category: str = "vpn-mz-test",
    ) -> None:
        STATE.shm_services.append(
            {
                "user_service_id": user_service_id,
                "service_id": service_id,
                "category": category,
                "status": "ACTIVE",
            }
        )

    def _add_user(
        self, user_service_id: int, hwid: Any, *, pre_hwid: Any = 0
    ) -> int:
        username = f"us_{user_service_id}"
        user_id = 1000 + user_service_id
        STATE.users[username] = {
            "id": user_id,
            "hwidDeviceLimit": hwid,
            "_pre_hwid": pre_hwid,
        }
        return user_id

    def _run(self, cfg: rec.ReconcileConfig) -> Tuple[int, str]:
        buf = io.StringIO()
        with mock.patch.object(rec, "log", lambda msg: buf.write(msg + "\n")):
            client = rec.HttpClient(delay_ms=0, timeout=5)
            code = rec.run(cfg, client=client)
        return code, buf.getvalue()

    def _load_plan(self, output: str) -> List[Dict[str, Any]]:
        with open(os.path.join(output, "plan.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def _load_summary(self, output: str) -> Dict[str, Any]:
        with open(os.path.join(output, "summary.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_absent_setting_current_zero_needs_reset(self) -> None:
        self._add_catalog(1, "ABSENT")
        self._add_user_service(11, 1)
        self._add_user(11, 0)
        out = os.path.join(self.tmp.name, "out-absent")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        row = self._load_plan(out)[0]
        self.assertEqual(row["username"], "us_11")
        self.assertTrue(row["target_resolved"])
        self.assertIsNone(row["target_hwid_device_limit"])
        self.assertEqual(row["current_hwid_device_limit"], 0)
        self.assertEqual(row["classification"], "needs_reset_to_panel_default")
        self.assertEqual(len(STATE.patches), 0)

    def test_null_setting_current_zero_needs_reset(self) -> None:
        self._add_catalog(2, None)
        self._add_user_service(12, 2)
        self._add_user(12, 0)
        out = os.path.join(self.tmp.name, "out-null")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        row = self._load_plan(out)[0]
        self.assertIsNone(row["target_hwid_device_limit"])
        self.assertEqual(row["classification"], "needs_reset_to_panel_default")

    def test_explicit_zero_stays_zero(self) -> None:
        self._add_catalog(3, 0)
        self._add_user_service(13, 3)
        self._add_user(13, 0)
        out = os.path.join(self.tmp.name, "out-keep-zero")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        row = self._load_plan(out)[0]
        self.assertEqual(row["target_hwid_device_limit"], 0)
        self.assertEqual(row["classification"], "already_correct")
        snap = self._load_summary(out)["hwid_snapshot"]
        self.assertEqual(snap["zero_should_stay_zero"], 1)
        self.assertEqual(snap["zero_should_become_null"], 0)

    def test_positive_setting_sets_explicit(self) -> None:
        self._add_catalog(4, 3)
        self._add_user_service(14, 4)
        self._add_user(14, None)
        out = os.path.join(self.tmp.name, "out-set-3")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        row = self._load_plan(out)[0]
        self.assertEqual(row["target_hwid_device_limit"], 3)
        self.assertEqual(row["classification"], "needs_set_explicit_limit")

    def test_current_five_target_null_needs_reset(self) -> None:
        self._add_catalog(5, "ABSENT")
        self._add_user_service(15, 5)
        self._add_user(15, 5)
        out = os.path.join(self.tmp.name, "out-five")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        row = self._load_plan(out)[0]
        self.assertEqual(row["current_hwid_device_limit"], 5)
        self.assertIsNone(row["target_hwid_device_limit"])
        self.assertEqual(row["classification"], "needs_reset_to_panel_default")

    def test_invalid_shm_setting(self) -> None:
        self._add_catalog(6, -1)
        self._add_user_service(16, 6)
        self._add_user(16, 0)
        out = os.path.join(self.tmp.name, "out-invalid")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        row = self._load_plan(out)[0]
        self.assertEqual(row["classification"], "invalid_shm_setting")
        self.assertNotIn(row["classification"], rec.APPLY_CLASSES)
        self.assertFalse(row["target_resolved"])
        self.assertNotIn("target_hwid_device_limit", row)
        with open(os.path.join(out, "errors.csv"), encoding="utf-8") as fh:
            csv_text = fh.read()
        self.assertIn("us_16", csv_text)
        self.assertIn("False", csv_text)
        self.assertEqual(len(STATE.patches), 0)

    def test_missing_in_remnawave(self) -> None:
        self._add_catalog(7, "ABSENT")
        self._add_user_service(17, 7)
        out = os.path.join(self.tmp.name, "out-missing")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        row = self._load_plan(out)[0]
        self.assertEqual(row["classification"], "missing_in_remnawave")
        with open(os.path.join(out, "missing.csv"), encoding="utf-8") as fh:
            self.assertIn("us_17", fh.read())

    def test_unknown_service_is_error_not_guessed(self) -> None:
        self._add_user_service(18, 999)
        self._add_user(18, 0)
        out = os.path.join(self.tmp.name, "out-nosvc")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        row = self._load_plan(out)[0]
        self.assertEqual(row["classification"], "error")
        self.assertFalse(row["target_resolved"])
        self.assertNotIn("target_hwid_device_limit", row)
        self.assertIn("not found", row["error_message"])
        self.assertEqual(len(STATE.patches), 0)

    def test_dry_run_is_default(self) -> None:
        self._add_catalog(8, "ABSENT")
        self._add_user_service(21, 8)
        self._add_user(21, 0)
        out = os.path.join(self.tmp.name, "out-dry")
        code, logs = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        self.assertEqual(STATE.patches, [])
        self.assertIn("Dry-run complete", logs)
        self.assertTrue(self._load_summary(out)["apply_default"] is False)

    def test_apply_guard_wrong_confirm(self) -> None:
        self._add_catalog(8, "ABSENT")
        self._add_user_service(22, 8)
        self._add_user(22, 0)
        out = os.path.join(self.tmp.name, "out-guard")
        code, logs = self._run(self._cfg(out, apply=True, confirm="NOPE"))
        self.assertEqual(code, 1)
        self.assertEqual(STATE.patches, [])
        self.assertFalse(os.path.exists(out))
        self.assertIn("apply refused", logs)

    def test_apply_requires_username_allow_list(self) -> None:
        self._add_catalog(8, "ABSENT")
        self._add_user_service(22, 8)
        self._add_user(22, 0)
        out = os.path.join(self.tmp.name, "out-no-allow")
        code, logs = self._run(
            self._cfg(out, apply=True, confirm=rec.CONFIRM_PHRASE)
        )
        self.assertEqual(code, 1)
        self.assertEqual(STATE.patches, [])
        self.assertFalse(os.path.exists(out))
        self.assertIn("--apply-username", logs)

    def test_apply_reset_sends_json_null(self) -> None:
        self._add_catalog(8, "ABSENT")
        self._add_user_service(23, 8)
        user_id = self._add_user(23, 0)
        out = os.path.join(self.tmp.name, "out-apply-null")
        code, _ = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                apply_usernames=("us_23",),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.patches), 1)
        self.assertEqual(STATE.patches[0]["id"], user_id)
        self.assertIsNone(STATE.patches[0]["hwidDeviceLimit"])
        raw = STATE.patch_raws[0]
        self.assertIn(b'"hwidDeviceLimit":null', raw)
        self.assertNotIn(b'"hwidDeviceLimit":0', raw)
        self.assertNotIn(b'"hwidDeviceLimit":"null"', raw)
        self.assertIsNone(STATE.users["us_23"]["hwidDeviceLimit"])
        with open(os.path.join(out, "applied.json"), encoding="utf-8") as fh:
            applied = json.load(fh)
        self.assertEqual(applied[0]["username"], "us_23")
        self.assertIsNone(applied[0]["target_hwid_device_limit"])

    def test_apply_does_not_reset_explicit_zero(self) -> None:
        self._add_catalog(9, 0)
        self._add_user_service(24, 9)
        self._add_user(24, 0)
        self._add_catalog(10, "ABSENT")
        self._add_user_service(25, 10)
        self._add_user(25, 0)
        out = os.path.join(self.tmp.name, "out-no-blind")
        code, _ = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                apply_usernames=("us_24", "us_25"),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.patches), 1)
        self.assertEqual(STATE.patches[0]["id"], 1025)
        self.assertIsNone(STATE.patches[0]["hwidDeviceLimit"])
        self.assertEqual(STATE.users["us_24"]["hwidDeviceLimit"], 0)

    def test_apply_sets_explicit_limit(self) -> None:
        self._add_catalog(11, 3)
        self._add_user_service(26, 11)
        self._add_user(26, None)
        out = os.path.join(self.tmp.name, "out-set")
        code, _ = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                apply_usernames=("us_26",),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(STATE.patches[0]["hwidDeviceLimit"], 3)
        self.assertEqual(STATE.users["us_26"]["hwidDeviceLimit"], 3)

    def test_idempotency_second_dry_run_already_correct(self) -> None:
        self._add_catalog(12, "ABSENT")
        self._add_user_service(27, 12)
        self._add_user(27, 0)
        out_apply = os.path.join(self.tmp.name, "out-idemp-a")
        code, _ = self._run(
            self._cfg(
                out_apply,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                apply_usernames=("us_27",),
            )
        )
        self.assertEqual(code, 0)
        self.assertIsNone(STATE.users["us_27"]["hwidDeviceLimit"])

        out_dry = os.path.join(self.tmp.name, "out-idemp-b")
        code, _ = self._run(self._cfg(out_dry))
        self.assertEqual(code, 0)
        row = self._load_plan(out_dry)[0]
        self.assertEqual(row["classification"], "already_correct")
        self.assertEqual(len(STATE.patches), 1)

    def test_snapshot_counts(self) -> None:
        self._add_catalog(20, "ABSENT")
        self._add_user_service(31, 20)
        self._add_user(31, 0)
        self._add_catalog(21, 0)
        self._add_user_service(32, 21)
        self._add_user(32, 0)
        self._add_catalog(22, 3)
        self._add_user_service(33, 22)
        self._add_user(33, 3)
        self._add_catalog(23, "ABSENT")
        self._add_user_service(34, 23)
        self._add_user(34, None)
        out = os.path.join(self.tmp.name, "out-snap")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        snap = self._load_summary(out)["hwid_snapshot"]
        self.assertEqual(snap["current_is_zero"], 2)
        self.assertEqual(snap["zero_should_become_null"], 1)
        self.assertEqual(snap["zero_should_stay_zero"], 1)
        self.assertEqual(snap["current_is_explicit_positive"], 1)
        self.assertEqual(snap["already_matches_shm"], 3)

    def test_username_uses_user_service_id(self) -> None:
        self._add_catalog(30, "ABSENT")
        STATE.shm_services.append(
            {
                "user_service_id": 777,
                "user_id": 1,
                "service_id": 30,
                "category": "vpn-mz-test",
                "status": "ACTIVE",
            }
        )
        self._add_user(777, 0)
        STATE.users["us_1"] = {"id": 1, "hwidDeviceLimit": 0}
        out = os.path.join(self.tmp.name, "out-usid")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        self.assertEqual(self._load_plan(out)[0]["username"], "us_777")

    def test_category_filter(self) -> None:
        self._add_catalog(40, "ABSENT", category="vpn-mz-test")
        self._add_user_service(41, 40, category="vpn-mz-test")
        self._add_user(41, 0)
        self._add_catalog(41, "ABSENT", category="other")
        self._add_user_service(42, 41, category="other")
        self._add_user(42, 0)
        out = os.path.join(self.tmp.name, "out-cat")
        code, _ = self._run(self._cfg(out, categories=("vpn-mz-test",)))
        self.assertEqual(code, 0)
        plan = self._load_plan(out)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["username"], "us_41")

    def test_reports_exist(self) -> None:
        self._add_catalog(50, "ABSENT")
        self._add_user_service(51, 50)
        self._add_user(51, 0)
        out = os.path.join(self.tmp.name, "out-reports")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        for name in (
            "summary.json",
            "plan.json",
            "plan.csv",
            "errors.csv",
            "missing.csv",
        ):
            self.assertTrue(os.path.exists(os.path.join(out, name)), name)

    def test_allow_list_excludes_other_mutation_users(self) -> None:
        self._add_catalog(60, "ABSENT")
        self._add_user_service(61, 60)
        self._add_user(61, 0)
        self._add_user_service(62, 60)
        self._add_user(62, 0)
        out = os.path.join(self.tmp.name, "out-allow-exclude")
        code, _ = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                apply_usernames=("us_61",),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.patches), 1)
        self.assertEqual(STATE.patches[0]["id"], 1061)
        self.assertIsNone(STATE.users["us_61"]["hwidDeviceLimit"])
        self.assertEqual(STATE.users["us_62"]["hwidDeviceLimit"], 0)
        summary = self._load_summary(out)
        self.assertEqual(summary["apply"]["requested"], 1)
        self.assertEqual(summary["apply"]["applied"], 1)

    def test_allow_listed_already_correct_is_not_patched(self) -> None:
        self._add_catalog(70, None)
        self._add_user_service(71, 70)
        self._add_user(71, None)
        out = os.path.join(self.tmp.name, "out-allow-correct")
        code, logs = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                apply_usernames=("us_71",),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(STATE.patches, [])
        self.assertIsNone(STATE.users["us_71"]["hwidDeviceLimit"])
        plan = self._load_plan(out)
        self.assertEqual(plan[0]["classification"], "already_correct")
        summary = self._load_summary(out)
        self.assertEqual(summary["apply"]["requested"], 0)
        self.assertEqual(summary["apply"]["applied"], 0)
        self.assertIn("allow-list=1", logs)

    def test_allow_listed_invalid_and_missing_are_not_patched(self) -> None:
        self._add_catalog(80, -1)
        self._add_user_service(81, 80)
        self._add_user(81, 0)
        self._add_catalog(81, "ABSENT")
        self._add_user_service(82, 81)
        out = os.path.join(self.tmp.name, "out-allow-invalid-missing")
        code, _ = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                apply_usernames=("us_81", "us_82"),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(STATE.patches, [])
        self.assertEqual(STATE.users["us_81"]["hwidDeviceLimit"], 0)
        plan = {row["username"]: row for row in self._load_plan(out)}
        self.assertEqual(plan["us_81"]["classification"], "invalid_shm_setting")
        self.assertEqual(plan["us_82"]["classification"], "missing_in_remnawave")
        summary = self._load_summary(out)
        self.assertEqual(summary["apply"]["requested"], 0)
        self.assertEqual(summary["apply"]["applied"], 0)

    def test_state_drift_between_plan_and_apply_blocks_patch(self) -> None:
        self._add_catalog(90, "ABSENT")
        self._add_user_service(91, 90)
        self._add_user(91, 0)
        STATE.drift_hwid_after_plan["us_91"] = 5
        out = os.path.join(self.tmp.name, "out-drift")
        code, logs = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                apply_usernames=("us_91",),
            )
        )
        self.assertEqual(code, 1)
        self.assertEqual(STATE.patches, [])
        self.assertEqual(STATE.users["us_91"]["hwidDeviceLimit"], 5)
        plan = self._load_plan(out)
        self.assertEqual(plan[0]["classification"], "needs_reset_to_panel_default")
        self.assertEqual(plan[0]["current_hwid_device_limit"], 0)
        with open(os.path.join(out, "errors.json"), encoding="utf-8") as fh:
            errors = json.load(fh)
        self.assertEqual(errors[0]["stage"], "precheck")
        self.assertIn("drift", errors[0]["error"])
        self.assertIn("us_91", errors[0]["error"] + errors[0]["username"])
        self.assertEqual(self._load_summary(out)["apply"]["applied"], 0)
        self.assertIn("Apply stopped", logs)

    def test_config_from_args_requires_apply_username(self) -> None:
        env = {
            "SHM_PASSWORD": SHM_PASSWORD,
            "REMNAWAVE_TOKEN": RW_TOKEN,
        }
        args = rec.parse_args(
            [
                "--shm-base-url",
                "https://shm.test",
                "--shm-login",
                "admin",
                "--shm-password-env",
                "SHM_PASSWORD",
                "--remnawave-panel-url",
                "https://panel.test",
                "--remnawave-token-env",
                "REMNAWAVE_TOKEN",
                "--output",
                "/tmp/out",
                "--apply",
                "--confirm",
                rec.CONFIRM_PHRASE,
            ]
        )
        with self.assertRaises(rec.FatalError) as ctx:
            rec.config_from_args(args, environ=env)
        self.assertIn("--apply-username", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
