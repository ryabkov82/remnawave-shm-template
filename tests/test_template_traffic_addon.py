#!/usr/bin/env python3
"""CREATE of remnawave_traffic_addon raises one existing user's limit.

The template must not create or delete a Remnawave user, reset traffic,
change expireAt, call enable, or move money. Payment comes from the
rendered withdraw. A repeated CREATE sets the stored absolute limit and
does not add the pack a second time.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import TestCase

from tests.proxy_env import DisableEnvProxiesMixin


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "shm-remnawave-traffic-addon.template.sh"

BASE = 322122547200
PACK = 322122547200
TARGET = BASE + PACK
PARENT = 18432
ADDON = 99001
USER = 1001
PURCHASE = "0123456789abcdef0123456789abcdef"
UUID = "11111111-1111-4111-8111-111111111111"
EXPIRE = "2026-12-15T00:00:00.000Z"
TOKEN = "test-token-must-not-appear"


def _period_bounds(period: str) -> tuple[str, str]:
    year, month = (int(part) for part in period.split("-"))
    start = datetime(year, month, 1, 0, 20, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, 0, 20, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, 0, 20, tzinfo=timezone.utc)
    return (
        start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _open_period(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=20, second=0, microsecond=0)
    if now < start:
        previous = start.replace(day=1) - timedelta(days=1)
        return f"{previous.year:04d}-{previous.month:02d}"
    return f"{now.year:04d}-{now.month:02d}"


def _shift_period(period: str, months: int) -> str:
    year, month = (int(part) for part in period.split("-"))
    index = year * 12 + (month - 1) + months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _catalog(bytes_value: int = PACK) -> Dict[str, Any]:
    return {
        "limit_bonus_percent": 0,
        "no_money_back": True,
        "traffic_addon": {
            "brand": "vff",
            "parent_category": "vpn-mz-test",
            "parent_service_ids": [3, 4, 5, 6],
            "bytes": bytes_value,
            "strategy": "MONTH",
            "period": "remnawave_monthly_reset",
            "reset_cron": "20 0 1 * *",
            "reset_timezone": "UTC",
            "max_purchases_per_period": 1,
        },
    }


def _settings(period: str) -> Dict[str, Any]:
    return {
        "purchase_id": PURCHASE,
        "parent_user_service_id": PARENT,
        "period": period,
    }


def _withdraw(paid: bool, total: str = "150.00") -> Any:
    row: Dict[str, Any] = {
        "withdraw_id": 775501,
        "user_id": USER,
        "user_service_id": ADDON,
        "service_id": 17,
        "total": total,
        "withdraw_date": None,
    }
    if paid:
        row["withdraw_date"] = "2026-11-26T00:00:00Z"
    return row


def _storage(period: str, state: str = "PAID", amount: str = "150.00") -> Dict[str, Any]:
    start, end = _period_bounds(period)
    return {
        "purchase_id": PURCHASE,
        "user_id": USER,
        "parent_user_service_id": PARENT,
        "user_service_id": ADDON,
        "withdraw_id": 775501,
        "period": period,
        "period_starts_at": start,
        "period_ends_at": end,
        "base_limit_bytes": BASE,
        "addon_bytes": PACK,
        "applied_limit_bytes": TARGET,
        "amount": amount,
        "currency": "RUB",
        "state": state,
        "state_changed_at": "2026-11-26T00:00:00Z",
        "last_error": None,
    }


def _parent(service_id: int = 3, category: str = "vpn-mz-test", user_id: int = USER, status: str = "ACTIVE") -> Dict[str, Any]:
    return {
        "data": [
            {
                "user_service_id": PARENT,
                "user_id": user_id,
                "service_id": service_id,
                "status": status,
                "service": {"category": category, "name": "tariff"},
            }
        ]
    }


def _user(
    limit: int = BASE,
    status: str = "ACTIVE",
    reset_at: Optional[str] = None,
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    user: Dict[str, Any] = {
        "uuid": UUID,
        "id": 10,
        "username": f"us_{PARENT}",
        "status": status,
        "trafficLimitBytes": limit,
        "usedTrafficBytes": 1024,
        "trafficLimitStrategy": "MONTH",
        "lastTrafficResetAt": reset_at,
        "expireAt": EXPIRE,
        "hwidDeviceLimit": 3,
    }
    if created_at is not None:
        user["createdAt"] = created_at
    return user


def _foreign_lock(until: int) -> Dict[str, Any]:
    return {
        "lock_id": "ffffffffffffffffffffffffffffffff",
        "purchase_id": "fedcba9876543210fedcba9876543210",
        "handler": "other",
        "parent_user_service_id": PARENT,
        "until": until,
    }


class World:
    def __init__(self, period: str) -> None:
        self.lock = threading.Lock()
        self.period = period
        self.storage: Dict[str, Any] = {}
        self.user = _user(reset_at=_period_bounds(period)[0].replace("Z", ".000Z"))
        self.parent = _parent()
        self.calls: List[Dict[str, Any]] = []
        self.panel_failures = 0
        self.panel_gets = 0
        self.fail_panel_get_at = 0
        self.purchase_posts = 0
        self.fail_purchase_post_at = 0
        self.patch_status = 200
        self.apply_patch = True
        self.applied_patches = 0
        self.lock_gets = 0
        self.steal_on_lock_get = 0
        self.parent_status = 200

    def purchase_key(self) -> str:
        return f"ta-{PARENT}-{self.period.replace('-', '')}"

    def lock_key(self) -> str:
        return f"tl-{PARENT}"

    def record(self, method: str, path: str, body: Any) -> None:
        self.calls.append({"method": method, "path": path, "body": body})


class Handler(BaseHTTPRequestHandler):
    world: World

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return None
        return json.loads(raw.decode())

    def _send(self, code: int, payload: Any) -> None:
        raw = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _dispatch(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        body = self._body() if method in {"PUT", "POST", "PATCH"} else None
        world = self.world
        with world.lock:
            world.record(method, path, body)
            if path.startswith("/shm/v1/storage/manage/"):
                self._storage(method, path.rsplit("/", 1)[-1], body)
                return
            if path == "/shm/v1/user/service":
                self._send(world.parent_status, world.parent if world.parent_status == 200 else {"error": "down"})
                return
            if path == f"/api/users/by-username/us_{PARENT}":
                world.panel_gets += 1
                if world.fail_panel_get_at and world.panel_gets == world.fail_panel_get_at:
                    self._send(503, {"error": "unavailable"})
                    return
                if world.panel_failures > 0:
                    world.panel_failures -= 1
                    self._send(503, {"error": "unavailable"})
                    return
                self._send(200, {"response": world.user})
                return
            if path == "/api/users" and method == "PATCH":
                if world.apply_patch and isinstance(body, dict) and "trafficLimitBytes" in body:
                    world.user["trafficLimitBytes"] = body["trafficLimitBytes"]
                    world.applied_patches += 1
                self._send(world.patch_status, {"response": world.user})
                return
            self._send(404, {"error": "not found"})

    def _storage(self, method: str, name: str, body: Any) -> None:
        world = self.world
        if method == "GET":
            if name == world.lock_key():
                world.lock_gets += 1
                if world.steal_on_lock_get and world.lock_gets == world.steal_on_lock_get:
                    world.storage[name] = _foreign_lock(int(time.time()) + 3600)
            if name not in world.storage:
                self._send(404, {"error": "missing"})
                return
            self._send(200, world.storage[name])
            return
        if method == "DELETE":
            world.storage.pop(name, None)
            self._send(200, {})
            return
        if method == "PUT":
            if name in world.storage:
                self._send(400, {"error": "exists"})
                return
            world.storage[name] = body
            self._send(200, body)
            return
        if method == "POST":
            if name == world.purchase_key():
                world.purchase_posts += 1
                if world.purchase_posts == world.fail_purchase_post_at:
                    self._send(500, {"error": "write failed"})
                    return
            world.storage[name] = body
            self._send(200, body)
            return
        self._send(405, {"error": method})


def _render(base: str, settings: Any, catalog: Any, withdraw: Any, **fields: str) -> str:
    values = {
        "{{ event_name }}": fields.get("event", "CREATE"),
        "{{ user.gen_session.id }}": "session-test",
        "{{ config.api.url }}": base,
        "{{ server.settings.remnawave.api }}": base,
        "{{ server.settings.remnawave.token }}": TOKEN,
        "{{ us.id }}": fields.get("us_id", str(ADDON)),
        "{{ us.user_id }}": fields.get("user_id", str(USER)),
        "{{ us.service_id }}": fields.get("service_id", "17"),
        "{{ us.service.category }}": fields.get("category", "traffic-add-vff"),
        "{{ us.service.cost }}": fields.get("cost", "150.00"),
        "{{ toJson(us.settings) }}": json.dumps(settings, ensure_ascii=False),
        "{{ toJson(us.service.settings) }}": json.dumps(catalog, ensure_ascii=False),
        "{{ toJson(us.withdraw.get) }}": json.dumps(withdraw, ensure_ascii=False),
    }
    text = TEMPLATE.read_text()
    for key in sorted(values, key=len, reverse=True):
        text = text.replace(key, values[key])
    leftover = text.find("{{")
    if leftover != -1:
        raise AssertionError(f"unrendered placeholder left in template: {text[leftover:leftover + 80]!r}")
    return text


def _start(world: World) -> ThreadingHTTPServer:
    Handler.world = world
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def _reason(result: subprocess.CompletedProcess[str]) -> str:
    for line in _output(result).splitlines():
        if "RESULT " in line and "reason=" in line:
            return line.split("reason=", 1)[-1].strip()
    return ""


def _patches(world: World) -> List[Dict[str, Any]]:
    return [call for call in world.calls if call["method"] == "PATCH"]


def _forbidden(world: World) -> List[str]:
    found = []
    for call in world.calls:
        path = call["path"]
        method = call["method"]
        if method == "POST" and path == "/api/users":
            found.append("create user")
        if method == "DELETE" and path.startswith("/api/users"):
            found.append("delete user")
        if "reset-traffic" in path or path.endswith("/actions/enable") or path.endswith("/actions/disable"):
            found.append(path)
        if "payment" in path or path.endswith("/service/order") or "money_back" in path:
            found.append(path)
        if method == "PATCH" and path != "/api/users":
            found.append(path)
    return found


class TrafficAddonTests(DisableEnvProxiesMixin, TestCase):
    def _execute(
        self,
        world: World,
        *,
        settings: Any = None,
        catalog: Any = None,
        withdraw: Any = None,
        **fields: str,
    ) -> subprocess.CompletedProcess[str]:
        server = _start(world)
        host, port = server.server_address
        try:
            script = _render(
                f"http://{host}:{port}",
                _settings(world.period) if settings is None else settings,
                _catalog() if catalog is None else catalog,
                _withdraw(True) if withdraw is None else withdraw,
                **fields,
            )
            with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
                handle.write(script)
                path = handle.name
            try:
                return _run(path)
            finally:
                Path(path).unlink(missing_ok=True)
        finally:
            server.shutdown()
            server.server_close()

    def assert_clean(self, world: World, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(_forbidden(world), [], result.stdout + result.stderr)
        blob = result.stdout + result.stderr
        self.assertNotIn(TOKEN, blob)
        for call in _patches(world):
            self.assertEqual(set(call["body"]), {"uuid", "trafficLimitBytes"})
            self.assertEqual(call["body"]["uuid"], UUID)

    def test_01_raises_base_limit_to_stored_total(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        result = self._execute(world)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assertEqual(world.user["expireAt"], EXPIRE)
        self.assertEqual(world.user["trafficLimitStrategy"], "MONTH")
        self.assertEqual(world.user["usedTrafficBytes"], 1024)
        self.assertEqual(world.user["status"], "ACTIVE")
        self.assertEqual(len(_patches(world)), 1)
        self.assertEqual(_patches(world)[0]["body"]["trafficLimitBytes"], TARGET)
        self.assertEqual(world.storage[world.purchase_key()]["state"], "APPLIED")
        self.assert_clean(world, result)
        logged = _output(result)
        self.assertIn(PURCHASE, logged)
        self.assertIn(str(PARENT), logged)
        self.assertIn(str(ADDON), logged)
        self.assertIn(period, logged)

    def test_02_repeat_does_not_add_another_pack(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        server = _start(world)
        host, port = server.server_address
        script = _render(f"http://{host}:{port}", _settings(period), _catalog(), _withdraw(True))
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
            handle.write(script)
            path = handle.name
        try:
            first = _run(path)
            second = _run(path)
        finally:
            server.shutdown()
            server.server_close()
            Path(path).unlink(missing_ok=True)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assertNotEqual(world.user["trafficLimitBytes"], TARGET + PACK)
        self.assertEqual(len(_patches(world)), 1)
        self.assert_clean(world, second)

    def test_03_reserved_without_payment_does_not_grant(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period, state="RESERVED", amount="")
        result = self._execute(world, withdraw=_withdraw(False))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "payment_not_confirmed")
        self.assertEqual(_patches(world), [])
        self.assertEqual(world.storage[world.purchase_key()]["state"], "RESERVED")
        self.assert_clean(world, result)

    def test_04_paid_reserved_is_applied(self) -> None:
        period = _open_period()
        world = World(period)
        stored = _storage(period, state="RESERVED", amount="")
        stored["withdraw_id"] = None
        world.storage[world.purchase_key()] = stored
        result = self._execute(world, withdraw=_withdraw(True, "150.00"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved = world.storage[world.purchase_key()]
        self.assertEqual(saved["state"], "APPLIED")
        self.assertEqual(saved["withdraw_id"], 775501)
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assert_clean(world, result)

    def test_05_lost_patch_response_is_recovered_without_second_increase(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        world.patch_status = 500
        result = self._execute(world)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assertEqual(len(_patches(world)), 1)
        self.assertEqual(world.storage[world.purchase_key()]["state"], "APPLIED")
        self.assert_clean(world, result)

    def test_06_panel_outage_then_retry(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        world.panel_failures = 1
        server = _start(world)
        host, port = server.server_address
        script = _render(f"http://{host}:{port}", _settings(period), _catalog(), _withdraw(True))
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
            handle.write(script)
            path = handle.name
        try:
            first = _run(path)
            patches_after_first = len(_patches(world))
            second = _run(path)
        finally:
            server.shutdown()
            server.server_close()
            Path(path).unlink(missing_ok=True)
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        self.assertEqual(_reason(first), "panel_unavailable")
        self.assertEqual(patches_after_first, 0)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assertEqual(len(_patches(world)), 1)
        self.assert_clean(world, second)

    def test_07_other_users_subscription(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        world.parent = _parent(user_id=USER + 9)
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "parent_not_owned")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_08_tariff_outside_allow_list(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        world.parent = _parent(service_id=99)
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "tariff_not_allowed")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_09_vff_premium_is_not_eligible(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        world.parent = _parent(service_id=14)
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "tariff_not_allowed")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_10_friends_connect_is_not_eligible(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        world.parent = _parent(service_id=10, category="vpn-mz-fc")
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "parent_category_mismatch")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_11_disabled_subscription_is_not_enabled(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        world.user = _user(status="DISABLED", reset_at=_period_bounds(period)[0])
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "panel_status")
        self.assertEqual(world.user["status"], "DISABLED")
        self.assertEqual(world.user["trafficLimitBytes"], BASE)
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_12_expired_subscription_is_not_extended(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        world.user = _user(status="EXPIRED", reset_at=_period_bounds(period)[0])
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "panel_status")
        self.assertEqual(world.user["expireAt"], EXPIRE)
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_13_limited_subscription_is_raised_without_enable(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        world.user = _user(status="LIMITED", reset_at=_period_bounds(period)[0])
        result = self._execute(world)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(world.user["status"], "LIMITED")
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assert_clean(world, result)

    def test_14_closed_period_is_not_granted(self) -> None:
        period = _shift_period(_open_period(), -2)
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        result = self._execute(world, settings=_settings(period))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "period_closed")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_15_unconfirmed_reset_does_not_grant(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        previous = _shift_period(period, -1)
        world.user["lastTrafficResetAt"] = _period_bounds(previous)[0]
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "period_not_confirmed")
        self.assertEqual(world.user["trafficLimitBytes"], BASE)
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_16_manual_limit_is_not_overwritten(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        world.user["trafficLimitBytes"] = 999
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "limit_conflict")
        self.assertEqual(world.user["trafficLimitBytes"], 999)
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_16b_target_without_proof_is_a_conflict(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        world.user["trafficLimitBytes"] = TARGET
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "limit_conflict")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_17_refunded_is_terminal(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period, state="REFUNDED")
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "state_refunded")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_18_expired_purchase_is_terminal(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period, state="EXPIRED")
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "state_expired")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_19_later_catalog_change_does_not_change_paid_volume(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        result = self._execute(world, catalog=_catalog(bytes_value=1))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(_patches(world)[0]["body"]["trafficLimitBytes"], TARGET)
        self.assertEqual(world.storage[world.purchase_key()]["addon_bytes"], PACK)
        self.assertEqual(world.storage[world.purchase_key()]["applied_limit_bytes"], TARGET)
        self.assert_clean(world, result)

    def test_20_applied_write_failure_stays_unsuccessful_then_retries(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        world.fail_purchase_post_at = 2
        server = _start(world)
        host, port = server.server_address
        script = _render(f"http://{host}:{port}", _settings(period), _catalog(), _withdraw(True))
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
            handle.write(script)
            path = handle.name
        try:
            first = _run(path)
            after_first = world.storage[world.purchase_key()]["state"]
            patches_after_first = len(_patches(world))
            second = _run(path)
        finally:
            server.shutdown()
            server.server_close()
            Path(path).unlink(missing_ok=True)
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        self.assertIn("temporary", _output(first))
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assertEqual(patches_after_first, 1)
        self.assertNotEqual(after_first, "APPLIED")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(len(_patches(world)), 1)
        self.assertEqual(world.storage[world.purchase_key()]["state"], "APPLIED")
        self.assert_clean(world, second)

    def test_21_repeat_after_period_end_does_not_grant(self) -> None:
        period = _shift_period(_open_period(), -2)
        world = World(period)
        stored = _storage(period, state="APPLIED")
        stored["apply_proof"] = {
            "remnawave_uuid": UUID,
            "expire_at": EXPIRE,
            "strategy": "MONTH",
            "observed_limit_before": BASE,
            "phase": "confirmed",
        }
        world.storage[world.purchase_key()] = stored
        world.user["trafficLimitBytes"] = BASE
        result = self._execute(world, settings=_settings(period))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "period_closed")
        self.assertEqual(world.user["trafficLimitBytes"], BASE)
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_22_missing_or_invalid_parameters(self) -> None:
        period = _open_period()
        cases = [
            ({"settings": {}}, "bad_purchase_id"),
            ({"settings": {"purchase_id": "zz", "parent_user_service_id": PARENT, "period": period}}, "bad_purchase_id"),
            ({"settings": {"purchase_id": PURCHASE, "period": period}}, "bad_parent_id"),
            ({"service_id": "18"}, "unexpected_service"),
            ({"category": "vpn-mz-test"}, "unexpected_category"),
            ({"event": "PROLONGATE"}, "unexpected_event"),
        ]
        for override, reason in cases:
            world = World(period)
            world.storage[world.purchase_key()] = _storage(period)
            settings = override.get("settings", _settings(period))
            result = self._execute(
                world,
                settings=settings,
                event=override.get("event", "CREATE"),
                service_id=override.get("service_id", "17"),
                category=override.get("category", "traffic-add-vff"),
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual(_reason(result), reason, result.stdout + result.stderr)
            self.assertEqual(_patches(world), [])
            self.assert_clean(world, result)

    def test_paid_order_unknown_is_applied_without_a_new_order(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period, state="ORDER_UNKNOWN")
        result = self._execute(world)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(world.storage[world.purchase_key()]["state"], "APPLIED")
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assert_clean(world, result)

    def test_failed_state_is_not_retried_into_success(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period, state="FAILED")
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "state_failed")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_created_in_period_without_reset_is_granted(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        world.user = _user(reset_at=None, created_at=_period_bounds(period)[0])
        result = self._execute(world)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assertEqual(len(_patches(world)), 1)
        self.assert_clean(world, result)

    def test_created_in_previous_period_without_reset_is_not_granted(self) -> None:
        period = _open_period()
        previous = _shift_period(period, -1)
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        world.user = _user(reset_at=None, created_at=_period_bounds(previous)[0])
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "period_not_confirmed")
        self.assertEqual(_patches(world), [])
        self.assertEqual(world.user["trafficLimitBytes"], BASE)
        self.assert_clean(world, result)

    def test_missing_created_at_and_reset_is_not_granted(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        world.user = _user(reset_at=None)
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "period_not_confirmed")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_future_created_at_is_not_granted(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        future = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        world.user = _user(reset_at=_period_bounds(period)[0], created_at=future)
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "created_at_in_future")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_missed_scheduled_reset_is_not_granted(self) -> None:
        period = _open_period()
        previous = _shift_period(period, -1)
        older = _shift_period(period, -2)
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        world.user = _user(
            reset_at=_period_bounds(previous)[0],
            created_at=_period_bounds(older)[0],
        )
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "period_not_confirmed")
        self.assertEqual(world.user["trafficLimitBytes"], BASE)
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_crash_before_patch_does_not_change_limit(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        world.fail_purchase_post_at = 1
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertIn("temporary", _output(result))
        self.assertEqual(_patches(world), [])
        self.assertEqual(world.user["trafficLimitBytes"], BASE)
        self.assertNotIn("apply_proof", world.storage[world.purchase_key()])
        self.assert_clean(world, result)

    def test_crash_after_proof_before_patch_retries_once(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        world.steal_on_lock_get = 2
        server = _start(world)
        host, port = server.server_address
        script = _render(f"http://{host}:{port}", _settings(period), _catalog(), _withdraw(True))
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
            handle.write(script)
            path = handle.name
        try:
            first = _run(path)
            proof = world.storage[world.purchase_key()].get("apply_proof", {})
            self.assertEqual(proof.get("phase"), "patching")
            self.assertEqual(world.user["trafficLimitBytes"], BASE)
            self.assertEqual(world.storage[world.lock_key()]["lock_id"], "ffffffffffffffffffffffffffffffff")
            world.steal_on_lock_get = 0
            world.storage.pop(world.lock_key(), None)
            second = _run(path)
        finally:
            server.shutdown()
            server.server_close()
            Path(path).unlink(missing_ok=True)
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        self.assertEqual(_reason(first), "lock_lost")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(world.applied_patches, 1)
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assertEqual(world.storage[world.purchase_key()]["state"], "APPLIED")
        self.assert_clean(world, second)

    def test_patch_sent_but_not_applied_retries_without_stacking(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        world.apply_patch = False
        world.patch_status = 500
        server = _start(world)
        host, port = server.server_address
        script = _render(f"http://{host}:{port}", _settings(period), _catalog(), _withdraw(True))
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
            handle.write(script)
            path = handle.name
        try:
            first = _run(path)
            self.assertEqual(world.user["trafficLimitBytes"], BASE)
            self.assertEqual(world.storage[world.purchase_key()]["apply_proof"]["phase"], "patching")
            world.apply_patch = True
            world.patch_status = 200
            second = _run(path)
        finally:
            server.shutdown()
            server.server_close()
            Path(path).unlink(missing_ok=True)
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(world.applied_patches, 1)
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assert_clean(world, second)

    def test_confirm_get_lost_after_patch_then_retry_does_not_stack(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        world.fail_panel_get_at = 2
        server = _start(world)
        host, port = server.server_address
        script = _render(f"http://{host}:{port}", _settings(period), _catalog(), _withdraw(True))
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
            handle.write(script)
            path = handle.name
        try:
            first = _run(path)
            after_first = world.storage[world.purchase_key()]["state"]
            second = _run(path)
        finally:
            server.shutdown()
            server.server_close()
            Path(path).unlink(missing_ok=True)
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assertNotEqual(after_first, "APPLIED")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(world.applied_patches, 1)
        self.assertEqual(world.storage[world.purchase_key()]["state"], "APPLIED")
        self.assert_clean(world, second)

    def test_foreign_unexpired_lock_is_not_taken_or_deleted(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        foreign = _foreign_lock(int(time.time()) + 3600)
        world.storage[world.lock_key()] = dict(foreign)
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "lock_held")
        self.assertEqual(_patches(world), [])
        self.assertEqual(world.storage[world.lock_key()], foreign)
        self.assert_clean(world, result)

    def test_expired_foreign_lock_can_be_replaced(self) -> None:
        period = _open_period()
        world = World(period)
        world.storage[world.purchase_key()] = _storage(period)
        world.storage[world.lock_key()] = _foreign_lock(int(time.time()) - 10)
        result = self._execute(world)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assertNotIn(world.lock_key(), world.storage)
        self.assert_clean(world, result)
