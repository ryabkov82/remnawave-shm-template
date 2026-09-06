#!/usr/bin/env python3
"""Tests for scripts/reconcile_traffic_limits.py."""

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

import reconcile_traffic_limits as rec  # noqa: E402


SHM_PASSWORD = "super-secret-shm-password-xyz"
RW_TOKEN = "super-secret-remnawave-token-abc"

STANDARD_BYTES = 322122547200
STANDARD_STRATEGY = "NO_RESET"
MONTH_BYTES = 107374182400
MONTH_STRATEGY = "MONTH"
GIB = 1024 ** 3


def _extract(bytes_raw: Any = "ABSENT", strategy_raw: Any = "ABSENT") -> rec.TrafficShmExtract:
    remnawave: Dict[str, Any] = {}
    if bytes_raw != "ABSENT":
        remnawave["traffic_limit_bytes"] = bytes_raw
    if strategy_raw != "ABSENT":
        remnawave["traffic_limit_strategy"] = strategy_raw
    present = bool(remnawave) or bytes_raw != "ABSENT" or strategy_raw != "ABSENT"
    if bytes_raw == "ABSENT" and strategy_raw == "ABSENT":
        return rec.TrafficShmExtract(
            remnawave_present=False,
            bytes_key_present=False,
            strategy_key_present=False,
        )
    return rec.TrafficShmExtract(
        remnawave_present=True,
        bytes_key_present=bytes_raw != "ABSENT",
        strategy_key_present=strategy_raw != "ABSENT",
        bytes_raw=None if bytes_raw == "ABSENT" else bytes_raw,
        strategy_raw=None if strategy_raw == "ABSENT" else strategy_raw,
    )


def _state(
    *,
    user_id: int = 11,
    bytes_value: Any = 0,
    strategy: Any = "NO_RESET",
    status: str = "ACTIVE",
    used: Any = 0,
) -> rec.RemnawaveTrafficState:
    return rec.RemnawaveTrafficState(
        numeric_id=user_id,
        traffic_limit_bytes=bytes_value,
        traffic_limit_strategy=strategy,
        status=status,
        used_traffic_bytes=used,
        field_paths={
            "id": "response.id",
            "trafficLimitBytes": "response.trafficLimitBytes",
            "trafficLimitStrategy": "response.trafficLimitStrategy",
            "status": "response.status",
            "usedTrafficBytes": "response.userTraffic.usedTrafficBytes",
        },
    )


class ParseAndClassifyTests(unittest.TestCase):
    def test_parse_tests_do_not_touch_proxy_env(self) -> None:
        before = snapshot_proxy_env()
        rec.parse_shm_traffic_limit_bytes(STANDARD_BYTES)
        rec.parse_shm_traffic_limit_strategy("no_reset")
        rec.classify_traffic(
            target_bytes=STANDARD_BYTES,
            target_strategy=STANDARD_STRATEGY,
            current_bytes=0,
            current_strategy="NO_RESET",
        )
        self.assertEqual(snapshot_proxy_env(), before)

    def test_parse_standard_300_gib_no_reset(self) -> None:
        self.assertEqual(rec.parse_shm_traffic_limit_bytes(STANDARD_BYTES), STANDARD_BYTES)
        self.assertEqual(
            rec.parse_shm_traffic_limit_bytes(str(STANDARD_BYTES)), STANDARD_BYTES
        )
        self.assertEqual(
            rec.parse_shm_traffic_limit_strategy("NO_RESET"), "NO_RESET"
        )
        self.assertEqual(
            rec.parse_shm_traffic_limit_strategy("no_reset"), "NO_RESET"
        )
        self.assertEqual(STANDARD_BYTES / GIB, 300.0)

    def test_parse_100_gib_month(self) -> None:
        self.assertEqual(rec.parse_shm_traffic_limit_bytes(MONTH_BYTES), MONTH_BYTES)
        self.assertEqual(rec.parse_shm_traffic_limit_bytes("107374182400"), MONTH_BYTES)
        self.assertEqual(rec.parse_shm_traffic_limit_strategy("MONTH"), "MONTH")
        self.assertEqual(rec.parse_shm_traffic_limit_strategy("month"), "MONTH")
        self.assertEqual(MONTH_BYTES / GIB, 100.0)

    def test_invalid_bytes(self) -> None:
        for raw in (-1, "-1", 1.5, "1.5", "abc", True, False, 3.0, "1e2"):
            with self.subTest(raw=raw):
                with self.assertRaises(rec.TrafficSettingError):
                    rec.parse_shm_traffic_limit_bytes(raw)

    def test_invalid_strategy(self) -> None:
        for raw in ("YEAR", "never", "", 1, True, None):
            with self.subTest(raw=raw):
                with self.assertRaises(rec.TrafficSettingError):
                    rec.parse_shm_traffic_limit_strategy(raw)  # type: ignore[arg-type]

    def test_month_rolling_is_not_accepted_from_shm(self) -> None:
        with self.assertRaises(rec.TrafficSettingError) as ctx:
            rec.parse_shm_traffic_limit_strategy("MONTH_ROLLING")
        self.assertIn("SHM template does not", str(ctx.exception))

    def test_missing_setting_is_unmanaged(self) -> None:
        row = rec.classify_row(
            user_service_id=1,
            category="vpn-mz-test",
            service_id=9,
            service_name="Legacy",
            extract=_extract(),
            fetch_kind="ok",
            remnawave=_state(),
            error_message=None,
        )
        self.assertEqual(row.classification, rec.CLASS_UNMANAGED)
        self.assertFalse(row.managed)
        self.assertIs(row.target_traffic_limit_bytes, rec.TARGET_UNRESOLVED)
        self.assertNotIn(row.classification, rec.APPLY_CLASSES)

    def test_null_bytes_is_unmanaged(self) -> None:
        row = rec.classify_row(
            user_service_id=1,
            category="vpn-mz-test",
            service_id=9,
            service_name="Legacy",
            extract=_extract(None, "NO_RESET"),
            fetch_kind="ok",
            remnawave=_state(),
            error_message=None,
        )
        self.assertEqual(row.classification, rec.CLASS_UNMANAGED)

    def test_strategy_missing_when_bytes_set_is_invalid(self) -> None:
        row = rec.classify_row(
            user_service_id=1,
            category="vpn-mz-test",
            service_id=9,
            service_name="Standard",
            extract=_extract(STANDARD_BYTES, "ABSENT"),
            fetch_kind="ok",
            remnawave=_state(),
            error_message=None,
        )
        self.assertEqual(row.classification, rec.CLASS_INVALID)
        self.assertTrue(row.managed)
        self.assertIn("traffic_limit_strategy is missing", row.error_message or "")

    def test_already_correct(self) -> None:
        row = rec.classify_row(
            user_service_id=1,
            category="vpn-mz-test",
            service_id=9,
            service_name="Standard",
            extract=_extract(STANDARD_BYTES, STANDARD_STRATEGY),
            fetch_kind="ok",
            remnawave=_state(bytes_value=STANDARD_BYTES, strategy=STANDARD_STRATEGY),
            error_message=None,
        )
        self.assertEqual(row.classification, rec.CLASS_ALREADY_CORRECT)
        self.assertFalse(row.would_be_over_limit_now)

    def test_needs_limit(self) -> None:
        self.assertEqual(
            rec.classify_traffic(
                target_bytes=STANDARD_BYTES,
                target_strategy="NO_RESET",
                current_bytes=0,
                current_strategy="NO_RESET",
            ),
            rec.CLASS_NEEDS_LIMIT,
        )

    def test_needs_strategy(self) -> None:
        self.assertEqual(
            rec.classify_traffic(
                target_bytes=MONTH_BYTES,
                target_strategy="MONTH",
                current_bytes=MONTH_BYTES,
                current_strategy="NO_RESET",
            ),
            rec.CLASS_NEEDS_STRATEGY,
        )

    def test_needs_both(self) -> None:
        self.assertEqual(
            rec.classify_traffic(
                target_bytes=STANDARD_BYTES,
                target_strategy="NO_RESET",
                current_bytes=0,
                current_strategy="MONTH",
            ),
            rec.CLASS_NEEDS_BOTH,
        )

    def test_over_limit_flag(self) -> None:
        row = rec.classify_row(
            user_service_id=1,
            category="vpn-mz-test",
            service_id=9,
            service_name="Standard",
            extract=_extract(STANDARD_BYTES, STANDARD_STRATEGY),
            fetch_kind="ok",
            remnawave=_state(
                bytes_value=0,
                strategy="NO_RESET",
                used=STANDARD_BYTES,
            ),
            error_message=None,
        )
        self.assertEqual(row.classification, rec.CLASS_NEEDS_LIMIT)
        self.assertTrue(row.would_be_over_limit_now)
        report = row.to_report_dict()
        self.assertEqual(report["used_traffic_gib"], 300.0)
        self.assertEqual(report["target_limit_gib"], 300.0)

    def test_over_limit_not_set_when_target_zero(self) -> None:
        row = rec.classify_row(
            user_service_id=1,
            category="vpn-mz-test",
            service_id=9,
            service_name="Unlimited",
            extract=_extract(0, "NO_RESET"),
            fetch_kind="ok",
            remnawave=_state(bytes_value=1, strategy="NO_RESET", used=999),
            error_message=None,
        )
        self.assertFalse(row.would_be_over_limit_now)

    def test_missing_in_remnawave(self) -> None:
        row = rec.classify_row(
            user_service_id=1,
            category="vpn-mz-test",
            service_id=9,
            service_name="Standard",
            extract=_extract(STANDARD_BYTES, STANDARD_STRATEGY),
            fetch_kind="missing",
            remnawave=None,
            error_message=None,
        )
        self.assertEqual(row.classification, rec.CLASS_MISSING)
        self.assertTrue(row.managed)
        self.assertNotIn(row.classification, rec.APPLY_CLASSES)

    def test_patch_payload_contains_only_id_and_traffic_fields(self) -> None:
        payload = rec.build_traffic_patch_payload(7, STANDARD_BYTES, "NO_RESET")
        self.assertEqual(
            payload,
            {
                "id": 7,
                "trafficLimitBytes": STANDARD_BYTES,
                "trafficLimitStrategy": "NO_RESET",
            },
        )
        self.assertEqual(set(payload.keys()), rec.PATCH_KEYS)
        raw = rec.encode_traffic_patch_body(7, STANDARD_BYTES, "NO_RESET")
        decoded = json.loads(raw.decode("utf-8"))
        self.assertEqual(set(decoded.keys()), rec.PATCH_KEYS)
        for forbidden in (
            "usedTrafficBytes",
            "lastTrafficResetAt",
            "status",
            "expireAt",
            "activeInternalSquads",
            "externalSquadUuid",
            "hwidDeviceLimit",
        ):
            self.assertNotIn(forbidden, decoded)

    def test_parse_remnawave_user_uses_nested_user_traffic(self) -> None:
        payload = {
            "response": {
                "id": 42,
                "status": "ACTIVE",
                "trafficLimitBytes": STANDARD_BYTES,
                "trafficLimitStrategy": "NO_RESET",
                "userTraffic": {"usedTrafficBytes": 123, "lifetimeUsedTrafficBytes": 456},
            }
        }
        state = rec.parse_remnawave_user(payload)
        self.assertEqual(state.numeric_id, 42)
        self.assertEqual(state.traffic_limit_bytes, STANDARD_BYTES)
        self.assertEqual(state.traffic_limit_strategy, "NO_RESET")
        self.assertEqual(state.status, "ACTIVE")
        self.assertEqual(state.used_traffic_bytes, 123)
        self.assertEqual(
            state.field_paths["usedTrafficBytes"],
            "response.userTraffic.usedTrafficBytes",
        )

    def test_parse_remnawave_user_legacy_top_level_used_traffic(self) -> None:
        payload = {
            "response": {
                "id": 42,
                "status": "ACTIVE",
                "trafficLimitBytes": 1,
                "trafficLimitStrategy": "DAY",
                "usedTrafficBytes": 77,
            }
        }
        state = rec.parse_remnawave_user(payload)
        self.assertEqual(state.used_traffic_bytes, 77)
        self.assertEqual(state.field_paths["usedTrafficBytes"], "response.usedTrafficBytes")

    def test_apply_unscoped_config_refused(self) -> None:
        env = {"SHM_PASSWORD": SHM_PASSWORD, "REMNAWAVE_TOKEN": RW_TOKEN}
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
        self.assertIn("--category or --service-id", str(ctx.exception))

    def test_select_apply_rows_skips_over_limit_by_default(self) -> None:
        over = rec.classify_row(
            user_service_id=1,
            category="vpn-mz-test",
            service_id=9,
            service_name="Standard",
            extract=_extract(STANDARD_BYTES, STANDARD_STRATEGY),
            fetch_kind="ok",
            remnawave=_state(bytes_value=0, used=STANDARD_BYTES),
            error_message=None,
        )
        under = rec.classify_row(
            user_service_id=2,
            category="vpn-mz-test",
            service_id=9,
            service_name="Standard",
            extract=_extract(STANDARD_BYTES, STANDARD_STRATEGY),
            fetch_kind="ok",
            remnawave=_state(user_id=12, bytes_value=0, used=1),
            error_message=None,
        )
        selected = rec.select_apply_rows([over, under], include_over_limit=False)
        self.assertEqual([row.username for row in selected], ["us_2"])
        included = rec.select_apply_rows([over, under], include_over_limit=True)
        self.assertEqual([row.username for row in included], ["us_1", "us_2"])
        allow_under = rec.select_apply_rows(
            [over, under],
            include_over_limit=False,
            apply_usernames=("us_1", "us_2"),
        )
        self.assertEqual([row.username for row in allow_under], ["us_2"])
        allow_over = rec.select_apply_rows(
            [over, under],
            include_over_limit=True,
            apply_usernames=("us_1",),
        )
        self.assertEqual([row.username for row in allow_over], ["us_1"])

    def test_parse_apply_username_repeatable(self) -> None:
        env = {"SHM_PASSWORD": SHM_PASSWORD, "REMNAWAVE_TOKEN": RW_TOKEN}
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
                "--service-id",
                "3",
                "--apply-username",
                "us_981",
                "--apply-username",
                " us_982 ",
                "--apply-username",
                "us_981",
            ]
        )
        cfg = rec.config_from_args(args, environ=env)
        self.assertEqual(cfg.apply_usernames, ("us_981", "us_982"))
        self.assertEqual(cfg.service_ids, ("3",))

    def test_unscoped_apply_refused_even_with_allow_list(self) -> None:
        env = {"SHM_PASSWORD": SHM_PASSWORD, "REMNAWAVE_TOKEN": RW_TOKEN}
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
                "--apply-username",
                "us_981",
            ]
        )
        with self.assertRaises(rec.FatalError) as ctx:
            rec.config_from_args(args, environ=env)
        self.assertIn("--category or --service-id", str(ctx.exception))

    def test_used_growth_vs_plan_is_safe(self) -> None:
        self.assertIsNone(rec.used_bytes_safe_vs_baseline(100, 110, label="precheck"))
        self.assertIsNone(rec.used_bytes_safe_vs_baseline(100, 100, label="precheck"))

    def test_used_decrease_vs_plan_is_unsafe(self) -> None:
        err = rec.used_bytes_safe_vs_baseline(100, 90, label="precheck")
        self.assertIsNotNone(err)
        self.assertIn("decreased", err or "")

    def _mutation_row(self, used: int = 100) -> rec.PlanRow:
        return rec.classify_row(
            user_service_id=1,
            category="vpn-mz-test",
            service_id=9,
            service_name="Standard",
            extract=_extract(STANDARD_BYTES, STANDARD_STRATEGY),
            fetch_kind="ok",
            remnawave=_state(bytes_value=0, strategy="NO_RESET", used=used),
            error_message=None,
        )

    def test_precheck_allows_used_growth_and_equal(self) -> None:
        row = self._mutation_row(100)
        for live_used in (110, 100):
            live = _state(bytes_value=0, strategy="NO_RESET", used=live_used)
            action, skip, err = rec.precheck_apply_row(
                row, live=live, fetch_kind="ok", fetch_error=None
            )
            self.assertEqual(action, rec.ACTION_PATCH, live_used)
            self.assertIsNone(skip)
            self.assertIsNone(err)

    def test_precheck_refuses_used_decrease(self) -> None:
        row = self._mutation_row(100)
        live = _state(bytes_value=0, strategy="NO_RESET", used=90)
        action, skip, err = rec.precheck_apply_row(
            row, live=live, fetch_kind="ok", fetch_error=None
        )
        self.assertEqual(action, rec.ACTION_ERROR)
        self.assertIsNone(skip)
        self.assertIn("decreased", err or "")

    def test_precheck_skips_fresh_over_limit(self) -> None:
        row = self._mutation_row(100)
        live = _state(bytes_value=0, strategy="NO_RESET", used=STANDARD_BYTES)
        action, skip, err = rec.precheck_apply_row(
            row, live=live, fetch_kind="ok", fetch_error=None
        )
        self.assertEqual(action, rec.ACTION_SKIP)
        self.assertEqual(skip, rec.SKIP_OVER_LIMIT)
        self.assertIsNone(err)

    def test_precheck_skips_already_correct(self) -> None:
        row = self._mutation_row(100)
        live = _state(
            bytes_value=STANDARD_BYTES, strategy=STANDARD_STRATEGY, used=100
        )
        action, skip, err = rec.precheck_apply_row(
            row, live=live, fetch_kind="ok", fetch_error=None
        )
        self.assertEqual(action, rec.ACTION_SKIP)
        self.assertEqual(skip, rec.SKIP_ALREADY_CORRECT)
        self.assertIsNone(err)


class MockState:
    def __init__(self) -> None:
        self.shm_services: List[Dict[str, Any]] = []
        self.catalog: List[Dict[str, Any]] = []
        self.users: Dict[str, Dict[str, Any]] = {}
        self.patches: List[Dict[str, Any]] = []
        self.patch_raws: List[bytes] = []
        self.requests: List[Tuple[str, str]] = []
        self.auth_bodies: List[Dict[str, Any]] = []
        self.patch_fail_usernames: set = set()
        self.patch_omit_traffic_usernames: set = set()
        self.get_lag_usernames: Dict[str, int] = {}
        self.get_user_hits: Dict[str, int] = {}
        self.drift_bytes_after_plan: Dict[str, Any] = {}
        self.drift_used_after_plan: Dict[str, int] = {}
        self.change_used_after_patch: Dict[str, int] = {}
        self.change_status_after_patch: Dict[str, str] = {}
        self.lock = threading.Lock()

    def reset(self) -> None:
        with self.lock:
            self.shm_services.clear()
            self.catalog.clear()
            self.users.clear()
            self.patches.clear()
            self.patch_raws.clear()
            self.requests.clear()
            self.auth_bodies.clear()
            self.patch_fail_usernames.clear()
            self.patch_omit_traffic_usernames.clear()
            self.get_lag_usernames.clear()
            self.get_user_hits.clear()
            self.drift_bytes_after_plan.clear()
            self.drift_used_after_plan.clear()
            self.change_used_after_patch.clear()
            self.change_status_after_patch.clear()


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

    def _user_payload(self, user: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "response": {
                "id": user["id"],
                "status": user.get("status", "ACTIVE"),
                "trafficLimitBytes": user.get("trafficLimitBytes"),
                "trafficLimitStrategy": user.get("trafficLimitStrategy"),
                "expireAt": user.get("expireAt", "2026-12-31T00:00:00.000Z"),
                "hwidDeviceLimit": user.get("hwidDeviceLimit"),
                "externalSquadUuid": user.get("externalSquadUuid"),
                "activeInternalSquads": user.get("activeInternalSquads", []),
                "lastTrafficResetAt": user.get("lastTrafficResetAt"),
                "userTraffic": {
                    "usedTrafficBytes": user.get("usedTrafficBytes", 0),
                    "lifetimeUsedTrafficBytes": user.get(
                        "lifetimeUsedTrafficBytes", 0
                    ),
                },
            }
        }

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
        if "reset-traffic" in parsed.path:
            self._send(500, {"message": "reset-traffic must not be used"})
            return
        if parsed.path == "/api/users/bulk/update":
            self._send(500, {"message": "bulk update must not be used"})
            return
        self._send(404, {"message": "not found"})

    def do_PATCH(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        with STATE.lock:
            STATE.requests.append(("PATCH", parsed.path))
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
        username = self._username_for_id(user_id)
        with STATE.lock:
            if username in STATE.patch_fail_usernames:
                self._send(500, {"message": "forced patch failure"})
                return
            user = STATE.users.get(username)
            if user is None:
                self._send(404, {"message": "User not found"})
                return
            if "trafficLimitBytes" in body:
                user["trafficLimitBytes"] = body["trafficLimitBytes"]
            if "trafficLimitStrategy" in body:
                user["trafficLimitStrategy"] = body["trafficLimitStrategy"]
            if username in STATE.change_used_after_patch:
                user["usedTrafficBytes"] = STATE.change_used_after_patch[username]
            if username in STATE.change_status_after_patch:
                user["status"] = STATE.change_status_after_patch[username]
            omit = username in STATE.patch_omit_traffic_usernames
            snapshot = dict(user)
        if omit:
            self._send(200, {"response": {"id": user_id}})
            return
        self._send(200, self._user_payload(snapshot))

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
                hits = STATE.get_user_hits.get(username, 0)
                if user is not None and hits >= 2 and not STATE.patches:
                    if username in STATE.drift_bytes_after_plan:
                        user["trafficLimitBytes"] = STATE.drift_bytes_after_plan[
                            username
                        ]
                    if username in STATE.drift_used_after_plan:
                        user["usedTrafficBytes"] = STATE.drift_used_after_plan[
                            username
                        ]
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


class ReconcileTrafficTests(DisableEnvProxiesMixin, unittest.TestCase):
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
            include_over_limit=False,
            http_timeout=5,
            verify_retry_attempts=5,
            verify_retry_delay_sec=0,
        )
        values.update(kwargs)
        return rec.ReconcileConfig(**values)

    def _add_catalog(
        self,
        service_id: int,
        *,
        bytes_value: Any = "ABSENT",
        strategy: Any = "ABSENT",
        category: str = "vpn-mz-test",
        name: str = "Service",
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
        self,
        user_service_id: int,
        *,
        bytes_value: Any = 0,
        strategy: str = "NO_RESET",
        used: int = 0,
        status: str = "ACTIVE",
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
            "lastTrafficResetAt": "2026-01-01T00:00:00.000Z",
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

    def test_missing_setting_unmanaged_service(self) -> None:
        self._add_catalog(1, name="Legacy")
        self._add_user_service(11, 1)
        self._add_user(11, bytes_value=0)
        out = os.path.join(self.tmp.name, "out-unmanaged")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        row = self._load_plan(out)[0]
        self.assertEqual(row["classification"], "unmanaged_service")
        self.assertFalse(row["managed"])
        self.assertFalse(row["target_resolved"])
        self.assertNotIn("target_traffic_limit_bytes", row)
        self.assertEqual(len(STATE.patches), 0)

    def test_invalid_bytes_row(self) -> None:
        self._add_catalog(2, bytes_value=-1, strategy="NO_RESET", name="Bad")
        self._add_user_service(12, 2)
        self._add_user(12)
        out = os.path.join(self.tmp.name, "out-invalid-bytes")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        row = self._load_plan(out)[0]
        self.assertEqual(row["classification"], "invalid_shm_setting")
        self.assertEqual(self._load_summary(out)["plan_counts"]["invalid_shm_setting"], 1)

    def test_invalid_strategy_row(self) -> None:
        self._add_catalog(3, bytes_value=STANDARD_BYTES, strategy="YEAR", name="Bad")
        self._add_user_service(13, 3)
        self._add_user(13)
        out = os.path.join(self.tmp.name, "out-invalid-strategy")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        self.assertEqual(self._load_plan(out)[0]["classification"], "invalid_shm_setting")

    def test_month_rolling_shm_is_invalid_and_noted(self) -> None:
        self._add_catalog(
            4, bytes_value=STANDARD_BYTES, strategy="MONTH_ROLLING", name="Rolling"
        )
        self._add_user_service(14, 4)
        self._add_user(14)
        out = os.path.join(self.tmp.name, "out-rolling")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        summary = self._load_summary(out)
        self.assertEqual(self._load_plan(out)[0]["classification"], "invalid_shm_setting")
        self.assertEqual(summary["month_rolling"]["shm_invalid_month_rolling_count"], 1)
        self.assertFalse(summary["month_rolling"]["accepted_by_shm_template"])

    def test_already_correct_standard(self) -> None:
        self._add_catalog(
            5,
            bytes_value=STANDARD_BYTES,
            strategy=STANDARD_STRATEGY,
            name="Standard",
        )
        self._add_user_service(15, 5)
        self._add_user(15, bytes_value=STANDARD_BYTES, strategy=STANDARD_STRATEGY)
        out = os.path.join(self.tmp.name, "out-correct")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        row = self._load_plan(out)[0]
        self.assertEqual(row["classification"], "already_correct")
        self.assertEqual(row["target_traffic_limit_bytes"], STANDARD_BYTES)
        self.assertEqual(row["target_limit_gib"], 300.0)

    def test_needs_limit_month_and_both(self) -> None:
        self._add_catalog(
            6, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_catalog(7, bytes_value=MONTH_BYTES, strategy="MONTH", name="Plus")
        self._add_user_service(21, 6)
        self._add_user(21, bytes_value=0, strategy="NO_RESET")
        self._add_user_service(22, 7)
        self._add_user(22, bytes_value=MONTH_BYTES, strategy="NO_RESET")
        self._add_user_service(23, 7)
        self._add_user(23, bytes_value=0, strategy="DAY")
        out = os.path.join(self.tmp.name, "out-needs")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        plan = {row["username"]: row for row in self._load_plan(out)}
        self.assertEqual(plan["us_21"]["classification"], "needs_set_limit")
        self.assertEqual(plan["us_22"]["classification"], "needs_set_strategy")
        self.assertEqual(plan["us_23"]["classification"], "needs_set_limit_and_strategy")
        summary = self._load_summary(out)
        self.assertEqual(summary["plan_counts"]["needs_set_limit"], 1)
        self.assertEqual(summary["plan_counts"]["needs_set_strategy"], 1)
        self.assertEqual(summary["plan_counts"]["needs_set_limit_and_strategy"], 1)
        combos = {
            (c["target_traffic_limit_bytes"], c["target_traffic_limit_strategy"]): c[
                "users_count"
            ]
            for c in summary["target_combinations"]
        }
        self.assertEqual(combos[(STANDARD_BYTES, "NO_RESET")], 1)
        self.assertEqual(combos[(MONTH_BYTES, "MONTH")], 2)

    def test_over_limit_csv(self) -> None:
        self._add_catalog(
            8, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_user_service(31, 8)
        self._add_user(31, bytes_value=0, used=STANDARD_BYTES + 1)
        out = os.path.join(self.tmp.name, "out-over")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        self.assertTrue(self._load_plan(out)[0]["would_be_over_limit_now"])
        with open(os.path.join(out, "over_limit.csv"), encoding="utf-8") as fh:
            self.assertIn("us_31", fh.read())
        self.assertEqual(
            self._load_summary(out)["risk"]["would_be_over_limit_now_count"], 1
        )

    def test_missing_in_remnawave_report(self) -> None:
        self._add_catalog(
            9, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_user_service(32, 9)
        out = os.path.join(self.tmp.name, "out-missing")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        self.assertEqual(self._load_plan(out)[0]["classification"], "missing_in_remnawave")
        with open(os.path.join(out, "missing.csv"), encoding="utf-8") as fh:
            self.assertIn("us_32", fh.read())

    def test_dry_run_is_default_and_no_reset(self) -> None:
        self._add_catalog(
            10, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_user_service(41, 10)
        self._add_user(41, bytes_value=0)
        out = os.path.join(self.tmp.name, "out-dry")
        code, logs = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        self.assertEqual(STATE.patches, [])
        self.assertIn("Dry-run complete", logs)
        self.assertFalse(any("reset-traffic" in path for _, path in STATE.requests))
        self.assertFalse(any(method == "PATCH" for method, _ in STATE.requests))
        self.assertTrue(self._load_summary(out)["apply_default"] is False)

    def test_apply_refuses_unscoped_run(self) -> None:
        self._add_catalog(
            11, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_user_service(42, 11)
        self._add_user(42, bytes_value=0)
        out = os.path.join(self.tmp.name, "out-unscoped")
        code, logs = self._run(
            self._cfg(out, apply=True, confirm=rec.CONFIRM_PHRASE)
        )
        self.assertEqual(code, 1)
        self.assertEqual(STATE.patches, [])
        self.assertFalse(os.path.exists(out))
        self.assertIn("apply refused", logs)
        self.assertIn("--category or --service-id", logs)

    def test_apply_wrong_confirm(self) -> None:
        self._add_catalog(
            11, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_user_service(43, 11)
        self._add_user(43, bytes_value=0)
        out = os.path.join(self.tmp.name, "out-confirm")
        code, logs = self._run(
            self._cfg(out, apply=True, confirm="NOPE", service_ids=("11",))
        )
        self.assertEqual(code, 1)
        self.assertEqual(STATE.patches, [])
        self.assertIn("apply refused", logs)

    def test_apply_patches_only_traffic_fields(self) -> None:
        self._add_catalog(
            12, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_user_service(51, 12)
        user_id = self._add_user(51, bytes_value=0, strategy="MONTH", used=12345)
        out = os.path.join(self.tmp.name, "out-apply")
        code, _ = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                service_ids=("12",),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.patches), 1)
        self.assertEqual(set(STATE.patches[0].keys()), rec.PATCH_KEYS)
        self.assertEqual(STATE.patches[0]["id"], user_id)
        self.assertEqual(STATE.patches[0]["trafficLimitBytes"], STANDARD_BYTES)
        self.assertEqual(STATE.patches[0]["trafficLimitStrategy"], "NO_RESET")
        self.assertFalse(any("reset-traffic" in path for _, path in STATE.requests))
        self.assertEqual(STATE.users["us_51"]["usedTrafficBytes"], 12345)
        self.assertEqual(STATE.users["us_51"]["status"], "ACTIVE")
        self.assertEqual(
            STATE.users["us_51"]["lastTrafficResetAt"], "2026-01-01T00:00:00.000Z"
        )
        with open(os.path.join(out, "applied.json"), encoding="utf-8") as fh:
            applied = json.load(fh)
        self.assertEqual(applied[0]["prepatch_used_traffic_bytes"], 12345)
        self.assertEqual(applied[0]["status"], "ACTIVE")

    def test_over_limit_skipped_without_flag(self) -> None:
        self._add_catalog(
            13, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_user_service(61, 13)
        self._add_user(61, bytes_value=0, used=STANDARD_BYTES)
        self._add_user_service(62, 13)
        self._add_user(62, bytes_value=0, used=1)
        out = os.path.join(self.tmp.name, "out-skip-over")
        code, logs = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                categories=("vpn-mz-test",),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.patches), 1)
        self.assertEqual(STATE.patches[0]["id"], 1062)
        self.assertEqual(STATE.users["us_61"]["trafficLimitBytes"], 0)
        self.assertEqual(STATE.users["us_62"]["trafficLimitBytes"], STANDARD_BYTES)
        self.assertIn("Skipping 1 plan-time over-limit", logs)
        summary = self._load_summary(out)
        self.assertEqual(summary["apply"]["requested"], 1)
        self.assertEqual(summary["apply"]["skipped_over_limit"], 1)

    def test_include_over_limit_applies(self) -> None:
        self._add_catalog(
            14, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_user_service(71, 14)
        self._add_user(71, bytes_value=0, used=STANDARD_BYTES)
        out = os.path.join(self.tmp.name, "out-include-over")
        code, _ = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                service_ids=("14",),
                include_over_limit=True,
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.patches), 1)
        self.assertEqual(STATE.users["us_71"]["trafficLimitBytes"], STANDARD_BYTES)
        self.assertEqual(STATE.users["us_71"]["usedTrafficBytes"], STANDARD_BYTES)

    def test_verify_preserves_used_traffic_and_status(self) -> None:
        self._add_catalog(
            15, bytes_value=MONTH_BYTES, strategy="MONTH", name="Plus"
        )
        self._add_user_service(81, 15)
        self._add_user(
            81, bytes_value=0, strategy="NO_RESET", used=555, status="ACTIVE"
        )
        out = os.path.join(self.tmp.name, "out-verify")
        code, _ = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                service_ids=("15",),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(STATE.users["us_81"]["usedTrafficBytes"], 555)
        self.assertEqual(STATE.users["us_81"]["status"], "ACTIVE")
        self.assertEqual(STATE.users["us_81"]["trafficLimitBytes"], MONTH_BYTES)
        self.assertEqual(STATE.users["us_81"]["trafficLimitStrategy"], "MONTH")

    def test_verify_allows_used_traffic_growth(self) -> None:
        self._add_catalog(
            16, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_user_service(91, 16)
        self._add_user(91, bytes_value=0, used=10)
        STATE.change_used_after_patch["us_91"] = 99
        out = os.path.join(self.tmp.name, "out-used-grew")
        code, _ = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                service_ids=("16",),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.patches), 1)
        with open(os.path.join(out, "applied.json"), encoding="utf-8") as fh:
            applied = json.load(fh)
        self.assertEqual(applied[0]["plan_used_traffic_bytes"], 10)
        self.assertEqual(applied[0]["prepatch_used_traffic_bytes"], 10)
        self.assertEqual(applied[0]["postpatch_used_traffic_bytes"], 99)

    def test_verify_stops_if_used_traffic_decreases(self) -> None:
        self._add_catalog(
            16, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_user_service(94, 16)
        self._add_user(94, bytes_value=0, used=10)
        STATE.change_used_after_patch["us_94"] = 5
        out = os.path.join(self.tmp.name, "out-used-decreased")
        code, logs = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                service_ids=("16",),
            )
        )
        self.assertEqual(code, 1)
        with open(os.path.join(out, "errors.json"), encoding="utf-8") as fh:
            errors = json.load(fh)
        self.assertEqual(errors[0]["stage"], "verify")
        self.assertIn("decreased", errors[0]["error"])
        self.assertIn("Apply stopped", logs)

    def test_precheck_used_growth_allows_patch(self) -> None:
        self._add_catalog(
            80, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_user_service(301, 80)
        self._add_user(301, bytes_value=0, used=100)
        STATE.drift_used_after_plan["us_301"] = 110
        out = os.path.join(self.tmp.name, "out-pre-grow")
        code, _ = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                service_ids=("80",),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.patches), 1)
        with open(os.path.join(out, "applied.json"), encoding="utf-8") as fh:
            applied = json.load(fh)
        self.assertEqual(applied[0]["plan_used_traffic_bytes"], 100)
        self.assertEqual(applied[0]["prepatch_used_traffic_bytes"], 110)

    def test_precheck_used_decrease_blocks_patch(self) -> None:
        self._add_catalog(
            81, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_user_service(302, 81)
        self._add_user(302, bytes_value=0, used=100)
        STATE.drift_used_after_plan["us_302"] = 90
        out = os.path.join(self.tmp.name, "out-pre-dec")
        code, _ = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                service_ids=("81",),
            )
        )
        self.assertEqual(code, 1)
        self.assertEqual(STATE.patches, [])
        with open(os.path.join(out, "errors.json"), encoding="utf-8") as fh:
            errors = json.load(fh)
        self.assertEqual(errors[0]["stage"], "precheck")
        self.assertIn("decreased", errors[0]["error"])

    def test_fresh_over_limit_skips_without_flag(self) -> None:
        self._add_catalog(
            82, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_user_service(303, 82)
        self._add_user(303, bytes_value=0, used=100)
        STATE.drift_used_after_plan["us_303"] = STANDARD_BYTES
        out = os.path.join(self.tmp.name, "out-fresh-over")
        code, _ = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                service_ids=("82",),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(STATE.patches, [])
        self.assertEqual(STATE.users["us_303"]["trafficLimitBytes"], 0)
        summary = self._load_summary(out)
        self.assertEqual(summary["apply"]["applied"], 0)
        self.assertEqual(summary["apply"]["skipped_over_limit"], 1)
        with open(os.path.join(out, "skipped.json"), encoding="utf-8") as fh:
            skipped = json.load(fh)
        self.assertEqual(skipped[0]["reason"], "over_limit")

    def test_already_correct_before_patch_skips(self) -> None:
        self._add_catalog(
            83, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_user_service(304, 83)
        self._add_user(304, bytes_value=0, used=100)
        STATE.drift_bytes_after_plan["us_304"] = STANDARD_BYTES
        out = os.path.join(self.tmp.name, "out-became-correct")
        code, _ = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                service_ids=("83",),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(STATE.patches, [])
        self.assertEqual(self._load_summary(out)["apply"]["skipped_already_correct"], 1)

    def test_verify_stops_if_status_changes(self) -> None:
        self._add_catalog(
            17, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_user_service(92, 17)
        self._add_user(92, bytes_value=0, used=10, status="ACTIVE")
        STATE.change_status_after_patch["us_92"] = "LIMITED"
        out = os.path.join(self.tmp.name, "out-status-changed")
        code, _ = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                service_ids=("17",),
            )
        )
        self.assertEqual(code, 1)
        with open(os.path.join(out, "errors.json"), encoding="utf-8") as fh:
            errors = json.load(fh)
        self.assertEqual(errors[0]["stage"], "verify")
        self.assertIn("status changed unexpectedly", errors[0]["error"])

    def test_idempotency_second_dry_run_already_correct(self) -> None:
        self._add_catalog(
            18, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_user_service(93, 18)
        self._add_user(93, bytes_value=0, strategy="MONTH", used=777)
        out_apply = os.path.join(self.tmp.name, "out-idemp-a")
        code, _ = self._run(
            self._cfg(
                out_apply,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                categories=("vpn-mz-test",),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(STATE.users["us_93"]["trafficLimitBytes"], STANDARD_BYTES)
        self.assertEqual(STATE.users["us_93"]["usedTrafficBytes"], 777)
        out_dry = os.path.join(self.tmp.name, "out-idemp-b")
        code, _ = self._run(self._cfg(out_dry))
        self.assertEqual(code, 0)
        row = self._load_plan(out_dry)[0]
        self.assertEqual(row["classification"], "already_correct")
        self.assertEqual(row["used_traffic_bytes"], 777)
        self.assertEqual(len(STATE.patches), 1)

    def test_category_and_service_id_filters(self) -> None:
        self._add_catalog(
            20,
            bytes_value=STANDARD_BYTES,
            strategy="NO_RESET",
            name="Standard",
            category="vpn-mz-test",
        )
        self._add_catalog(
            21,
            bytes_value=MONTH_BYTES,
            strategy="MONTH",
            name="Plus",
            category="other",
        )
        self._add_user_service(101, 20, category="vpn-mz-test")
        self._add_user(101, bytes_value=0)
        self._add_user_service(102, 21, category="other")
        self._add_user(102, bytes_value=0)
        out_cat = os.path.join(self.tmp.name, "out-cat")
        code, _ = self._run(self._cfg(out_cat, categories=("vpn-mz-test",)))
        self.assertEqual(code, 0)
        self.assertEqual([row["username"] for row in self._load_plan(out_cat)], ["us_101"])
        out_sid = os.path.join(self.tmp.name, "out-sid")
        code, _ = self._run(self._cfg(out_sid, service_ids=("21",)))
        self.assertEqual(code, 0)
        self.assertEqual([row["username"] for row in self._load_plan(out_sid)], ["us_102"])

    def test_reports_exist_and_schema_recorded(self) -> None:
        self._add_catalog(
            30, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_user_service(111, 30)
        self._add_user(111, bytes_value=0)
        out = os.path.join(self.tmp.name, "out-reports")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        for name in (
            "summary.json",
            "plan.json",
            "plan.csv",
            "errors.csv",
            "missing.csv",
            "over_limit.csv",
        ):
            self.assertTrue(os.path.exists(os.path.join(out, name)), name)
        with open(os.path.join(out, "plan.csv"), encoding="utf-8") as fh:
            header = fh.readline()
        for col in (
            "user_service_id",
            "username",
            "shm_category",
            "service_id",
            "service_name",
            "current_traffic_limit_bytes",
            "target_traffic_limit_bytes",
            "current_traffic_limit_strategy",
            "target_traffic_limit_strategy",
            "used_traffic_bytes",
            "used_traffic_gib",
            "target_limit_gib",
            "would_be_over_limit_now",
            "classification",
        ):
            self.assertIn(col, header)
        schema = self._load_summary(out)["remnawave_schema"]
        self.assertEqual(
            schema["usedTrafficBytes"], "response.userTraffic.usedTrafficBytes"
        )

    def test_allow_list_limits_mutations_and_excludes_others(self) -> None:
        self._add_catalog(
            50, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_user_service(201, 50)
        self._add_user(201, bytes_value=0)
        self._add_user_service(202, 50)
        self._add_user(202, bytes_value=0)
        self._add_user_service(203, 50)
        self._add_user(203, bytes_value=0)
        out = os.path.join(self.tmp.name, "out-allow")
        code, logs = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                service_ids=("50",),
                apply_usernames=("us_201", "us_202"),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.patches), 2)
        patched_ids = {p["id"] for p in STATE.patches}
        self.assertEqual(patched_ids, {1201, 1202})
        self.assertEqual(STATE.users["us_203"]["trafficLimitBytes"], 0)
        self.assertIn("allow-list=2", logs)
        with open(os.path.join(out, "applied.json"), encoding="utf-8") as fh:
            applied = json.load(fh)
        self.assertEqual({row["username"] for row in applied}, {"us_201", "us_202"})
        allow = self._load_summary(out)["apply"]["allowlist"]
        self.assertTrue(allow["enabled"])
        self.assertEqual(allow["requested"], 2)
        self.assertEqual(allow["matched"], 2)
        self.assertEqual(allow["applied"], 2)
        self.assertEqual(allow["not_found"], 0)

    def test_allow_list_does_not_expand_service_id_scope(self) -> None:
        self._add_catalog(
            51, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_catalog(
            52, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Other"
        )
        self._add_user_service(211, 51)
        self._add_user(211, bytes_value=0)
        self._add_user_service(212, 52)
        self._add_user(212, bytes_value=0)
        out = os.path.join(self.tmp.name, "out-no-expand")
        code, _ = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                service_ids=("51",),
                apply_usernames=("us_211", "us_212"),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.patches), 1)
        self.assertEqual(STATE.patches[0]["id"], 1211)
        self.assertEqual(STATE.users["us_212"]["trafficLimitBytes"], 0)
        plan_names = {row["username"] for row in self._load_plan(out)}
        self.assertEqual(plan_names, {"us_211"})
        allow = self._load_summary(out)["apply"]["allowlist"]
        self.assertEqual(allow["matched"], 1)
        self.assertEqual(allow["not_found"], 1)
        self.assertEqual(allow["applied"], 1)

    def test_allow_list_unscoped_run_refused(self) -> None:
        self._add_catalog(
            53, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_user_service(221, 53)
        self._add_user(221, bytes_value=0)
        out = os.path.join(self.tmp.name, "out-allow-unscoped")
        code, logs = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                apply_usernames=("us_221",),
            )
        )
        self.assertEqual(code, 1)
        self.assertEqual(STATE.patches, [])
        self.assertFalse(os.path.exists(out))
        self.assertIn("apply refused", logs)

    def test_allow_listed_already_correct_is_not_patched(self) -> None:
        self._add_catalog(
            54, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_user_service(231, 54)
        self._add_user(231, bytes_value=STANDARD_BYTES, strategy="NO_RESET")
        out = os.path.join(self.tmp.name, "out-allow-correct")
        code, _ = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                service_ids=("54",),
                apply_usernames=("us_231",),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(STATE.patches, [])
        self.assertEqual(self._load_plan(out)[0]["classification"], "already_correct")
        allow = self._load_summary(out)["apply"]["allowlist"]
        self.assertEqual(allow["applied"], 0)
        self.assertEqual(allow["skipped_nonmutation"], 1)

    def test_allow_listed_missing_is_not_patched(self) -> None:
        self._add_catalog(
            55, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_user_service(241, 55)
        out = os.path.join(self.tmp.name, "out-allow-missing")
        code, _ = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                service_ids=("55",),
                apply_usernames=("us_241",),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(STATE.patches, [])
        self.assertEqual(
            self._load_plan(out)[0]["classification"], "missing_in_remnawave"
        )
        allow = self._load_summary(out)["apply"]["allowlist"]
        self.assertEqual(allow["applied"], 0)
        self.assertEqual(allow["skipped_nonmutation"], 1)

    def test_allow_listed_over_limit_skipped_without_flag(self) -> None:
        self._add_catalog(
            56, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_user_service(251, 56)
        self._add_user(251, bytes_value=0, used=STANDARD_BYTES)
        self._add_user_service(252, 56)
        self._add_user(252, bytes_value=0, used=1)
        out = os.path.join(self.tmp.name, "out-allow-over")
        code, logs = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                service_ids=("56",),
                apply_usernames=("us_251", "us_252"),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.patches), 1)
        self.assertEqual(STATE.patches[0]["id"], 1252)
        self.assertEqual(STATE.users["us_251"]["trafficLimitBytes"], 0)
        self.assertIn("Skipping 1 plan-time over-limit", logs)
        summary = self._load_summary(out)
        self.assertEqual(summary["apply"]["requested"], 1)
        self.assertEqual(summary["apply"]["skipped_over_limit"], 1)
        self.assertEqual(summary["apply"]["allowlist"]["skipped_over_limit"], 1)
        self.assertEqual(summary["apply"]["allowlist"]["applied"], 1)

    def test_include_over_limit_with_allow_list_stays_in_scope(self) -> None:
        self._add_catalog(
            57, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_catalog(
            58, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Other"
        )
        self._add_user_service(261, 57)
        self._add_user(261, bytes_value=0, used=STANDARD_BYTES)
        self._add_user_service(262, 57)
        self._add_user(262, bytes_value=0, used=1)
        self._add_user_service(263, 58)
        self._add_user(263, bytes_value=0, used=STANDARD_BYTES)
        out = os.path.join(self.tmp.name, "out-allow-over-include")
        code, _ = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                service_ids=("57",),
                apply_usernames=("us_261", "us_263"),
                include_over_limit=True,
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.patches), 1)
        self.assertEqual(STATE.patches[0]["id"], 1261)
        self.assertEqual(STATE.users["us_262"]["trafficLimitBytes"], 0)
        self.assertEqual(STATE.users["us_263"]["trafficLimitBytes"], 0)
        allow = self._load_summary(out)["apply"]["allowlist"]
        self.assertEqual(allow["applied"], 1)
        self.assertEqual(allow["not_found"], 1)

    def test_allow_list_idempotent_second_run(self) -> None:
        self._add_catalog(
            59, bytes_value=STANDARD_BYTES, strategy="NO_RESET", name="Standard"
        )
        self._add_user_service(271, 59)
        self._add_user(271, bytes_value=0, used=42)
        self._add_user_service(272, 59)
        self._add_user(272, bytes_value=0, used=7)
        first = os.path.join(self.tmp.name, "out-allow-idemp-a")
        code, _ = self._run(
            self._cfg(
                first,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                service_ids=("59",),
                apply_usernames=("us_271", "us_272"),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.patches), 2)
        second = os.path.join(self.tmp.name, "out-allow-idemp-b")
        code, _ = self._run(
            self._cfg(
                second,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                service_ids=("59",),
                apply_usernames=("us_271", "us_272"),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.patches), 2)
        plan = {row["username"]: row for row in self._load_plan(second)}
        self.assertEqual(plan["us_271"]["classification"], "already_correct")
        self.assertEqual(plan["us_272"]["classification"], "already_correct")
        self.assertEqual(self._load_summary(second)["apply"]["applied"], 0)
        self.assertEqual(STATE.users["us_271"]["usedTrafficBytes"], 42)
        self.assertEqual(STATE.users["us_272"]["usedTrafficBytes"], 7)

    def test_unmanaged_not_applied_even_when_scoped(self) -> None:
        self._add_catalog(40, name="Legacy")
        self._add_user_service(121, 40)
        self._add_user(121, bytes_value=999)
        out = os.path.join(self.tmp.name, "out-no-unmanaged-apply")
        code, _ = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                service_ids=("40",),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(STATE.patches, [])
        self.assertEqual(STATE.users["us_121"]["trafficLimitBytes"], 999)
        self.assertEqual(self._load_summary(out)["apply"]["requested"], 0)


if __name__ == "__main__":
    unittest.main()
