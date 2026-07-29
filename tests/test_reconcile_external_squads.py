#!/usr/bin/env python3
"""Tests for scripts/reconcile_external_squads.py with a local mock HTTP server."""

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
sys.path.insert(0, str(SCRIPTS))

import reconcile_external_squads as rec  # noqa: E402


VFF_UUID = "11111111-1111-1111-1111-111111111111"
FC_UUID = "22222222-2222-2222-2222-222222222222"
OTHER_UUID = "99999999-9999-9999-9999-999999999999"

SHM_PASSWORD = "super-secret-shm-password-xyz"
RW_TOKEN = "super-secret-remnawave-token-abc"


class MockState:
    def __init__(self) -> None:
        self.shm_services: List[Dict[str, Any]] = []
        self.users: Dict[str, Dict[str, Any]] = {}
        self.patches: List[Dict[str, Any]] = []
        self.auth_bodies: List[Dict[str, Any]] = []
        self.service_offsets: List[int] = []
        self.get_user_hits: Dict[str, int] = {}
        self.patch_fail_usernames: set = set()
        self.patch_omit_external_usernames: set = set()
        self.get_lag_usernames: Dict[str, int] = {}
        self.lock = threading.Lock()

    def reset(self) -> None:
        with self.lock:
            self.shm_services.clear()
            self.users.clear()
            self.patches.clear()
            self.auth_bodies.clear()
            self.service_offsets.clear()
            self.get_user_hits.clear()
            self.patch_fail_usernames.clear()
            self.patch_omit_external_usernames.clear()
            self.get_lag_usernames.clear()


STATE = MockState()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _username_for_uuid(self, user_uuid: str) -> str:
        with STATE.lock:
            for username, user in STATE.users.items():
                if user.get("uuid") == user_uuid:
                    return username
        return ""

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/shm/user/auth.cgi":
            body = self._read_json() or {}
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
        body = self._read_json() or {}
        with STATE.lock:
            STATE.patches.append(body)
        user_uuid = body.get("uuid")
        target = body.get("externalSquadUuid")
        username = self._username_for_uuid(str(user_uuid))
        with STATE.lock:
            if username in STATE.patch_fail_usernames:
                self._send(500, {"message": "forced patch failure"})
                return
            user = STATE.users.get(username)
            if user is None:
                self._send(404, {"message": "User not found"})
                return
            user["externalSquadUuid"] = target
            omit = username in STATE.patch_omit_external_usernames
            shape = user.get("_shape", "flat")
        if omit:
            if shape == "nested":
                self._send(200, {"response": {"user": {"uuid": user_uuid}}})
            else:
                self._send(200, {"response": {"uuid": user_uuid}})
            return
        if shape == "nested":
            self._send(
                200,
                {
                    "response": {
                        "user": {
                            "uuid": user_uuid,
                            "externalSquadUuid": target,
                        }
                    }
                },
            )
        else:
            self._send(
                200,
                {
                    "response": {
                        "uuid": user_uuid,
                        "externalSquadUuid": target,
                    }
                },
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
                STATE.service_offsets.append(offset)
                items = list(STATE.shm_services)
            total = len(items)
            page = items[offset : offset + limit]
            self._send(
                200,
                {
                    "data": page,
                    "items": total,
                    "limit": limit,
                    "offset": offset,
                },
            )
            return

        if parsed.path == "/api/external-squads":
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {RW_TOKEN}":
                self._send(401, {"message": "unauthorized"})
                return
            self._send(
                200,
                {
                    "response": {
                        "externalSquads": [
                            {"name": "VPN-for-Friends", "uuid": VFF_UUID},
                            {"name": "Friends-Connect", "uuid": FC_UUID},
                        ]
                    }
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
                    p.get("uuid") == (user or {}).get("uuid") for p in STATE.patches
                )
                post_patch_gets = 0
                if patched and user is not None:
                    key = f"post:{username}"
                    STATE.get_user_hits[key] = STATE.get_user_hits.get(key, 0) + 1
                    post_patch_gets = STATE.get_user_hits[key]
            if user is None:
                self._send(404, {"message": "User not found"})
                return
            effective_external = user.get("externalSquadUuid")
            # After PATCH, first `lag` GETs pretend the value is still unset.
            if patched and lag and post_patch_gets <= lag:
                effective_external = None

            if user.get("_shape") == "nested":
                self._send(
                    200,
                    {
                        "response": {
                            "user": {
                                "uuid": user["uuid"],
                                "externalSquadUuid": effective_external,
                            }
                        }
                    },
                )
            else:
                self._send(
                    200,
                    {
                        "response": {
                            "uuid": user["uuid"],
                            "externalSquadUuid": effective_external,
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


class ReconcileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.base_url = start_server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        STATE.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {
            "SHM_PASSWORD": SHM_PASSWORD,
            "REMNAWAVE_TOKEN": RW_TOKEN,
        }

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
            http_timeout=5,
            verify_retry_attempts=5,
            verify_retry_delay_sec=0,
        )
        values.update(kwargs)
        return rec.ReconcileConfig(**values)

    def _add_service(
        self, user_service_id: int, category: str, status: str = "ACTIVE"
    ) -> None:
        STATE.shm_services.append(
            {
                "user_service_id": user_service_id,
                "category": category,
                "status": status,
            }
        )

    def _add_user(
        self,
        user_service_id: int,
        *,
        external: Any = None,
        shape: str = "flat",
        missing_key: bool = False,
    ) -> str:
        username = f"us_{user_service_id}"
        uuid = f"user-{user_service_id:04d}-aaaa-bbbb-cccccccccccc"
        user: Dict[str, Any] = {"uuid": uuid, "_shape": shape}
        if not missing_key:
            user["externalSquadUuid"] = external
        STATE.users[username] = user
        return uuid

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

    def test_01_vpn_mz_test_maps_to_vff(self) -> None:
        self._add_service(101, "vpn-mz-test")
        self._add_user(101, external=None)
        out = os.path.join(self.tmp.name, "out1")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        row = self._load_plan(out)[0]
        self.assertEqual(row["username"], "us_101")
        self.assertEqual(row["target_external_squad_name"], "VPN-for-Friends")
        self.assertEqual(row["target_external_squad_uuid"], VFF_UUID)
        self.assertEqual(row["classification"], "needs_assignment")

    def test_02_vpn_mz_fc_maps_to_friends_connect(self) -> None:
        self._add_service(102, "vpn-mz-fc")
        self._add_user(102, external=None, shape="nested")
        out = os.path.join(self.tmp.name, "out2")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        row = self._load_plan(out)[0]
        self.assertEqual(row["username"], "us_102")
        self.assertEqual(row["target_external_squad_name"], "Friends-Connect")
        self.assertEqual(row["target_external_squad_uuid"], FC_UUID)

    def test_03_already_correct_skipped(self) -> None:
        self._add_service(103, "vpn-mz-test")
        self._add_user(103, external=VFF_UUID)
        out = os.path.join(self.tmp.name, "out3")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        row = self._load_plan(out)[0]
        self.assertEqual(row["classification"], "already_correct")
        self.assertEqual(
            self._load_summary(out)["plan_counts"]["already_correct"], 1
        )

    def test_04_null_needs_assignment(self) -> None:
        self._add_service(104, "vpn-mz-test")
        self._add_user(104, external=None)
        out = os.path.join(self.tmp.name, "out4")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        self.assertEqual(self._load_plan(out)[0]["classification"], "needs_assignment")

    def test_05_conflict_not_modified(self) -> None:
        self._add_service(105, "vpn-mz-test")
        uuid_conflict = self._add_user(105, external=OTHER_UUID)
        self._add_service(106, "vpn-mz-test")
        uuid_need = self._add_user(106, external=None)
        out = os.path.join(self.tmp.name, "out5")
        cfg = self._cfg(out, apply=True, confirm=rec.CONFIRM_PHRASE)
        code, _ = self._run(cfg)
        self.assertEqual(code, 0)
        plan = {r["user_service_id"]: r for r in self._load_plan(out)}
        self.assertEqual(plan[105]["classification"], "conflict")
        self.assertEqual(plan[106]["classification"], "needs_assignment")
        patched_uuids = [p["uuid"] for p in STATE.patches]
        self.assertNotIn(uuid_conflict, patched_uuids)
        self.assertIn(uuid_need, patched_uuids)
        self.assertEqual(STATE.users["us_105"]["externalSquadUuid"], OTHER_UUID)
        self.assertEqual(STATE.users["us_106"]["externalSquadUuid"], VFF_UUID)

    def test_06_missing_in_remnawave(self) -> None:
        self._add_service(107, "vpn-mz-fc")
        out = os.path.join(self.tmp.name, "out6")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        row = self._load_plan(out)[0]
        self.assertEqual(row["classification"], "missing_in_remnawave")
        with open(os.path.join(out, "missing.csv"), encoding="utf-8") as fh:
            self.assertIn("us_107", fh.read())

    def test_07_shm_pagination(self) -> None:
        for i in range(5):
            self._add_service(200 + i, "vpn-mz-test")
            self._add_user(200 + i, external=None)
        out = os.path.join(self.tmp.name, "out7")
        code, _ = self._run(self._cfg(out, page_size=2))
        self.assertEqual(code, 0)
        self.assertEqual(len(self._load_plan(out)), 5)
        self.assertEqual(STATE.service_offsets, [0, 2, 4])

    def test_08_patch_only_needs_assignment_payload(self) -> None:
        self._add_service(301, "vpn-mz-test")
        uuid_need = self._add_user(301, external=None)
        self._add_service(302, "vpn-mz-test")
        self._add_user(302, external=VFF_UUID)
        self._add_service(303, "vpn-mz-test")
        self._add_user(303, external=OTHER_UUID)
        out = os.path.join(self.tmp.name, "out8")
        code, _ = self._run(
            self._cfg(out, apply=True, confirm=rec.CONFIRM_PHRASE)
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.patches), 1)
        patch = STATE.patches[0]
        self.assertEqual(set(patch.keys()), {"uuid", "externalSquadUuid"})
        self.assertEqual(patch["uuid"], uuid_need)
        self.assertEqual(patch["externalSquadUuid"], VFF_UUID)
        summary = self._load_summary(out)
        self.assertEqual(summary["apply"]["requested"], 1)
        self.assertEqual(summary["apply"]["applied"], 1)
        self.assertEqual(summary["apply"]["failed"], 0)
        self.assertTrue(summary["apply"]["complete"])
        with open(os.path.join(out, "applied.json"), encoding="utf-8") as fh:
            applied = json.load(fh)
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["username"], "us_301")

    def test_09_patch_response_verified(self) -> None:
        self._add_service(401, "vpn-mz-fc")
        uuid_need = self._add_user(401, external=None, shape="nested")
        out = os.path.join(self.tmp.name, "out9")
        code, _ = self._run(
            self._cfg(out, apply=True, confirm=rec.CONFIRM_PHRASE)
        )
        self.assertEqual(code, 0)
        self.assertEqual(STATE.patches[0]["uuid"], uuid_need)
        self.assertEqual(STATE.users["us_401"]["externalSquadUuid"], FC_UUID)

    def test_10_wrong_confirm_blocks_apply(self) -> None:
        self._add_service(402, "vpn-mz-test")
        self._add_user(402, external=None)
        out = os.path.join(self.tmp.name, "out10")
        code, logs = self._run(self._cfg(out, apply=True, confirm="NOPE"))
        self.assertEqual(code, 1)
        self.assertEqual(STATE.patches, [])
        self.assertFalse(os.path.exists(out))
        self.assertIn("apply refused", logs)

    def test_11_secrets_not_in_stdout_or_reports(self) -> None:
        self._add_service(501, "vpn-mz-test")
        self._add_user(501, external=None)
        out = os.path.join(self.tmp.name, "out11")
        code, logs = self._run(
            self._cfg(out, apply=True, confirm=rec.CONFIRM_PHRASE)
        )
        self.assertEqual(code, 0)
        self.assertNotIn(SHM_PASSWORD, logs)
        self.assertNotIn(RW_TOKEN, logs)
        for root, _dirs, files in os.walk(out):
            for name in files:
                text = Path(root, name).read_text(encoding="utf-8")
                self.assertNotIn(SHM_PASSWORD, text)
                self.assertNotIn(RW_TOKEN, text)
                self.assertNotIn("test-session", text)

    def test_12_second_dry_run_after_apply_is_already_correct(self) -> None:
        self._add_service(601, "vpn-mz-fc")
        self._add_user(601, external=None)
        out_apply = os.path.join(self.tmp.name, "out12a")
        code, _ = self._run(
            self._cfg(out_apply, apply=True, confirm=rec.CONFIRM_PHRASE)
        )
        self.assertEqual(code, 0)
        self.assertEqual(STATE.users["us_601"]["externalSquadUuid"], FC_UUID)

        out_dry = os.path.join(self.tmp.name, "out12b")
        code, _ = self._run(self._cfg(out_dry))
        self.assertEqual(code, 0)
        row = self._load_plan(out_dry)[0]
        self.assertEqual(row["classification"], "already_correct")
        self.assertEqual(len(STATE.patches), 1)

    def test_13_get_retry_when_patch_omits_external(self) -> None:
        self._add_service(701, "vpn-mz-test")
        self._add_user(701, external=None)
        STATE.patch_omit_external_usernames.add("us_701")
        STATE.get_lag_usernames["us_701"] = 2
        out = os.path.join(self.tmp.name, "out13")
        code, _ = self._run(
            self._cfg(
                out,
                apply=True,
                confirm=rec.CONFIRM_PHRASE,
                verify_retry_attempts=5,
                verify_retry_delay_sec=0,
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(STATE.patches), 1)
        # plan GET + verify GETs after omitted PATCH field
        self.assertGreaterEqual(STATE.get_user_hits.get("post:us_701", 0), 3)
        with open(os.path.join(out, "applied.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)[0]["username"], "us_701")

    def test_14_error_stops_subsequent_patches(self) -> None:
        self._add_service(801, "vpn-mz-test")
        self._add_user(801, external=None)
        self._add_service(802, "vpn-mz-test")
        self._add_user(802, external=None)
        self._add_service(803, "vpn-mz-test")
        self._add_user(803, external=None)
        STATE.patch_fail_usernames.add("us_802")
        out = os.path.join(self.tmp.name, "out14")
        code, _ = self._run(
            self._cfg(out, apply=True, confirm=rec.CONFIRM_PHRASE)
        )
        self.assertEqual(code, 1)
        patched_users = [self._username_for_patch(p) for p in STATE.patches]
        # failure still records the PATCH attempt for us_802, but not us_803
        self.assertEqual(patched_users, ["us_801", "us_802"])
        self.assertEqual(STATE.users["us_801"]["externalSquadUuid"], VFF_UUID)
        self.assertIsNone(STATE.users["us_802"]["externalSquadUuid"])
        self.assertIsNone(STATE.users["us_803"]["externalSquadUuid"])
        with open(os.path.join(out, "applied.json"), encoding="utf-8") as fh:
            applied = json.load(fh)
        self.assertEqual([a["username"] for a in applied], ["us_801"])
        with open(os.path.join(out, "errors.json"), encoding="utf-8") as fh:
            errors = json.load(fh)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["username"], "us_802")
        summary = self._load_summary(out)
        self.assertEqual(summary["apply"]["requested"], 3)
        self.assertEqual(summary["apply"]["applied"], 1)
        self.assertEqual(summary["apply"]["failed"], 1)
        self.assertFalse(summary["apply"]["complete"])

    def test_15_rerun_after_partial_apply_continues_remaining(self) -> None:
        self._add_service(901, "vpn-mz-test")
        self._add_user(901, external=None)
        self._add_service(902, "vpn-mz-test")
        self._add_user(902, external=None)
        self._add_service(903, "vpn-mz-fc")
        self._add_user(903, external=None)
        STATE.patch_fail_usernames.add("us_902")

        out1 = os.path.join(self.tmp.name, "out15a")
        code, _ = self._run(
            self._cfg(out1, apply=True, confirm=rec.CONFIRM_PHRASE)
        )
        self.assertEqual(code, 1)
        self.assertEqual(STATE.users["us_901"]["externalSquadUuid"], VFF_UUID)
        self.assertIsNone(STATE.users["us_902"]["externalSquadUuid"])
        self.assertIsNone(STATE.users["us_903"]["externalSquadUuid"])

        # Fix the failure and continue
        STATE.patch_fail_usernames.clear()
        STATE.patches.clear()
        out2 = os.path.join(self.tmp.name, "out15b")
        code, _ = self._run(
            self._cfg(out2, apply=True, confirm=rec.CONFIRM_PHRASE)
        )
        self.assertEqual(code, 0)
        plan = {r["username"]: r["classification"] for r in self._load_plan(out2)}
        self.assertEqual(plan["us_901"], "already_correct")
        self.assertEqual(plan["us_902"], "needs_assignment")
        self.assertEqual(plan["us_903"], "needs_assignment")
        patched = [self._username_for_patch(p) for p in STATE.patches]
        self.assertEqual(set(patched), {"us_902", "us_903"})
        self.assertEqual(STATE.users["us_902"]["externalSquadUuid"], VFF_UUID)
        self.assertEqual(STATE.users["us_903"]["externalSquadUuid"], FC_UUID)
        summary = self._load_summary(out2)
        self.assertEqual(summary["plan_counts"]["already_correct"], 1)
        self.assertEqual(summary["apply"]["requested"], 2)
        self.assertEqual(summary["apply"]["applied"], 2)
        self.assertTrue(summary["apply"]["complete"])

    def test_cli_reads_secrets_from_env_names(self) -> None:
        args = rec.parse_args(
            [
                "--shm-base-url",
                self.base_url,
                "--shm-login",
                "admin",
                "--shm-password-env",
                "SHM_PASSWORD",
                "--remnawave-panel-url",
                self.base_url,
                "--remnawave-token-env",
                "REMNAWAVE_TOKEN",
                "--output",
                os.path.join(self.tmp.name, "cli-out"),
                "--request-delay-ms",
                "0",
            ]
        )
        cfg = rec.config_from_args(args, environ=self.env)
        self.assertEqual(cfg.shm_password, SHM_PASSWORD)
        self.assertEqual(cfg.remnawave_token, RW_TOKEN)

    def test_username_uses_user_service_id_not_user_id(self) -> None:
        STATE.shm_services.append(
            {
                "user_service_id": 777,
                "user_id": 1,
                "category": "vpn-mz-test",
                "status": "ACTIVE",
            }
        )
        self._add_user(777, external=None)
        STATE.users["us_1"] = {"uuid": "wrong", "externalSquadUuid": None}
        out = os.path.join(self.tmp.name, "out-usid")
        code, _ = self._run(self._cfg(out))
        self.assertEqual(code, 0)
        self.assertEqual(self._load_plan(out)[0]["username"], "us_777")

    def _username_for_patch(self, patch: Dict[str, Any]) -> str:
        user_uuid = patch["uuid"]
        for username, user in STATE.users.items():
            if user.get("uuid") == user_uuid:
                return username
        return ""


if __name__ == "__main__":
    unittest.main()
