#!/usr/bin/env python3
"""Tests for scripts/reset_traffic_migration.py."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

from tests.proxy_env import DisableEnvProxiesMixin, snapshot_proxy_env  # noqa: E402

import reset_traffic_migration as rst  # noqa: E402
import reconcile_traffic_limits as rec  # noqa: E402


SHM_PASSWORD = "super-secret-shm-password-xyz"
RW_TOKEN = "super-secret-remnawave-token-abc"

STANDARD_BYTES = 322122547200
WRONG_BYTES = 107374182400
CUTOFF_RAW = "2026-09-10T18:00:00Z"
CUTOFF = datetime(2026, 9, 10, 18, 0, tzinfo=timezone.utc)
CREATED_BEFORE = "2026-01-01T00:00:00.000Z"
CREATED_AFTER = "2026-09-11T00:00:00.000Z"
CREATED_EQ_CUTOFF = "2026-09-10T18:00:00Z"
LAST_RESET_BEFORE = "2026-08-01T00:00:00.000Z"
LAST_RESET_EQ = "2026-09-10T18:00:00Z"
LAST_RESET_AFTER = "2026-09-10T19:00:00.000Z"
POST_RESET_AT = "2026-09-10T18:00:05.000Z"


def _extract(bytes_raw: Any = STANDARD_BYTES, strategy_raw: Any = "MONTH"):
    remnawave: Dict[str, Any] = {}
    if bytes_raw != "ABSENT":
        remnawave["traffic_limit_bytes"] = bytes_raw
    if strategy_raw != "ABSENT":
        remnawave["traffic_limit_strategy"] = strategy_raw
    present = bool(remnawave)
    return rec.TrafficShmExtract(
        remnawave_present=present,
        bytes_key_present=bytes_raw != "ABSENT",
        strategy_key_present=strategy_raw != "ABSENT",
        bytes_raw=None if bytes_raw == "ABSENT" else bytes_raw,
        strategy_raw=None if strategy_raw == "ABSENT" else strategy_raw,
    )


def _state(
    *,
    user_id: int = 77,
    bytes_value: Any = STANDARD_BYTES,
    strategy: Any = "MONTH",
    status: str = "ACTIVE",
    used: Any = 50 * rst.GIB,
    created_at: Optional[str] = CREATED_BEFORE,
    last_reset: Optional[str] = None,
) -> rst.ResetUserState:
    return rst.ResetUserState(
        numeric_id=user_id,
        traffic_limit_bytes=bytes_value,
        traffic_limit_strategy=strategy,
        status=status,
        used_traffic_bytes=used,
        created_at=created_at,
        last_traffic_reset_at=last_reset,
        expire_at="2026-12-31T00:00:00.000Z",
        hwid_device_limit=2,
        external_squad_uuid="ext-1",
        active_internal_squads=["squad-1"],
    )


def _classify(
    extract: rec.TrafficShmExtract = None,
    fetch_kind: str = "ok",
    remna: Optional[rst.ResetUserState] = None,
    error_message: Optional[str] = None,
) -> rst.PlanRow:
    return rst.classify_reset_row(
        user_service_id=1,
        username="us_1",
        service_id=3,
        service_name="1 месяц",
        category="vpn-mz-test",
        cutoff=CUTOFF,
        cutoff_raw=CUTOFF_RAW,
        extract=_extract() if extract is None else extract,
        fetch_kind=fetch_kind,
        remna=_state() if remna is None and fetch_kind == "ok" else remna,
        error_message=error_message,
    )


class ParseAndClassifyTests(unittest.TestCase):
    def test_parse_tests_do_not_touch_proxy_env(self) -> None:
        before = snapshot_proxy_env()
        rst.parse_migration_cutoff(CUTOFF_RAW)
        _classify()
        self.assertEqual(snapshot_proxy_env(), before)

    def test_cutoff_required_and_utc(self) -> None:
        parsed = rst.parse_migration_cutoff(CUTOFF_RAW)
        self.assertEqual(parsed, CUTOFF)
        with self.assertRaises(rst.FatalError):
            rst.parse_migration_cutoff("")
        with self.assertRaises(rst.FatalError):
            rst.parse_migration_cutoff("2026-09-10T18:00:00")

    def test_shm_no_reset_blocked(self) -> None:
        row = _classify(extract=_extract(STANDARD_BYTES, "NO_RESET"))
        self.assertEqual(row.classification, rst.CLASS_SERVICE_NOT_MONTH)
        self.assertFalse(row.eligible_for_reset)
        self.assertTrue(row.potential_after_month_migration)

    def test_shm_month_remna_no_reset_blocked(self) -> None:
        row = _classify(remna=_state(strategy="NO_RESET"))
        self.assertEqual(row.classification, rst.CLASS_REMNA_NOT_MONTH)
        self.assertTrue(row.potential_after_month_migration)

    def test_both_month_eligible(self) -> None:
        row = _classify()
        self.assertEqual(row.classification, rst.CLASS_ELIGIBLE)
        self.assertTrue(row.eligible_for_reset)
        self.assertFalse(row.potential_after_month_migration)

    def test_wrong_shm_limit(self) -> None:
        row = _classify(extract=_extract(WRONG_BYTES, "MONTH"))
        self.assertEqual(row.classification, rst.CLASS_SERVICE_WRONG_LIMIT)
        self.assertFalse(row.potential_after_month_migration)

    def test_wrong_remna_limit(self) -> None:
        row = _classify(remna=_state(bytes_value=WRONG_BYTES))
        self.assertEqual(row.classification, rst.CLASS_REMNA_WRONG_LIMIT)

    def test_disabled_wrong_limit_becomes_eligible_after_policy_reconcile(self) -> None:
        """Live LOW case: DISABLED + trafficLimitBytes=0 is not a reset-tool bug.

        After SHM is MONTH/300 GiB and reconcile_traffic_limits PATCHes
        Remnawave to the same policy, the migration classifier must
        treat that user as eligible_for_reset. Reset stays after policy.
        """
        before = _classify(
            extract=_extract(STANDARD_BYTES, "MONTH"),
            remna=_state(status="DISABLED", bytes_value=0, strategy="NO_RESET"),
        )
        self.assertEqual(before.classification, rst.CLASS_REMNA_WRONG_LIMIT)
        self.assertFalse(before.eligible_for_reset)
        after = _classify(
            extract=_extract(STANDARD_BYTES, "MONTH"),
            remna=_state(
                status="DISABLED",
                bytes_value=STANDARD_BYTES,
                strategy="MONTH",
            ),
        )
        self.assertEqual(after.classification, rst.CLASS_ELIGIBLE)
        self.assertTrue(after.eligible_for_reset)
        self.assertEqual(after.remna_status, "DISABLED")

    def test_missing_user(self) -> None:
        row = _classify(fetch_kind="missing", remna=None)
        self.assertEqual(row.classification, rst.CLASS_MISSING)
        self.assertFalse(row.eligible_for_reset)

    def test_active_eligible(self) -> None:
        row = _classify(remna=_state(status="ACTIVE"))
        self.assertEqual(row.classification, rst.CLASS_ELIGIBLE)
        self.assertTrue(row.eligible_for_reset)

    def test_disabled_eligible(self) -> None:
        row = _classify(remna=_state(status="DISABLED"))
        self.assertEqual(row.classification, rst.CLASS_ELIGIBLE)
        self.assertTrue(row.eligible_for_reset)
        self.assertEqual(row.remna_status, "DISABLED")

    def test_limited_expired_unknown_blocked(self) -> None:
        for status in ("LIMITED", "EXPIRED", "UNKNOWN"):
            with self.subTest(status=status):
                row = _classify(remna=_state(status=status))
                self.assertEqual(row.classification, rst.CLASS_INACTIVE)
                self.assertFalse(row.eligible_for_reset)

    def test_disabled_created_after_cutoff_not_eligible(self) -> None:
        row = _classify(remna=_state(status="DISABLED", created_at=CREATED_AFTER))
        self.assertEqual(row.classification, rst.CLASS_CREATED_AFTER)
        self.assertFalse(row.eligible_for_reset)

    def test_disabled_already_reset_since_cutoff_not_eligible(self) -> None:
        row = _classify(remna=_state(status="DISABLED", last_reset=LAST_RESET_AFTER))
        self.assertEqual(row.classification, rst.CLASS_ALREADY_RESET)
        self.assertFalse(row.eligible_for_reset)

    def test_disabled_wrong_strategy_still_blocked(self) -> None:
        row = _classify(
            extract=_extract(STANDARD_BYTES, "NO_RESET"),
            remna=_state(status="DISABLED", strategy="NO_RESET"),
        )
        self.assertEqual(row.classification, rst.CLASS_SERVICE_NOT_MONTH)
        self.assertTrue(row.potential_after_month_migration)

    def test_verify_disabled_stays_disabled(self) -> None:
        pre = _state(status="DISABLED", last_reset=LAST_RESET_BEFORE)
        post = _state(
            status="DISABLED",
            used=123,
            last_reset=POST_RESET_AT,
        )
        self.assertIsNone(rst._verify_reset_state(pre, post, CUTOFF))

    def test_verify_disabled_to_active_is_critical(self) -> None:
        pre = _state(status="DISABLED", last_reset=LAST_RESET_BEFORE)
        post = _state(status="ACTIVE", used=123, last_reset=POST_RESET_AT)
        err = rst._verify_reset_state(pre, post, CUTOFF)
        self.assertIsNotNone(err)
        self.assertIn("CRITICAL", err or "")
        self.assertIn("DISABLED became ACTIVE", err or "")

    def test_verify_active_to_disabled_fails(self) -> None:
        pre = _state(status="ACTIVE", last_reset=LAST_RESET_BEFORE)
        post = _state(status="DISABLED", used=123, last_reset=POST_RESET_AT)
        err = rst._verify_reset_state(pre, post, CUTOFF)
        self.assertIsNotNone(err)
        self.assertIn("status changed unexpectedly", err or "")

    def test_created_after_and_equal_cutoff(self) -> None:
        self.assertEqual(
            _classify(remna=_state(created_at=CREATED_AFTER)).classification,
            rst.CLASS_CREATED_AFTER,
        )
        self.assertEqual(
            _classify(remna=_state(created_at=CREATED_EQ_CUTOFF)).classification,
            rst.CLASS_CREATED_AFTER,
        )

    def test_last_reset_rules(self) -> None:
        self.assertEqual(
            _classify(remna=_state(last_reset=None)).classification,
            rst.CLASS_ELIGIBLE,
        )
        self.assertEqual(
            _classify(remna=_state(last_reset=LAST_RESET_BEFORE)).classification,
            rst.CLASS_ELIGIBLE,
        )
        self.assertEqual(
            _classify(remna=_state(last_reset=LAST_RESET_EQ)).classification,
            rst.CLASS_ALREADY_RESET,
        )
        self.assertEqual(
            _classify(remna=_state(last_reset=LAST_RESET_AFTER)).classification,
            rst.CLASS_ALREADY_RESET,
        )

    def test_invalid_shm_strategy(self) -> None:
        row = _classify(extract=_extract(STANDARD_BYTES, "MONTH_ROLLING"))
        self.assertEqual(row.classification, rst.CLASS_INVALID)
        self.assertFalse(row.eligible_for_reset)

    def test_apply_without_scope_refused(self) -> None:
        env = {"SHM_PASSWORD": SHM_PASSWORD, "REMNAWAVE_TOKEN": RW_TOKEN}
        args = rst.parse_args(
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
                "--migration-cutoff",
                CUTOFF_RAW,
                "--apply",
                "--confirm",
                rst.CONFIRM_PHRASE,
            ]
        )
        with self.assertRaises(rst.FatalError) as ctx:
            rst.config_from_args(args, environ=env)
        self.assertIn("--category or --service-id", str(ctx.exception))

    def test_apply_without_cutoff_refused(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(sys, "stderr", stderr):
            with self.assertRaises(SystemExit):
                rst.parse_args(
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
                        "--service-id",
                        "3",
                        "--apply",
                        "--confirm",
                        rst.CONFIRM_PHRASE,
                    ]
                )
        self.assertIn("--migration-cutoff", stderr.getvalue())

    def test_lock_file_cli_default_and_custom(self) -> None:
        common = [
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
            "--migration-cutoff",
            CUTOFF_RAW,
            "--service-id",
            "3",
        ]
        default_args = rst.parse_args(common)
        self.assertEqual(default_args.lock_file, rst.DEFAULT_APPLY_LOCK_FILE)
        custom_args = rst.parse_args(common + ["--lock-file", "/tmp/custom-reset.lock"])
        self.assertEqual(custom_args.lock_file, "/tmp/custom-reset.lock")
        env = {"SHM_PASSWORD": SHM_PASSWORD, "REMNAWAVE_TOKEN": RW_TOKEN}
        cfg = rst.config_from_args(custom_args, environ=env)
        self.assertEqual(cfg.lock_file, "/tmp/custom-reset.lock")
        metadata = rst.build_apply_lock_metadata(cfg)
        blob = json.dumps(metadata)
        self.assertNotIn(SHM_PASSWORD, blob)
        self.assertNotIn(RW_TOKEN, blob)
        self.assertNotIn("admin", blob)
        self.assertEqual(metadata["migration_cutoff"], CUTOFF_RAW)
        self.assertEqual(metadata["service_ids"], ["3"])

    def test_empty_lock_file_refused(self) -> None:
        env = {"SHM_PASSWORD": SHM_PASSWORD, "REMNAWAVE_TOKEN": RW_TOKEN}
        args = rst.parse_args(
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
                "--migration-cutoff",
                CUTOFF_RAW,
                "--service-id",
                "3",
                "--lock-file",
                "   ",
            ]
        )
        with self.assertRaises(rst.FatalError) as ctx:
            rst.config_from_args(args, environ=env)
        self.assertIn("--lock-file", str(ctx.exception))

    def test_source_has_no_patch_call(self) -> None:
        text = (SCRIPTS / "reset_traffic_migration.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r'request\(\s*[\'"]PATCH[\'"]', text))
        self.assertIsNone(re.search(r'method\s*=\s*[\'"]PATCH[\'"]', text))
        self.assertIn("PATCH must not be used", text)
        self.assertIn("/actions/reset-traffic", text)
        self.assertIn("fcntl.flock", text)
        self.assertIn("LOCK_NB", text)

    def test_reconcile_still_never_resets(self) -> None:
        text = (SCRIPTS / "reconcile_traffic_limits.py").read_text(encoding="utf-8")
        self.assertIn("reset-traffic URL must not be used", text)
        self.assertEqual(
            rec.classify_traffic(
                target_bytes=STANDARD_BYTES,
                target_strategy="MONTH",
                current_bytes=STANDARD_BYTES,
                current_strategy="NO_RESET",
            ),
            rec.CLASS_NEEDS_STRATEGY,
        )
        payload = rec.build_traffic_patch_payload(11, STANDARD_BYTES, "MONTH")
        self.assertEqual(
            set(payload.keys()),
            {"id", "trafficLimitBytes", "trafficLimitStrategy"},
        )


class MockState:
    def __init__(self) -> None:
        self.shm_services: List[Dict[str, Any]] = []
        self.catalog: List[Dict[str, Any]] = []
        self.users: Dict[str, Dict[str, Any]] = {}
        self.resets: List[str] = []
        self.patches: List[Dict[str, Any]] = []
        self.requests: List[Tuple[str, str]] = []
        self.auth_bodies: List[Dict[str, Any]] = []
        self.get_user_hits: Dict[str, int] = {}
        self.catalog_cycles = 0
        self.drift_service_strategy: Optional[str] = None
        self.drift_remna_strategy: Dict[str, str] = {}
        self.drift_status: Dict[str, str] = {}
        self.change_status_after_reset: Dict[str, str] = {}
        self.post_reset_used = 123
        self.lock = threading.Lock()

    def reset(self) -> None:
        with self.lock:
            self.shm_services.clear()
            self.catalog.clear()
            self.users.clear()
            self.resets.clear()
            self.patches.clear()
            self.requests.clear()
            self.auth_bodies.clear()
            self.get_user_hits.clear()
            self.catalog_cycles = 0
            self.drift_service_strategy = None
            self.drift_remna_strategy.clear()
            self.drift_status.clear()
            self.change_status_after_reset.clear()
            self.post_reset_used = 123


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

    def _user_payload(self, user: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "response": {
                "id": user["id"],
                "status": user.get("status", "ACTIVE"),
                "trafficLimitBytes": user.get("trafficLimitBytes"),
                "trafficLimitStrategy": user.get("trafficLimitStrategy"),
                "expireAt": user.get("expireAt", "2026-12-31T00:00:00.000Z"),
                "hwidDeviceLimit": user.get("hwidDeviceLimit", 2),
                "externalSquadUuid": user.get("externalSquadUuid", "ext-1"),
                "activeInternalSquads": user.get("activeInternalSquads", ["squad-1"]),
                "createdAt": user.get("createdAt", CREATED_BEFORE),
                "lastTrafficResetAt": user.get("lastTrafficResetAt"),
                "userTraffic": {
                    "usedTrafficBytes": user.get("usedTrafficBytes", 0),
                    "lifetimeUsedTrafficBytes": user.get(
                        "lifetimeUsedTrafficBytes", 0
                    ),
                },
            }
        }

    def _username_for_id(self, user_id: Any) -> str:
        with STATE.lock:
            for username, user in STATE.users.items():
                if user.get("id") == user_id:
                    return username
        return ""

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        with STATE.lock:
            STATE.requests.append(("POST", parsed.path))
        if parsed.path == "/shm/user/auth.cgi":
            body = json.loads(self._read_raw().decode("utf-8") or "null") or {}
            with STATE.lock:
                STATE.auth_bodies.append(body)
            self._send(200, {"session_id": "test-session"})
            return
        if parsed.path.endswith("/actions/reset-traffic"):
            user_id = int(parsed.path.split("/")[3])
            username = self._username_for_id(user_id)
            with STATE.lock:
                STATE.resets.append(parsed.path)
                user = STATE.users.get(username)
                if user is None:
                    self._send(404, {"message": "User not found"})
                    return
                user["lastTrafficResetAt"] = POST_RESET_AT
                user["usedTrafficBytes"] = STATE.post_reset_used
                if username in STATE.change_status_after_reset:
                    user["status"] = STATE.change_status_after_reset[username]
            self._send(200, {"response": {"ok": True}})
            return
        self._send(404, {"message": "not found"})

    def do_PATCH(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        with STATE.lock:
            STATE.requests.append(("PATCH", parsed.path))
            STATE.patches.append({"path": parsed.path})
        self._send(500, {"message": "PATCH must not be used by reset tool"})

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        with STATE.lock:
            STATE.requests.append(("GET", parsed.path))

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
                if offset == 0:
                    STATE.catalog_cycles += 1
                    if STATE.catalog_cycles >= 2 and STATE.drift_service_strategy:
                        for item in STATE.catalog:
                            settings = item.get("config", {}).get("remnawave", {})
                            settings["traffic_limit_strategy"] = (
                                STATE.drift_service_strategy
                            )
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
                hits = STATE.get_user_hits[username]
                if user is not None and hits >= 2:
                    if username in STATE.drift_remna_strategy:
                        user["trafficLimitStrategy"] = STATE.drift_remna_strategy[
                            username
                        ]
                    if username in STATE.drift_status:
                        user["status"] = STATE.drift_status[username]
            if user is None:
                self._send(404, {"message": "User not found"})
                return
            self._send(200, self._user_payload(user))
            return

        self._send(404, {"message": "not found"})


def start_server() -> Tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


class ResetMigrationTests(DisableEnvProxiesMixin, unittest.TestCase):
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

    def _cfg(self, output: str, **kwargs: Any) -> rst.ResetConfig:
        values = dict(
            shm_base_url=self.base_url,
            shm_login="admin",
            shm_password=SHM_PASSWORD,
            remnawave_panel_url=self.base_url,
            remnawave_token=RW_TOKEN,
            output=output,
            migration_cutoff_raw=CUTOFF_RAW,
            migration_cutoff=CUTOFF,
            service_ids=("3",),
            page_size=250,
            request_delay_ms=0,
            apply=False,
            confirm=None,
            apply_usernames=(),
            lock_file=os.path.join(self.tmp.name, "apply.lock"),
            http_timeout=5,
            verify_retry_attempts=5,
            verify_retry_delay_sec=0,
        )
        values.update(kwargs)
        return rst.ResetConfig(**values)

    def _add_catalog(
        self,
        service_id: int = 3,
        *,
        bytes_value: Any = STANDARD_BYTES,
        strategy: Any = "MONTH",
        category: str = "vpn-mz-test",
        name: str = "Standard",
    ) -> None:
        remnawave: Dict[str, Any] = {"internal_squad_name": "Some-Squad"}
        if bytes_value != "ABSENT":
            remnawave["traffic_limit_bytes"] = bytes_value
        if strategy != "ABSENT":
            remnawave["traffic_limit_strategy"] = strategy
        STATE.catalog.append(
            {
                "service_id": service_id,
                "name": name,
                "category": category,
                "config": {"remnawave": remnawave},
            }
        )

    def _add_user_service(
        self,
        user_service_id: int,
        service_id: int = 3,
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
        self,
        user_service_id: int,
        *,
        bytes_value: Any = STANDARD_BYTES,
        strategy: str = "MONTH",
        used: int = 50 * rst.GIB,
        status: str = "ACTIVE",
        created_at: str = CREATED_BEFORE,
        last_reset: Optional[str] = None,
    ) -> int:
        username = f"us_{user_service_id}"
        user_id = 1000 + user_service_id
        STATE.users[username] = {
            "id": user_id,
            "trafficLimitBytes": bytes_value,
            "trafficLimitStrategy": strategy,
            "usedTrafficBytes": used,
            "lifetimeUsedTrafficBytes": used,
            "status": status,
            "createdAt": created_at,
            "lastTrafficResetAt": last_reset,
            "expireAt": "2026-12-31T00:00:00.000Z",
            "hwidDeviceLimit": 2,
            "externalSquadUuid": "ext-1",
            "activeInternalSquads": ["squad-1"],
        }
        return user_id

    def _run(self, cfg: rst.ResetConfig) -> Tuple[int, str]:
        buf = io.StringIO()
        with mock.patch.object(rst, "log", lambda msg: buf.write(msg + "\n")):
            client = rst.HttpClient(delay_ms=0, timeout=5)
            code = rst.run(cfg, client=client)
        return code, buf.getvalue()

    def _load_plan(self, output: str) -> List[Dict[str, Any]]:
        with open(os.path.join(output, "plan.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def _load_summary(self, output: str) -> Dict[str, Any]:
        with open(os.path.join(output, "summary.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_dry_run_default_zero_resets(self) -> None:
        self._add_catalog()
        self._add_user_service(1)
        self._add_user(1)
        out = os.path.join(self.tmp.name, "dry")
        code, logs = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        self.assertEqual(STATE.resets, [])
        self.assertEqual(STATE.patches, [])
        self.assertIn("0 reset-traffic", logs)
        self.assertEqual(self._load_plan(out)[0]["classification"], rst.CLASS_ELIGIBLE)

    def test_wrong_confirm_refused(self) -> None:
        self._add_catalog()
        self._add_user_service(1)
        self._add_user(1)
        out = os.path.join(self.tmp.name, "bad-confirm")
        code, logs = self._run(
            self._cfg(out, apply=True, confirm="WRONG")
        )
        self.assertEqual(code, 1)
        self.assertEqual(STATE.resets, [])
        self.assertIn("RESET_MONTHLY_TRAFFIC_MIGRATION", logs)

    def test_current_live_no_reset_is_blocked(self) -> None:
        self._add_catalog(strategy="NO_RESET")
        self._add_user_service(1)
        self._add_user(1, strategy="NO_RESET")
        out = os.path.join(self.tmp.name, "live-now")
        code, _ = self._run(self._cfg(out, apply=True, confirm=rst.CONFIRM_PHRASE))
        self.assertEqual(code, 0)
        self.assertEqual(STATE.resets, [])
        row = self._load_plan(out)[0]
        self.assertEqual(row["classification"], rst.CLASS_SERVICE_NOT_MONTH)
        self.assertTrue(row["potential_after_month_migration"])
        summary = self._load_summary(out)
        self.assertEqual(summary["eligible_for_reset"], 0)
        self.assertEqual(summary["potential_after_month_migration"], 1)

    def test_allow_list_limits_mutations(self) -> None:
        self._add_catalog()
        self._add_user_service(1)
        self._add_user_service(2)
        self._add_user(1)
        self._add_user(2)
        out = os.path.join(self.tmp.name, "allow")
        code, _ = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rst.CONFIRM_PHRASE,
                apply_usernames=("us_1",),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.resets), 1)
        self.assertTrue(STATE.resets[0].endswith("/api/users/1001/actions/reset-traffic"))
        self.assertEqual(STATE.patches, [])

    def test_allow_list_does_not_widen_service_scope(self) -> None:
        self._add_catalog(3)
        self._add_catalog(4, name="Other")
        self._add_user_service(1, 3)
        self._add_user_service(2, 4)
        self._add_user(1)
        self._add_user(2)
        out = os.path.join(self.tmp.name, "scope")
        code, _ = self._run(
            self._cfg(
                out,
                service_ids=("3",),
                apply=True,
                confirm=rst.CONFIRM_PHRASE,
                apply_usernames=("us_1", "us_2"),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(self._load_plan(out)), 1)
        self.assertEqual(self._load_plan(out)[0]["username"], "us_1")
        self.assertEqual(len(STATE.resets), 1)
        self.assertIn("/1001/", STATE.resets[0])

    def test_apply_resets_once_and_preserves_config(self) -> None:
        self._add_catalog()
        self._add_user_service(1)
        self._add_user(1, used=20 * rst.GIB)
        out = os.path.join(self.tmp.name, "apply")
        code, _ = self._run(
            self._cfg(out, apply=True, confirm=rst.CONFIRM_PHRASE)
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.resets), 1)
        self.assertEqual(STATE.patches, [])
        self.assertFalse(any(method == "PATCH" for method, _ in STATE.requests))
        user = STATE.users["us_1"]
        self.assertEqual(user["trafficLimitBytes"], STANDARD_BYTES)
        self.assertEqual(user["trafficLimitStrategy"], "MONTH")
        self.assertEqual(user["status"], "ACTIVE")
        self.assertEqual(user["lastTrafficResetAt"], POST_RESET_AT)
        self.assertEqual(user["usedTrafficBytes"], 123)
        applied = json.loads(
            Path(out, "applied.json").read_text(encoding="utf-8")
        )
        self.assertEqual(applied[0]["post_used_traffic_bytes"], 123)
        self.assertEqual(applied[0]["post_last_traffic_reset_at"], POST_RESET_AT)

    def test_repeated_run_same_cutoff_zero_second_reset(self) -> None:
        self._add_catalog()
        self._add_user_service(1)
        self._add_user(1, used=20 * rst.GIB)
        first = os.path.join(self.tmp.name, "first")
        code, _ = self._run(
            self._cfg(first, apply=True, confirm=rst.CONFIRM_PHRASE)
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.resets), 1)
        second = os.path.join(self.tmp.name, "second")
        code, _ = self._run(
            self._cfg(second, apply=True, confirm=rst.CONFIRM_PHRASE)
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.resets), 1)
        self.assertEqual(
            self._load_plan(second)[0]["classification"],
            rst.CLASS_ALREADY_RESET,
        )

    def test_service_strategy_changed_during_precheck(self) -> None:
        self._add_catalog()
        self._add_user_service(1)
        self._add_user(1)
        STATE.drift_service_strategy = "NO_RESET"
        out = os.path.join(self.tmp.name, "svc-drift")
        code, _ = self._run(
            self._cfg(out, apply=True, confirm=rst.CONFIRM_PHRASE)
        )
        self.assertEqual(code, 0)
        self.assertEqual(STATE.resets, [])
        skipped = json.loads(
            Path(out, "apply_skipped.json").read_text(encoding="utf-8")
        )
        self.assertEqual(skipped[0]["reason"], rst.CLASS_SERVICE_NOT_MONTH)

    def test_remna_strategy_changed_during_precheck(self) -> None:
        self._add_catalog()
        self._add_user_service(1)
        self._add_user(1)
        STATE.drift_remna_strategy["us_1"] = "NO_RESET"
        out = os.path.join(self.tmp.name, "remna-drift")
        code, _ = self._run(
            self._cfg(out, apply=True, confirm=rst.CONFIRM_PHRASE)
        )
        self.assertEqual(code, 0)
        self.assertEqual(STATE.resets, [])
        skipped = json.loads(
            Path(out, "apply_skipped.json").read_text(encoding="utf-8")
        )
        self.assertEqual(skipped[0]["reason"], rst.CLASS_REMNA_NOT_MONTH)

    def test_status_changed_during_precheck(self) -> None:
        self._add_catalog()
        self._add_user_service(1)
        self._add_user(1)
        STATE.drift_status["us_1"] = "LIMITED"
        out = os.path.join(self.tmp.name, "status-drift")
        code, _ = self._run(
            self._cfg(out, apply=True, confirm=rst.CONFIRM_PHRASE)
        )
        self.assertEqual(code, 0)
        self.assertEqual(STATE.resets, [])
        skipped = json.loads(
            Path(out, "apply_skipped.json").read_text(encoding="utf-8")
        )
        self.assertEqual(skipped[0]["reason"], rst.CLASS_INACTIVE)

    def test_verify_accepts_nonzero_used_when_reset_advanced(self) -> None:
        self._add_catalog()
        self._add_user_service(1)
        self._add_user(1, used=30 * rst.GIB)
        STATE.post_reset_used = 4096
        out = os.path.join(self.tmp.name, "nonzero")
        code, _ = self._run(
            self._cfg(out, apply=True, confirm=rst.CONFIRM_PHRASE)
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.resets), 1)
        applied = json.loads(Path(out, "applied.json").read_text(encoding="utf-8"))
        self.assertEqual(applied[0]["post_used_traffic_bytes"], 4096)
        self.assertGreater(applied[0]["post_used_traffic_bytes"], 0)

    def test_lowercase_month_normalizes(self) -> None:
        self._add_catalog(strategy="month")
        self._add_user_service(1)
        self._add_user(1)
        out = os.path.join(self.tmp.name, "lower")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        self.assertEqual(self._load_plan(out)[0]["classification"], rst.CLASS_ELIGIBLE)

    def test_disabled_reset_once_and_stays_disabled(self) -> None:
        self._add_catalog()
        self._add_user_service(1)
        self._add_user(1, status="DISABLED", used=20 * rst.GIB)
        out = os.path.join(self.tmp.name, "disabled-apply")
        code, _ = self._run(
            self._cfg(out, apply=True, confirm=rst.CONFIRM_PHRASE)
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.resets), 1)
        self.assertTrue(STATE.resets[0].endswith("/api/users/1001/actions/reset-traffic"))
        self.assertEqual(STATE.patches, [])
        self.assertEqual(STATE.users["us_1"]["status"], "DISABLED")
        summary = self._load_summary(out)
        self.assertEqual(summary["eligible_for_reset"], 1)
        self.assertEqual(summary["eligible_active"], 0)
        self.assertEqual(summary["eligible_disabled"], 1)
        applied = json.loads(Path(out, "applied.json").read_text(encoding="utf-8"))
        self.assertEqual(applied[0]["pre_status"], "DISABLED")
        self.assertEqual(applied[0]["post_status"], "DISABLED")

    def test_disabled_to_active_after_reset_stops(self) -> None:
        self._add_catalog()
        self._add_user_service(1)
        self._add_user_service(2)
        self._add_user(1, status="DISABLED", used=20 * rst.GIB)
        self._add_user(2, status="DISABLED", used=15 * rst.GIB)
        STATE.change_status_after_reset["us_1"] = "ACTIVE"
        out = os.path.join(self.tmp.name, "disabled-flip")
        code, _ = self._run(
            self._cfg(out, apply=True, confirm=rst.CONFIRM_PHRASE)
        )
        self.assertEqual(code, 2)
        self.assertEqual(len(STATE.resets), 1)
        errors = json.loads(Path(out, "apply_errors.json").read_text(encoding="utf-8"))
        self.assertEqual(errors[0]["stage"], "verify")
        self.assertIn("CRITICAL", errors[0]["error"])
        self.assertEqual(errors[0]["pre_status"], "DISABLED")
        self.assertEqual(errors[0]["post_status"], "ACTIVE")

    def test_active_to_disabled_after_reset_stops(self) -> None:
        self._add_catalog()
        self._add_user_service(1)
        self._add_user(1, used=20 * rst.GIB)
        STATE.change_status_after_reset["us_1"] = "DISABLED"
        out = os.path.join(self.tmp.name, "active-flip")
        code, _ = self._run(
            self._cfg(out, apply=True, confirm=rst.CONFIRM_PHRASE)
        )
        self.assertEqual(code, 2)
        self.assertEqual(len(STATE.resets), 1)
        errors = json.loads(Path(out, "apply_errors.json").read_text(encoding="utf-8"))
        self.assertIn("status changed unexpectedly", errors[0]["error"])

    def test_repeated_disabled_run_same_cutoff_zero_second_reset(self) -> None:
        self._add_catalog()
        self._add_user_service(1)
        self._add_user(1, status="DISABLED", used=20 * rst.GIB)
        first = os.path.join(self.tmp.name, "disabled-first")
        code, _ = self._run(
            self._cfg(first, apply=True, confirm=rst.CONFIRM_PHRASE)
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.resets), 1)
        self.assertEqual(STATE.users["us_1"]["status"], "DISABLED")
        second = os.path.join(self.tmp.name, "disabled-second")
        code, _ = self._run(
            self._cfg(second, apply=True, confirm=rst.CONFIRM_PHRASE)
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.resets), 1)
        self.assertEqual(
            self._load_plan(second)[0]["classification"],
            rst.CLASS_ALREADY_RESET,
        )
        self.assertEqual(STATE.users["us_1"]["status"], "DISABLED")

    def test_allow_list_works_for_disabled(self) -> None:
        self._add_catalog()
        self._add_user_service(1)
        self._add_user_service(2)
        self._add_user(1, status="DISABLED")
        self._add_user(2, status="DISABLED")
        out = os.path.join(self.tmp.name, "disabled-allow")
        code, _ = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rst.CONFIRM_PHRASE,
                apply_usernames=("us_2",),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.resets), 1)
        self.assertIn("/1002/", STATE.resets[0])
        self.assertEqual(STATE.users["us_1"]["lastTrafficResetAt"], None)
        self.assertEqual(STATE.users["us_2"]["status"], "DISABLED")

    def test_disabled_created_after_cutoff_no_reset(self) -> None:
        self._add_catalog()
        self._add_user_service(1)
        self._add_user(1, status="DISABLED", created_at=CREATED_AFTER)
        out = os.path.join(self.tmp.name, "disabled-created")
        code, _ = self._run(
            self._cfg(out, apply=True, confirm=rst.CONFIRM_PHRASE)
        )
        self.assertEqual(code, 0)
        self.assertEqual(STATE.resets, [])
        self.assertEqual(
            self._load_plan(out)[0]["classification"],
            rst.CLASS_CREATED_AFTER,
        )

    def test_disabled_already_reset_no_second_call(self) -> None:
        self._add_catalog()
        self._add_user_service(1)
        self._add_user(1, status="DISABLED", last_reset=LAST_RESET_AFTER)
        out = os.path.join(self.tmp.name, "disabled-already")
        code, _ = self._run(
            self._cfg(out, apply=True, confirm=rst.CONFIRM_PHRASE)
        )
        self.assertEqual(code, 0)
        self.assertEqual(STATE.resets, [])
        self.assertEqual(
            self._load_plan(out)[0]["classification"],
            rst.CLASS_ALREADY_RESET,
        )

    def test_current_live_disabled_no_reset_is_potential(self) -> None:
        self._add_catalog(strategy="NO_RESET")
        self._add_user_service(1)
        self._add_user(1, status="DISABLED", strategy="NO_RESET")
        out = os.path.join(self.tmp.name, "live-disabled")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        row = self._load_plan(out)[0]
        self.assertEqual(row["classification"], rst.CLASS_SERVICE_NOT_MONTH)
        self.assertTrue(row["potential_after_month_migration"])
        summary = self._load_summary(out)
        self.assertEqual(summary["eligible_for_reset"], 0)
        self.assertEqual(summary["potential_disabled"], 1)
        self.assertEqual(summary["disabled_found"]["count"], 1)

    def test_dry_run_does_not_require_apply_lock(self) -> None:
        self._add_catalog()
        self._add_user_service(1)
        self._add_user(1)
        lock_path = os.path.join(self.tmp.name, "held.lock")
        holder = _hold_lock_subprocess(lock_path)
        self.addCleanup(holder.stop)
        out = os.path.join(self.tmp.name, "dry-while-held")
        code, logs = self._run(self._cfg(out, lock_file=lock_path))
        self.assertEqual(code, 0)
        self.assertEqual(STATE.resets, [])
        self.assertIn("0 reset-traffic", logs)
        summary = self._load_summary(out)
        self.assertFalse(summary["apply_lock_acquired"])
        self.assertIsNone(summary["apply_lock_path"])

    def test_apply_acquires_lock_and_writes_metadata(self) -> None:
        self._add_catalog()
        self._add_user_service(1)
        self._add_user(1)
        lock_path = os.path.join(self.tmp.name, "apply.lock")
        out = os.path.join(self.tmp.name, "locked-apply")
        code, logs = self._run(
            self._cfg(out, apply=True, confirm=rst.CONFIRM_PHRASE, lock_file=lock_path)
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.resets), 1)
        self.assertIn("Apply lock acquired", logs)
        summary = self._load_summary(out)
        self.assertTrue(summary["apply_lock_acquired"])
        self.assertEqual(os.path.abspath(summary["apply_lock_path"]), os.path.abspath(lock_path))
        meta = json.loads(Path(lock_path).read_text(encoding="utf-8"))
        self.assertEqual(meta["migration_cutoff"], CUTOFF_RAW)
        self.assertEqual(meta["service_ids"], ["3"])
        self.assertNotIn(SHM_PASSWORD, json.dumps(meta))
        self.assertNotIn(RW_TOKEN, json.dumps(meta))
        self.assertNotIn("super-secret", Path(lock_path).read_text(encoding="utf-8"))

    def test_concurrent_apply_refused_zero_resets(self) -> None:
        self._add_catalog()
        self._add_user_service(1)
        self._add_user(1)
        lock_path = os.path.join(self.tmp.name, "shared.lock")
        holder = _hold_lock_subprocess(lock_path)
        self.addCleanup(holder.stop)
        out = os.path.join(self.tmp.name, "blocked-apply")
        code, logs = self._run(
            self._cfg(out, apply=True, confirm=rst.CONFIRM_PHRASE, lock_file=lock_path)
        )
        self.assertEqual(code, 1)
        self.assertEqual(STATE.resets, [])
        self.assertEqual(STATE.patches, [])
        self.assertIn(rst.CONCURRENT_APPLY_MESSAGE, logs)
        self.assertFalse(os.path.exists(os.path.join(out, "summary.json")))

    def test_lock_released_after_apply_allows_next(self) -> None:
        self._add_catalog()
        self._add_user_service(1)
        self._add_user(1)
        lock_path = os.path.join(self.tmp.name, "reuse.lock")
        first = os.path.join(self.tmp.name, "first-apply")
        code, _ = self._run(
            self._cfg(first, apply=True, confirm=rst.CONFIRM_PHRASE, lock_file=lock_path)
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.resets), 1)
        second = os.path.join(self.tmp.name, "second-apply")
        code, logs = self._run(
            self._cfg(second, apply=True, confirm=rst.CONFIRM_PHRASE, lock_file=lock_path)
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.resets), 1)
        self.assertNotIn(rst.CONCURRENT_APPLY_MESSAGE, logs)
        self.assertEqual(self._load_plan(second)[0]["classification"], rst.CLASS_ALREADY_RESET)

    def test_exception_inside_apply_releases_lock(self) -> None:
        self._add_catalog()
        self._add_user_service(1)
        self._add_user(1)
        lock_path = os.path.join(self.tmp.name, "exc.lock")
        out = os.path.join(self.tmp.name, "exc-apply")
        with mock.patch.object(rst, "apply_resets", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self._run(
                    self._cfg(
                        out,
                        apply=True,
                        confirm=rst.CONFIRM_PHRASE,
                        lock_file=lock_path,
                    )
                )
        self.assertEqual(STATE.resets, [])
        retry = os.path.join(self.tmp.name, "after-exc")
        code, logs = self._run(
            self._cfg(retry, apply=True, confirm=rst.CONFIRM_PHRASE, lock_file=lock_path)
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.resets), 1)
        self.assertNotIn(rst.CONCURRENT_APPLY_MESSAGE, logs)

    def test_stale_lock_file_without_held_flock_does_not_block(self) -> None:
        self._add_catalog()
        self._add_user_service(1)
        self._add_user(1)
        lock_path = os.path.join(self.tmp.name, "stale.lock")
        Path(lock_path).write_text(
            json.dumps(
                {
                    "pid": 999999,
                    "started_at": "2020-01-01T00:00:00Z",
                    "migration_cutoff": CUTOFF_RAW,
                    "service_ids": ["3"],
                    "categories": [],
                }
            ),
            encoding="utf-8",
        )
        out = os.path.join(self.tmp.name, "stale-apply")
        code, logs = self._run(
            self._cfg(out, apply=True, confirm=rst.CONFIRM_PHRASE, lock_file=lock_path)
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.resets), 1)
        self.assertNotIn(rst.CONCURRENT_APPLY_MESSAGE, logs)

    def test_custom_lock_file_is_used(self) -> None:
        self._add_catalog()
        self._add_user_service(1)
        self._add_user(1)
        custom = os.path.join(self.tmp.name, "nested", "custom.lock")
        out = os.path.join(self.tmp.name, "custom-lock-apply")
        code, logs = self._run(
            self._cfg(out, apply=True, confirm=rst.CONFIRM_PHRASE, lock_file=custom)
        )
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(custom))
        self.assertIn(os.path.abspath(custom), logs)

    def test_incident_overlapping_apply_then_cutoff_retry(self) -> None:
        self._add_catalog()
        self._add_user_service(1)
        self._add_user(1)
        lock_path = os.path.join(self.tmp.name, "incident.lock")
        holder = _hold_lock_subprocess(lock_path)
        self.addCleanup(holder.stop)
        blocked = os.path.join(self.tmp.name, "incident-b")
        code, logs = self._run(
            self._cfg(
                blocked,
                apply=True,
                confirm=rst.CONFIRM_PHRASE,
                lock_file=lock_path,
                apply_usernames=("us_1",),
            )
        )
        self.assertEqual(code, 1)
        self.assertEqual(STATE.resets, [])
        self.assertIn("Refusing concurrent apply", logs)
        holder.stop()
        STATE.users["us_1"]["lastTrafficResetAt"] = LAST_RESET_AFTER
        retry = os.path.join(self.tmp.name, "incident-retry")
        code, _ = self._run(
            self._cfg(
                retry,
                apply=True,
                confirm=rst.CONFIRM_PHRASE,
                lock_file=lock_path,
                apply_usernames=("us_1",),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(STATE.resets, [])
        self.assertEqual(self._load_plan(retry)[0]["classification"], rst.CLASS_ALREADY_RESET)


class _HeldLock:
    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self.proc = proc

    def stop(self) -> None:
        if self.proc.poll() is None:
            if self.proc.stdin is not None:
                try:
                    self.proc.stdin.write(b"release\n")
                    self.proc.stdin.close()
                except BrokenPipeError:
                    pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                stream.close()


def _hold_lock_subprocess(lock_path: str) -> _HeldLock:
    script = (
        "import fcntl, os, sys\n"
        "path = sys.argv[1]\n"
        "os.makedirs(os.path.dirname(path) or '.', exist_ok=True)\n"
        "fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "sys.stdout.write('LOCKED\\n')\n"
        "sys.stdout.flush()\n"
        "sys.stdin.readline()\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script, lock_path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if line.strip() != b"LOCKED":
        stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        proc.kill()
        raise RuntimeError(f"lock holder failed: {line!r} {stderr}")
    if proc.stdout is not None:
        proc.stdout.close()
    if proc.stderr is not None:
        proc.stderr.close()
    return _HeldLock(proc)


if __name__ == "__main__":
    unittest.main()
