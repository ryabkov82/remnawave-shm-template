#!/usr/bin/env python3
"""CREATE of remnawave_traffic_addon raises one existing user's limit.

The template reads the paid purchase from the rendered withdraw and from
user_services.settings. It does not use storage, locks, or its own state.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import TestCase
from urllib.parse import parse_qs, urlsplit

from tests.proxy_env import DisableEnvProxiesMixin


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "shm-remnawave-traffic-addon.template.sh"

BASE = 322122547200
PACK = 322122547200
TARGET = BASE + PACK
PARENT = 18432
SIBLING = 18439
ADDON = 99001
USER = 1001
PURCHASE = "0123456789abcdef0123456789abcdef"
PANEL_ID = 10
VLESS_UUID = "11111111-1111-4111-8111-111111111111"
EXPIRE = "2026-12-15T00:00:00.000Z"
TOKEN = "test-token-must-not-appear"
_UNSET = object()
_NULL = object()


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


def _stamp_epoch(stamp: str) -> int:
    return int(datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


def _grant(period: str, status: str, us_id: int = 99002) -> tuple[Dict[str, Any], Dict[str, Any]]:
    start, end = _period_bounds(period)
    row = {
        "user_service_id": us_id,
        "user_id": USER,
        "service_id": 17,
        "status": status,
        "settings": {
            "purchase_id": "abcdef0123456789abcdef0123456789",
            "parent_user_service_id": PARENT,
            "period": period,
            "period_starts_at": start,
            "period_ends_at": end,
            "base_limit_bytes": BASE,
            "addon_bytes": PACK,
            "applied_limit_bytes": TARGET,
            "expected_amount": "150.00",
        },
    }
    money = {
        "withdraw_id": 775502,
        "user_id": USER,
        "user_service_id": us_id,
        "service_id": 17,
        "total": "150.00",
        "withdraw_date": "2026-12-01T01:00:00Z",
    }
    return row, money


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
    start, end = _period_bounds(period)
    return {
        "purchase_id": PURCHASE,
        "parent_user_service_id": PARENT,
        "period": period,
        "period_starts_at": start,
        "period_ends_at": end,
        "base_limit_bytes": BASE,
        "addon_bytes": PACK,
        "applied_limit_bytes": TARGET,
        "expected_amount": "150.00",
    }


# Object rendered by toJson(us.withdraw.pairs) for user service 11450 on
# SHM 3.7.0. total is a JSON number. The same object is rendered for CREATE
# and REMOVE. The flat list from us.withdraw.get is not accepted.
PRODUCTION_WITHDRAW = {
    "bonus": 0,
    "cost": 150,
    "create_date": "2026-11-27 17:40:24",
    "discount": 0,
    "end_date": "2026-11-27 17:40:23",
    "months": 0,
    "qnt": 1,
    "service_id": 17,
    "total": 150,
    "user_id": 763,
    "user_service_id": 11450,
    "withdraw_date": "2026-11-27 17:40:24",
    "withdraw_id": 15406,
}


def _withdraw(paid: bool, total: str = "150.00", **overrides: Any) -> Any:
    row: Dict[str, Any] = {
        "withdraw_id": 775501,
        "user_id": USER,
        "user_service_id": ADDON,
        "service_id": 17,
        "total": total,
        "withdraw_date": "2026-11-26T00:00:00Z" if paid else None,
    }
    row.update(overrides)
    return row


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
        "id": PANEL_ID,
        "username": f"us_{PARENT}",
        "status": status,
        "trafficLimitBytes": limit,
        "trafficLimitStrategy": "MONTH",
        "lastTrafficResetAt": reset_at,
        "expireAt": EXPIRE,
        "hwidDeviceLimit": 3,
        "description": "kept",
        "vlessUuid": VLESS_UUID,
        "userTraffic": {"usedTrafficBytes": 1024, "lifetimeUsedTrafficBytes": 2048},
    }
    if created_at is not None:
        user["createdAt"] = created_at
    return user


class World:
    def __init__(self, period: str) -> None:
        self.lock = threading.Lock()
        self.period = period
        self.user = _user(
            reset_at=_period_bounds(period)[0].replace("Z", ".000Z"),
            created_at="2026-10-02T00:32:25.000Z",
        )
        primary = _parent()["data"][0]
        sibling = dict(primary)
        sibling["user_service_id"] = SIBLING
        self.parent = {"data": [primary, sibling]}
        self.reveal_user_id = False
        self.calls: List[Dict[str, Any]] = []
        self.panel_failures = 0
        self.panel_gets = 0
        self.fail_panel_get_at = 0
        self.patch_status = 200
        self.apply_patch = True
        self.applied_patches = 0
        self.parent_status = 200
        self.jump_clock_on = ""
        self.clock_jump_to = 0
        self.clock_file = ""
        self.prolong_expire = ""
        self.swap_id_on_get = 0
        self.reply_username = ""
        self.panel_missing = False

    def record(self, method: str, path: str, body: Any, query: str = "") -> None:
        self.calls.append({"method": method, "path": path, "body": body, "query": query})


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
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parsed.query
        body = self._body() if method in {"PUT", "POST", "PATCH"} else None
        world = self.world
        with world.lock:
            world.record(method, path, body, query)
            if path == "/shm/v1/user/service":
                if world.parent_status != 200:
                    self._send(world.parent_status, {"error": "down"})
                    return
                rows = world.parent.get("data") if isinstance(world.parent, dict) else None
                rows = list(rows) if isinstance(rows, list) else []
                params = parse_qs(query, keep_blank_values=True)
                if "filter" in params:
                    try:
                        filt = json.loads(params["filter"][0])
                    except json.JSONDecodeError:
                        rows = []
                    else:
                        wanted = filt.get("user_service_id") if isinstance(filt, dict) else None
                        rows = [row for row in rows if str(row.get("user_service_id")) == str(wanted)]
                public = []
                for row in rows:
                    item = dict(row)
                    if not world.reveal_user_id:
                        item.pop("user_id", None)
                    public.append(item)
                self._send(200, {"data": public})
                return
            if re.fullmatch(r"/api/users/by-username/us_\d+", path):
                absent = {"errorCode": "A063", "message": "User with specified params not found"}
                if path != f"/api/users/by-username/{world.user.get('username', '')}":
                    self._send(404, absent)
                    return
                world.panel_gets += 1
                if world.panel_missing:
                    self._send(404, absent)
                    return
                if world.jump_clock_on == "reread" and world.panel_gets == 2 and world.clock_file:
                    Path(world.clock_file).write_text(str(world.clock_jump_to))
                if world.fail_panel_get_at and world.panel_gets == world.fail_panel_get_at:
                    self._send(503, {"error": "unavailable"})
                    return
                if world.swap_id_on_get and world.panel_gets == world.swap_id_on_get:
                    changed = dict(world.user)
                    changed["id"] = int(world.user["id"]) + 1
                    self._send(200, {"response": changed})
                    return
                if world.reply_username:
                    changed = dict(world.user)
                    changed["username"] = world.reply_username
                    self._send(200, {"response": changed})
                    return
                if world.panel_failures > 0:
                    world.panel_failures -= 1
                    self._send(503, {"error": "unavailable"})
                    return
                self._send(200, {"response": world.user})
                return
            if path == "/api/users" and method == "PATCH":
                if world.jump_clock_on == "patch" and world.clock_file:
                    Path(world.clock_file).write_text(str(world.clock_jump_to))
                if (
                    world.apply_patch
                    and isinstance(body, dict)
                    and body.get("username") == world.user.get("username")
                    and "trafficLimitBytes" in body
                ):
                    world.user["trafficLimitBytes"] = body["trafficLimitBytes"]
                    world.applied_patches += 1
                if world.prolong_expire:
                    world.user["expireAt"] = world.prolong_expire
                self._send(world.patch_status, {"response": world.user})
                return
            self._send(404, {"error": "not found"})


def _render(
    base: str,
    settings: Any,
    catalog: Any,
    withdraw: Any,
    services: Any = _UNSET,
    withdraws: Any = _UNSET,
    **fields: str,
) -> str:
    values = {
        "{{ toJson(event_name) }}": json.dumps(fields.get("event", "CREATE"), ensure_ascii=False),
        "{{ toJson(user.services.items(where = { service_id = 17, status = 'ANY' }).pluck('pairs')) }}": json.dumps(
            [] if services is _UNSET else services, ensure_ascii=False
        ),
        "{{ toJson(user.wd.items(where = { service_id = 17 }).pluck('pairs')) }}": json.dumps(
            [] if withdraws is _UNSET else withdraws, ensure_ascii=False
        ),
        "{{ toJson(user.gen_session.id) }}": json.dumps("session-test", ensure_ascii=False),
        "{{ toJson(config.api.url) }}": json.dumps(base, ensure_ascii=False),
        "{{ toJson(server.settings.remnawave.api) }}": json.dumps(base, ensure_ascii=False),
        "{{ toJson(server.settings.remnawave.token) }}": json.dumps(TOKEN, ensure_ascii=False),
        "{{ toJson(us.id) }}": json.dumps(int(fields.get("us_id", str(ADDON)))),
        "{{ toJson(us.user_id) }}": json.dumps(int(fields.get("user_id", str(USER)))),
        "{{ toJson(us.service_id) }}": json.dumps(int(fields.get("service_id", "17"))),
        "{{ toJson(us.service.category) }}": json.dumps(fields.get("category", "traffic-add-vff"), ensure_ascii=False),
        "{{ toJson(us.service.cost) }}": json.dumps(fields.get("cost", "150.00"), ensure_ascii=False),
        "{{ toJson(us.settings) }}": json.dumps(settings, ensure_ascii=False),
        "{{ toJson(us.service.settings) }}": "null" if catalog is _NULL else json.dumps(catalog, ensure_ascii=False),
        "{{ toJson(us.withdraw.pairs) }}": json.dumps(withdraw, ensure_ascii=False),
    }
    text = TEMPLATE.read_text()
    text = text.replace("# {{ IF event_name == 'REMOVE' }}", "")
    text = text.replace("# {{ END }}", "")
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


def _run(script: str, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        ["bash", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=run_env,
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


def _parent_filters(world: World) -> List[str]:
    return [call["query"] for call in world.calls if call["path"] == "/shm/v1/user/service"]


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
        if "storage/manage" in path:
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
        env: Optional[Dict[str, str]] = None,
        services: Any = _UNSET,
        withdraws: Any = _UNSET,
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
                services,
                withdraws,
                **fields,
            )
            with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
                handle.write(script)
                path = handle.name
            try:
                return _run(path, env)
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
            self.assertEqual(set(call["body"]), {"username", "trafficLimitBytes"})
            self.assertEqual(call["body"]["username"], world.user["username"])
            self.assertNotIn("uuid", call["body"])
            self.assertNotIn("vlessUuid", call["body"])

    def _clock(self, world: World, jump_to: int) -> Dict[str, str]:
        handle = tempfile.NamedTemporaryFile("w", delete=False)
        handle.write(str(int(time.time())))
        handle.close()
        world.clock_file = handle.name
        world.clock_jump_to = jump_to
        self.addCleanup(Path(handle.name).unlink, missing_ok=True)
        return {"TRAFFIC_ADDON_CLOCK_FILE": handle.name}

    def test_01_raises_base_limit_to_stored_total(self) -> None:
        period = _open_period()
        world = World(period)
        result = self._execute(world)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assertEqual(world.user["expireAt"], EXPIRE)
        self.assertEqual(world.user["trafficLimitStrategy"], "MONTH")
        self.assertEqual(world.user["userTraffic"]["usedTrafficBytes"], 1024)
        self.assertEqual(world.user["status"], "ACTIVE")
        self.assertEqual(world.user["id"], PANEL_ID)
        self.assertEqual(world.user["hwidDeviceLimit"], 3)
        self.assertEqual(world.user["description"], "kept")
        self.assertEqual(world.user["vlessUuid"], VLESS_UUID)
        self.assertNotIn("uuid", world.user)
        self.assertEqual(len(_patches(world)), 1)
        self.assertEqual(_patches(world)[0]["body"]["trafficLimitBytes"], TARGET)
        self.assertIn("filter=%7B%22user_service_id%22%3A18432%7D", _parent_filters(world))
        self.assert_clean(world, result)

    def test_02_repeat_does_not_add_another_pack(self) -> None:
        period = _open_period()
        world = World(period)
        world.user["trafficLimitBytes"] = TARGET
        result = self._execute(world)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def _production_world(self) -> World:
        world = World(_open_period())
        world.parent = _parent(user_id=763)
        return world

    def test_03_pairs_withdraw_pays_and_grants(self) -> None:
        world = self._production_world()
        result = self._execute(world, withdraw=PRODUCTION_WITHDRAW, us_id="11450", user_id="763")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assertEqual(len(_patches(world)), 1)
        self.assert_clean(world, result)

    def test_03_pairs_withdraw_rejects_foreign_owner_instance_and_amount(self) -> None:
        foreign_user = dict(PRODUCTION_WITHDRAW)
        foreign_user["user_id"] = 764
        foreign_service = dict(PRODUCTION_WITHDRAW)
        foreign_service["user_service_id"] = 11451
        short = dict(PRODUCTION_WITHDRAW)
        short["total"] = 149
        for withdraw in (foreign_user, foreign_service, short):
            world = self._production_world()
            result = self._execute(world, withdraw=withdraw, us_id="11450", user_id="763")
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual(_reason(result), "payment_mismatch")
            self.assertEqual(_patches(world), [])

    def test_03_flat_withdraw_list_is_not_parsed(self) -> None:
        flat = [item for pair in PRODUCTION_WITHDRAW.items() for item in pair]
        world = self._production_world()
        result = self._execute(world, withdraw=flat, us_id="11450", user_id="763")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "withdraw_unreadable")
        self.assertEqual(_patches(world), [])

    def test_03_remove_accepts_the_pairs_withdraw(self) -> None:
        period = "2026-11"
        _, end = _period_bounds(period)
        world = World(period)
        world.parent = _parent(user_id=763)
        world.user["trafficLimitBytes"] = TARGET
        world.user["lastTrafficResetAt"] = end.replace("Z", ".000Z")
        result = self._remove(
            world,
            _stamp_epoch(end) + 3600,
            withdraw=PRODUCTION_WITHDRAW,
            us_id="11450",
            user_id="763",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "restored")
        self.assertEqual(world.user["trafficLimitBytes"], BASE)
        self.assertEqual(len(_patches(world)), 1)
        self.assertIn("filter=%7B%22user_service_id%22%3A18432%7D", _parent_filters(world))

    def test_03_remove_rejects_a_revealed_foreign_parent(self) -> None:
        period = "2026-11"
        _, end = _period_bounds(period)
        world = World(period)
        world.parent = _parent(user_id=USER + 9)
        world.reveal_user_id = True
        world.user["trafficLimitBytes"] = TARGET
        world.user["lastTrafficResetAt"] = end.replace("Z", ".000Z")
        result = self._remove(world, _stamp_epoch(end) + 3600)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "parent_not_owned")
        self.assertEqual(_patches(world), [])
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)

    def test_03_remove_rejects_a_non_addon_row_in_history(self) -> None:
        period = "2026-11"
        _, end = _period_bounds(period)
        standard = {
            "user_service_id": 7742,
            "user_id": 763,
            "service_id": 3,
            "status": "ACTIVE",
            "settings": {},
        }
        world = World(period)
        world.user["trafficLimitBytes"] = TARGET
        world.user["lastTrafficResetAt"] = end.replace("Z", ".000Z")
        result = self._remove(world, _stamp_epoch(end) + 3600, services=[standard], withdraws=[])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "history_unreadable")
        self.assertEqual(_patches(world), [])
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)

    def test_03_unpaid_withdraw_does_not_grant(self) -> None:
        period = _open_period()
        world = World(period)
        result = self._execute(world, withdraw=_withdraw(False))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "payment_not_confirmed")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_05_lost_patch_response_is_recovered_without_second_increase(self) -> None:
        period = _open_period()
        world = World(period)
        world.patch_status = 500
        result = self._execute(world)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assertEqual(len(_patches(world)), 1)
        self.assert_clean(world, result)

    def test_06_panel_outage_then_retry(self) -> None:
        period = _open_period()
        world = World(period)
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
        world.parent = _parent(user_id=USER + 9)
        world.reveal_user_id = True
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "parent_not_owned")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_07b_several_subscriptions_require_the_parent_filter(self) -> None:
        period = _open_period()
        world = World(period)
        world.parent = {"data": [_parent()["data"][0], _parent(service_id=4)["data"][0]]}
        world.parent["data"][1]["user_service_id"] = SIBLING
        result = self._execute(world)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assertIn("filter=%7B%22user_service_id%22%3A18432%7D", _parent_filters(world))
        self.assert_clean(world, result)

        missed = World(period)
        missed.parent = {"data": [_parent()["data"][0]]}
        missed.parent["data"][0]["user_service_id"] = SIBLING
        missed_result = self._execute(missed)
        self.assertEqual(missed_result.returncode, 1)
        self.assertEqual(_reason(missed_result), "parent_not_owned")
        self.assertEqual(_patches(missed), [])

    def test_07c_hidden_user_id_still_grants_the_owned_parent(self) -> None:
        world = World("2026-11")
        owned = _parent(user_id=763)["data"][0]
        owned["user_service_id"] = 2699
        other = dict(owned)
        other["user_service_id"] = 7742
        world.parent = {"data": [other, owned]}
        world.user["username"] = "us_2699"
        settings = _settings("2026-11")
        settings["parent_user_service_id"] = 2699
        result = self._execute(
            world,
            settings=settings,
            withdraw=PRODUCTION_WITHDRAW,
            us_id="11450",
            user_id="763",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "applied")
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assertEqual(world.user["userTraffic"]["usedTrafficBytes"], 1024)
        self.assertEqual(world.user["expireAt"], EXPIRE)
        self.assertIn("filter=%7B%22user_service_id%22%3A2699%7D", _parent_filters(world))
        self.assertEqual(len(_patches(world)), 1)
        self.assert_clean(world, result)

    def test_08_tariff_outside_allow_list(self) -> None:
        period = _open_period()
        world = World(period)
        world.parent = _parent(service_id=14)
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "tariff_not_allowed")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_09_vff_premium_is_not_eligible(self) -> None:
        period = _open_period()
        world = World(period)
        world.parent = _parent(service_id=14, category="vpn-mz-test")
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "tariff_not_allowed")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_10_friends_connect_is_not_eligible(self) -> None:
        period = _open_period()
        world = World(period)
        world.parent = _parent(service_id=10, category="vpn-mz-fc")
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertIn(_reason(result), {"parent_category_mismatch", "tariff_not_allowed"})
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_11_disabled_subscription_is_not_enabled(self) -> None:
        period = _open_period()
        world = World(period)
        world.user["status"] = "DISABLED"
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "panel_status")
        self.assertEqual(_patches(world), [])
        self.assertEqual(world.user["status"], "DISABLED")
        self.assert_clean(world, result)

    def test_12_expired_subscription_is_not_extended(self) -> None:
        period = _open_period()
        world = World(period)
        world.user["status"] = "EXPIRED"
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "panel_status")
        self.assertEqual(_patches(world), [])
        self.assertEqual(world.user["expireAt"], EXPIRE)
        self.assert_clean(world, result)

    def test_13_limited_subscription_is_raised_without_enable(self) -> None:
        period = _open_period()
        world = World(period)
        world.user["status"] = "LIMITED"
        result = self._execute(world)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(world.user["status"], "LIMITED")
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assert_clean(world, result)

    def test_14_closed_period_is_not_granted(self) -> None:
        period = _shift_period(_open_period(), -2)
        world = World(period)
        result = self._execute(world, settings=_settings(period))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "period_closed")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_15_unconfirmed_reset_does_not_grant(self) -> None:
        period = _open_period()
        previous = _shift_period(period, -1)
        world = World(period)
        world.user = _user(reset_at=_period_bounds(previous)[0])
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "period_not_confirmed")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_16_manual_limit_is_not_overwritten(self) -> None:
        period = _open_period()
        world = World(period)
        world.user["trafficLimitBytes"] = BASE + 1
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "limit_conflict")
        self.assertEqual(world.user["trafficLimitBytes"], BASE + 1)
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_16b_target_without_prior_patch_is_not_increased(self) -> None:
        period = _open_period()
        world = World(period)
        world.user["trafficLimitBytes"] = TARGET
        result = self._execute(world)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(_patches(world), [])
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assert_clean(world, result)

    def test_19_later_catalog_change_does_not_change_paid_volume(self) -> None:
        period = _open_period()
        world = World(period)
        result = self._execute(world, catalog=_catalog(bytes_value=1))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(_patches(world)[0]["body"]["trafficLimitBytes"], TARGET)
        self.assert_clean(world, result)

    def test_21_repeat_after_period_end_does_not_grant(self) -> None:
        period = _shift_period(_open_period(), -2)
        world = World(period)
        world.user["trafficLimitBytes"] = BASE
        result = self._execute(world, settings=_settings(period))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "period_closed")
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
        for fields, reason in cases:
            world = World(period)
            settings = fields.get("settings", _settings(period))
            result = self._execute(
                world,
                settings=settings,
                event=fields.get("event", "CREATE"),
                service_id=fields.get("service_id", "17"),
                category=fields.get("category", "traffic-add-vff"),
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual(_reason(result), reason, result.stdout + result.stderr)
            self.assertEqual(_patches(world), [])

    def test_created_in_period_without_reset_is_granted(self) -> None:
        period = _open_period()
        world = World(period)
        world.user = _user(reset_at=None, created_at=_period_bounds(period)[0])
        result = self._execute(world)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assert_clean(world, result)

    def test_created_in_previous_period_without_reset_is_not_granted(self) -> None:
        period = _open_period()
        previous = _shift_period(period, -1)
        world = World(period)
        world.user = _user(reset_at=None, created_at=_period_bounds(previous)[0])
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "period_not_confirmed")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_missing_created_at_and_reset_is_not_granted(self) -> None:
        period = _open_period()
        world = World(period)
        world.user = _user(reset_at=None, created_at=None)
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "period_not_confirmed")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_future_created_at_is_not_granted(self) -> None:
        period = _open_period()
        world = World(period)
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
        world.user = _user(reset_at=_period_bounds(previous)[0], created_at=_period_bounds(older)[0])
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "period_not_confirmed")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_patch_sent_but_not_applied_retries_without_stacking(self) -> None:
        period = _open_period()
        world = World(period)
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
            limit_after_first = world.user["trafficLimitBytes"]
            world.apply_patch = True
            world.patch_status = 200
            second = _run(path)
        finally:
            server.shutdown()
            server.server_close()
            Path(path).unlink(missing_ok=True)
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        self.assertEqual(limit_after_first, BASE)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(world.applied_patches, 1)
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assert_clean(world, second)

    def test_confirm_get_lost_after_patch_then_retry_does_not_stack(self) -> None:
        period = _open_period()
        world = World(period)
        world.fail_panel_get_at = 3
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
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(world.applied_patches, 1)
        self.assert_clean(world, second)

    def test_period_boundary_before_patch_does_not_grant(self) -> None:
        period = _open_period()
        end = datetime.strptime(_period_bounds(period)[1], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        world = World(period)
        world.jump_clock_on = "reread"
        result = self._execute(world, env=self._clock(world, int(end.timestamp())))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "period_closed")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_period_boundary_during_patch_is_not_success(self) -> None:
        period = _open_period()
        end = datetime.strptime(_period_bounds(period)[1], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        world = World(period)
        world.jump_clock_on = "patch"
        result = self._execute(world, env=self._clock(world, int(end.timestamp())))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "period_crossed")
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assertIn(f"period_ends_at={_period_bounds(period)[1]}", result.stderr)
        self.assert_clean(world, result)

    def test_substituted_user_service_id_is_rejected(self) -> None:
        period = _open_period()
        world = World(period)
        result = self._execute(world, withdraw=_withdraw(True, user_service_id=42))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "payment_mismatch")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_withdraw_for_other_user_is_rejected(self) -> None:
        period = _open_period()
        world = World(period)
        result = self._execute(world, withdraw=_withdraw(True, user_id=USER + 3))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "payment_mismatch")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_active_user_with_past_expire_is_not_granted(self) -> None:
        period = _open_period()
        world = World(period)
        world.user["expireAt"] = "2020-01-01T00:00:00.000Z"
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "subscription_expired")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_concurrent_prolongation_keeps_applied_limit(self) -> None:
        period = _open_period()
        world = World(period)
        prolonged = "2027-06-01T00:00:00.000Z"
        world.prolong_expire = prolonged
        result = self._execute(world)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assertEqual(world.user["expireAt"], prolonged)
        self.assertEqual(set(_patches(world)[0]["body"]), {"username", "trafficLimitBytes"})
        self.assert_clean(world, result)

    def test_temp_files_removed_on_success_and_error(self) -> None:
        period = _open_period()
        with tempfile.TemporaryDirectory() as tmp:
            world = World(period)
            ok = self._execute(world, env={"TMPDIR": tmp})
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
            self.assertEqual(list(Path(tmp).glob("traffic-addon.*")), [])
            failed = World(period)
            bad = self._execute(failed, withdraw=_withdraw(False), env={"TMPDIR": tmp})
            self.assertEqual(bad.returncode, 1)
            self.assertEqual(list(Path(tmp).glob("traffic-addon.*")), [])
            self.assert_clean(world, ok)

    def test_tls_verification_is_not_disabled(self) -> None:
        text = TEMPLATE.read_text()
        self.assertNotIn("curl -k", text)
        self.assertNotIn("curl -skS", text)
        self.assertIn("curl -sS", text)
        self.assertNotIn("storage/manage", text)
        self.assertNotIn("snapshot_mac", text)

    def test_shell_placeholders_are_not_evaluated(self) -> None:
        period = _open_period()
        world = World(period)
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "pwned"
            result = self._execute(world, category=f"$(touch {marker})")
            self.assertEqual(result.returncode, 1)
            self.assertEqual(_reason(result), "unexpected_category")
            self.assertFalse(marker.exists())
            self.assert_clean(world, result)

    def test_inconsistent_limits_are_rejected(self) -> None:
        period = _open_period()
        world = World(period)
        settings = _settings(period)
        settings["applied_limit_bytes"] = TARGET + 1
        result = self._execute(world, settings=settings)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "bad_settings")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_confirm_id_must_match_patched_user(self) -> None:
        period = _open_period()
        world = World(period)
        world.swap_id_on_get = 3
        result = self._execute(world)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "panel_user_mismatch")
        self.assertIn("level=permanent", result.stderr)
        self.assertEqual(len(_patches(world)), 1)
        self.assertEqual(_patches(world)[0]["body"]["username"], f"us_{PARENT}")
        self.assert_clean(world, result)

    def test_response_without_internal_id_is_malformed(self) -> None:
        period = _open_period()
        world = World(period)
        world.user.pop("id")
        result = self._execute(world)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "panel_malformed")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_username_mismatch_is_rejected(self) -> None:
        period = _open_period()
        world = World(period)
        world.reply_username = "us_other"
        result = self._execute(world)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "panel_username_mismatch")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_parent_progress_during_activate_is_temporary(self) -> None:
        period = _open_period()
        world = World(period)
        world.parent = _parent(status="PROGRESS")
        result = self._execute(world)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "parent_in_progress")
        self.assertIn("level=temporary", result.stderr)
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_removed_parent_is_not_granted(self) -> None:
        period = _open_period()
        world = World(period)
        world.parent = _parent(status="REMOVED")
        result = self._execute(world)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "parent_not_active")
        self.assertIn("level=permanent", result.stderr)
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_paid_total_must_match_expected_amount(self) -> None:
        period = _open_period()
        world = World(period)
        result = self._execute(world, withdraw=_withdraw(True, total="149.00"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "payment_mismatch")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def test_expected_amount_comes_from_settings_not_a_constant(self) -> None:
        period = _open_period()
        world = World(period)
        settings = _settings(period)
        settings["expected_amount"] = "200.50"
        result = self._execute(
            world,
            settings=settings,
            withdraw=_withdraw(True, total=200.50),
            cost="999.00",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assertNotIn("150", result.stdout + result.stderr)
        self.assert_clean(world, result)

    def test_parent_not_active_is_rejected(self) -> None:
        period = _open_period()
        world = World(period)
        world.parent = _parent(status="BLOCK")
        result = self._execute(world)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "parent_not_active")
        self.assertEqual(_patches(world), [])
        self.assert_clean(world, result)

    def _fixed_clock(self, epoch: int) -> Dict[str, str]:
        handle = tempfile.NamedTemporaryFile("w", delete=False)
        handle.write(str(epoch))
        handle.close()
        self.addCleanup(Path(handle.name).unlink, missing_ok=True)
        return {"TRAFFIC_ADDON_CLOCK_FILE": handle.name}

    def _remove(self, world: World, epoch: int, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return self._execute(world, env=self._fixed_clock(epoch), event="REMOVE", **kwargs)

    def test_remove_before_period_end_retries(self) -> None:
        period = "2026-11"
        _, end = _period_bounds(period)
        world = World(period)
        world.user["trafficLimitBytes"] = TARGET
        result = self._remove(world, _stamp_epoch(end) - 60)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "period_open")
        self.assertEqual(_patches(world), [])
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)

    def test_remove_without_confirmed_reset_retries(self) -> None:
        period = "2026-11"
        _, end = _period_bounds(period)
        world = World(period)
        world.user["trafficLimitBytes"] = TARGET
        world.user["lastTrafficResetAt"] = _period_bounds(period)[0].replace("Z", ".000Z")
        result = self._remove(world, _stamp_epoch(end) + 3600)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "reset_not_confirmed")
        self.assertEqual(_patches(world), [])

    def test_remove_restores_saved_base(self) -> None:
        period = "2026-11"
        _, end = _period_bounds(period)
        world = World(period)
        world.user["trafficLimitBytes"] = TARGET
        world.user["lastTrafficResetAt"] = end.replace("Z", ".000Z")
        result = self._remove(world, _stamp_epoch(end) + 3600)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "restored")
        self.assertEqual(world.user["trafficLimitBytes"], BASE)
        self.assertEqual(world.user["expireAt"], EXPIRE)
        self.assertEqual(world.user["status"], "ACTIVE")
        self.assertEqual(len(_patches(world)), 1)
        self.assertEqual(_patches(world)[0]["body"]["trafficLimitBytes"], BASE)
        self.assert_clean(world, result)

    def test_remove_repeat_is_success_without_patch(self) -> None:
        period = "2026-11"
        _, end = _period_bounds(period)
        world = World(period)
        world.user["trafficLimitBytes"] = BASE
        world.user["lastTrafficResetAt"] = end.replace("Z", ".000Z")
        result = self._remove(world, _stamp_epoch(end) + 60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "already_base")
        self.assertEqual(_patches(world), [])

    def test_remove_lost_patch_is_confirmed_by_reread(self) -> None:
        period = "2026-11"
        _, end = _period_bounds(period)
        world = World(period)
        world.user["trafficLimitBytes"] = TARGET
        world.user["lastTrafficResetAt"] = end.replace("Z", ".000Z")
        world.patch_status = 500
        result = self._remove(world, _stamp_epoch(end) + 60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "restored")
        self.assertEqual(len(_patches(world)), 1)
        self.assertEqual(world.user["trafficLimitBytes"], BASE)

    def test_remove_keeps_manual_high_low_and_zero(self) -> None:
        period = "2026-11"
        _, end = _period_bounds(period)
        epoch = _stamp_epoch(end) + 60
        for limit, reason in ((TARGET + 1, "limit_kept"), (1, "limit_kept"), (0, "limit_kept")):
            world = World(period)
            world.user["trafficLimitBytes"] = limit
            world.user["lastTrafficResetAt"] = end.replace("Z", ".000Z")
            result = self._remove(world, epoch)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(_reason(result), reason)
            self.assertEqual(world.user["trafficLimitBytes"], limit)
            self.assertEqual(_patches(world), [])

    def test_remove_does_not_lower_a_newer_paid_pack(self) -> None:
        period = "2026-11"
        _, end = _period_bounds(period)
        newer, money = _grant("2026-12", "ACTIVE")
        world = World(period)
        world.user["trafficLimitBytes"] = TARGET
        world.user["lastTrafficResetAt"] = end.replace("Z", ".000Z")
        result = self._remove(world, _stamp_epoch(end) + 60, services=[newer], withdraws=[money])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "newer_pack")
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        self.assertEqual(_patches(world), [])

    def test_remove_does_not_lower_paid_progress_of_the_new_period(self) -> None:
        period = "2026-11"
        _, end = _period_bounds(period)
        newer, money = _grant("2026-12", "PROGRESS")
        world = World(period)
        world.user["trafficLimitBytes"] = TARGET
        world.user["lastTrafficResetAt"] = end.replace("Z", ".000Z")
        result = self._remove(world, _stamp_epoch(end) + 60, services=[newer], withdraws=[money])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "newer_pack")
        self.assertEqual(_patches(world), [])

    def test_remove_of_blocked_and_expired_parent_restores_base(self) -> None:
        period = "2026-11"
        _, end = _period_bounds(period)
        world = World(period)
        world.parent = _parent(status="BLOCK")
        world.user["status"] = "DISABLED"
        world.user["expireAt"] = "2026-11-20T00:00:00.000Z"
        world.user["trafficLimitBytes"] = TARGET
        world.user["lastTrafficResetAt"] = end.replace("Z", ".000Z")
        result = self._remove(world, _stamp_epoch(end) + 60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "restored")
        self.assertEqual(world.user["trafficLimitBytes"], BASE)
        self.assertEqual(world.user["expireAt"], "2026-11-20T00:00:00.000Z")
        self.assertEqual(world.user["status"], "DISABLED")
        self.assert_clean(world, result)

    def test_remove_of_deleted_parent_succeeds_without_patch(self) -> None:
        period = "2026-11"
        _, end = _period_bounds(period)
        world = World(period)
        world.parent_status = 404
        world.panel_missing = True
        world.user["trafficLimitBytes"] = TARGET
        result = self._remove(world, _stamp_epoch(end) + 60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "parent_absent")
        self.assertEqual(_patches(world), [])
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)

    def test_remove_retries_when_history_or_panel_cannot_be_read(self) -> None:
        period = "2026-11"
        _, end = _period_bounds(period)
        epoch = _stamp_epoch(end) + 60
        world = World(period)
        world.user["trafficLimitBytes"] = TARGET
        world.user["lastTrafficResetAt"] = end.replace("Z", ".000Z")
        result = self._remove(world, epoch, services=None)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "history_unreadable")
        self.assertEqual(_patches(world), [])
        world = World(period)
        world.user["trafficLimitBytes"] = TARGET
        world.user["lastTrafficResetAt"] = end.replace("Z", ".000Z")
        world.panel_failures = 2
        result = self._remove(world, epoch)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_reason(result), "panel_unavailable")
        self.assertEqual(_patches(world), [])

    def test_remove_ignores_disabled_catalog(self) -> None:
        period = "2026-11"
        _, end = _period_bounds(period)
        world = World(period)
        world.user["trafficLimitBytes"] = TARGET
        world.user["lastTrafficResetAt"] = end.replace("Z", ".000Z")
        result = self._remove(world, _stamp_epoch(end) + 60, catalog=_NULL)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "restored")
        self.assertEqual(world.user["trafficLimitBytes"], BASE)
        create = World(period)
        refused = self._execute(create, catalog=_NULL)
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(_reason(refused), "unsupported_catalog")
        self.assertEqual(_patches(create), [])

    def test_remove_retries_when_another_pack_cannot_be_excluded(self) -> None:
        period = "2026-11"
        _, end = _period_bounds(period)
        epoch = _stamp_epoch(end) + 60
        newer, money = _grant("2026-12", "ACTIVE")
        broken = dict(newer)
        broken["settings"] = "not-an-object"
        cases = (
            [broken],
            [money],
            [newer, "bad-element"],
            {"row": newer, "junk": "x"},
        )
        for services in cases:
            world = World(period)
            world.user["trafficLimitBytes"] = TARGET
            world.user["lastTrafficResetAt"] = end.replace("Z", ".000Z")
            result = self._remove(world, epoch, services=services, withdraws=[money])
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual(_reason(result), "history_unreadable")
            self.assertEqual(_patches(world), [])
            self.assertEqual(world.user["trafficLimitBytes"], TARGET)
        mismatch = dict(money)
        mismatch["total"] = "10.00"
        world = World(period)
        world.user["trafficLimitBytes"] = TARGET
        world.user["lastTrafficResetAt"] = end.replace("Z", ".000Z")
        result = self._remove(world, epoch, services=[newer], withdraws=[mismatch])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "history_unreadable")
        self.assertEqual(_patches(world), [])
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)

    def test_remove_retries_when_another_pack_end_date_is_invalid(self) -> None:
        period = "2026-11"
        _, end = _period_bounds(period)
        epoch = _stamp_epoch(end) + 60
        for raw_end in ("2026-02-31T00:20:00Z", "not-a-date", "2026-12-01 03:20:00"):
            newer, money = _grant("2026-12", "ACTIVE")
            newer["settings"]["period_ends_at"] = raw_end
            world = World(period)
            world.user["trafficLimitBytes"] = TARGET
            world.user["lastTrafficResetAt"] = end.replace("Z", ".000Z")
            result = self._remove(world, epoch, services=[newer], withdraws=[money])
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual(_reason(result), "history_unreadable")
            self.assertEqual(_patches(world), [])
            self.assertEqual(world.user["trafficLimitBytes"], TARGET)

    def test_remove_without_confirmed_parent_does_not_patch(self) -> None:
        period = "2026-11"
        _, end = _period_bounds(period)
        epoch = _stamp_epoch(end) + 60
        world = World(period)
        world.parent_status = 404
        world.user["trafficLimitBytes"] = TARGET
        world.user["lastTrafficResetAt"] = end.replace("Z", ".000Z")
        result = self._remove(world, epoch)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "parent_unconfirmed")
        self.assertEqual(_patches(world), [])
        self.assertEqual(world.user["trafficLimitBytes"], TARGET)

        world = World(period)
        world.parent = {"data": []}
        world.user["trafficLimitBytes"] = TARGET
        world.user["lastTrafficResetAt"] = end.replace("Z", ".000Z")
        result = self._remove(world, epoch)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "parent_unconfirmed")
        self.assertEqual(_patches(world), [])

        world = World(period)
        world.parent = {"data": []}
        world.panel_missing = True
        world.user["trafficLimitBytes"] = TARGET
        result = self._remove(world, epoch)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "parent_absent")
        self.assertEqual(_patches(world), [])

        world = World(period)
        world.parent = _parent(status="REMOVED")
        world.panel_missing = True
        world.user["trafficLimitBytes"] = TARGET
        result = self._remove(world, epoch)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "parent_absent")
        self.assertEqual(_patches(world), [])

        world = World(period)
        world.parent = _parent(status="REMOVED")
        world.user["trafficLimitBytes"] = TARGET
        world.user["lastTrafficResetAt"] = end.replace("Z", ".000Z")
        result = self._remove(world, epoch)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(_reason(result), "restored")
        self.assertEqual(world.user["trafficLimitBytes"], BASE)
        self.assertEqual(len(_patches(world)), 1)
