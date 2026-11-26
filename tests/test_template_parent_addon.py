#!/usr/bin/env python3
"""ACTIVATE and PROLONGATE keep a paid VFF traffic pack for the current month."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "shm-remnawave.template.sh"

BASE = 322122547200
PACK = 322122547200
TARGET = BASE + PACK
PARENT = "18432"
USER = "1001"
CLOCK = "2026-11-26T12:00:00Z"
RESET = "2026-11-01T00:20:00.000Z"

MOCKS = r"""
_http_get() {
  local p="$1"
  echo "GET ${p}" >> "${CALL_LOG}"
  if [[ "${p}" == /api/users/by-username/* ]]; then
    cat "${PANEL_FILE}"
    return 0
  fi
  echo "unexpected GET ${p}" >&2
  return 1
}

_http_post() {
  local p="$1"
  shift
  echo "POST ${p}" >> "${CALL_LOG}"
  if [[ "${p}" == */actions/reset-traffic ]]; then
    echo "${p}" >> "${RESET_LOG}"
  fi
  printf '%s' '{"response":{"ok":true}}'
}

_http_patch() {
  local p="$1"
  shift
  echo "PATCH ${p}" >> "${CALL_LOG}"
  local body=""
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--data" ]]; then
      body="$2"
      shift 2
      continue
    fi
    shift
  done
  printf '%s' "${body}" > "${PATCH_BODY}"
  printf '%s' '{"response":{"ok":true}}'
}

_http_delete() {
  echo "DELETE $1" >> "${CALL_LOG}"
  printf '%s' '{"response":{"ok":true}}'
}
"""


def _catalog(parent_ids: Optional[List[int]] = None) -> Dict[str, Any]:
    return {
        "traffic_addon": {
            "brand": "vff",
            "parent_category": "vpn-mz-test",
            "parent_service_ids": parent_ids if parent_ids is not None else [3, 4, 5, 6],
            "bytes": PACK,
            "strategy": "MONTH",
            "period": "remnawave_monthly_reset",
            "reset_cron": "20 0 1 * *",
            "reset_timezone": "UTC",
            "max_purchases_per_period": 1,
        }
    }


def _bounds(period: str) -> tuple[str, str]:
    year, month = (int(part) for part in period.split("-"))
    start = f"{year:04d}-{month:02d}-01T00:20:00Z"
    if month == 12:
        end = f"{year + 1:04d}-01-01T00:20:00Z"
    else:
        end = f"{year:04d}-{month + 1:02d}-01T00:20:00Z"
    return start, end


def _row(
    *,
    period: str = "2026-11",
    status: str = "ACTIVE",
    parent: str = PARENT,
    service_id: int = 99001,
    applied: int = TARGET,
    expected: str = "150.00",
    base: int = BASE,
    addon: int = PACK,
) -> Dict[str, Any]:
    start, end = _bounds(period)
    return {
        "user_service_id": service_id,
        "user_id": int(USER),
        "service_id": 17,
        "status": status,
        "settings": {
            "purchase_id": "0123456789abcdef0123456789abcdef",
            "parent_user_service_id": int(parent),
            "period": period,
            "period_starts_at": start,
            "period_ends_at": end,
            "base_limit_bytes": base,
            "addon_bytes": addon,
            "applied_limit_bytes": applied,
            "expected_amount": expected,
        },
    }


def _withdraw(
    service_id: int = 99001,
    *,
    total: Any = "150.00",
    paid: bool = True,
    user_id: int = int(USER),
) -> Dict[str, Any]:
    return {
        "withdraw_id": 775501,
        "user_id": user_id,
        "user_service_id": service_id,
        "service_id": 17,
        "total": total,
        "withdraw_date": "2026-11-26T00:00:00Z" if paid else None,
    }


def _render(
    *,
    event: str,
    category: str = "vpn-mz-test",
    service_id: str = "3",
    strategy: str = "MONTH",
    limit: str = str(BASE),
    rows: Any = None,
    withdraws: Any = None,
    catalog: Any = None,
    rows_raw: Optional[str] = None,
    withdraws_raw: Optional[str] = None,
) -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    replacements = {
        "{{ event_name }}": event,
        "{{ user.gen_session.id }}": "test-session",
        "{{ config.api.url }}": "https://shm.test",
        "{{ server.settings.remnawave.api }}": "https://panel.test",
        "{{ server.settings.remnawave.token }}": "test-token",
        "{{ server.settings.remnawave.default_internal_squad_name }}": "Default-Squad",
        "{{ us.service.settings.remnawave.internal_squad_name }}": "",
        "{{ toJson(us.service.settings.remnawave.internal_squad_pool_names) }}": "",
        "{{ us.service.settings.remnawave.external_squad_name }}": "",
        "{{ us.service.settings.remnawave.traffic_limit_bytes }}": limit,
        "{{ us.service.settings.remnawave.traffic_limit_strategy }}": strategy,
        "{{ us.service.settings.remnawave.hwid_device_limit }}": "",
        "{{ server.settings.remnawave.shm_tz }}": "UTC",
        "{{ server.settings.remnawave.expire_safety_minutes }}": "0",
        "{{ us.id }}": PARENT,
        "{{ server.settings.remnawave.sanitize_username }}": "false",
        "{{ us.expire }}": "2026-12-31 23:59:59",
        "{{ us.service.category }}": category,
        "{{ us.service_id }}": service_id,
        "{{ us.user_id }}": USER,
        "{{ toJson(service(17).config) }}": json.dumps(catalog if catalog is not None else _catalog()),
        "{{ toJson(user.services.list(where = { service_id = 17, status = 'ANY' })) }}": rows_raw
        if rows_raw is not None
        else json.dumps([] if rows is None else rows),
        "{{ toJson(user.wd.list(where = { service_id = 17 })) }}": withdraws_raw
        if withdraws_raw is not None
        else json.dumps([] if withdraws is None else withdraws),
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
    marker = 'log "Remnawave Template'
    cut = text.find(marker)
    if cut < 0:
        raise AssertionError("could not find template case/start marker")
    return text[:cut] + MOCKS + text[cut:]


def _run(
    event: str,
    *,
    panel_limit: Optional[int] = BASE,
    reset: Optional[str] = RESET,
    clock: str = CLOCK,
    **kwargs: Any,
) -> Dict[str, Any]:
    rendered = _render(event=event, **kwargs)
    panel: Dict[str, Any] = {"response": {"id": 77, "username": f"us_{PARENT}"}}
    if panel_limit is not None:
        panel["response"]["trafficLimitBytes"] = panel_limit
    if reset is not None:
        panel["response"]["lastTrafficResetAt"] = reset
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "rendered.sh")
        call_log = os.path.join(tmp, "calls.log")
        reset_log = os.path.join(tmp, "resets.log")
        patch_body = os.path.join(tmp, "patch.json")
        panel_file = os.path.join(tmp, "panel.json")
        clock_file = os.path.join(tmp, "clock")
        Path(script).write_text(rendered, encoding="utf-8")
        Path(panel_file).write_text(json.dumps(panel), encoding="utf-8")
        Path(clock_file).write_text(clock + "\n", encoding="utf-8")
        env = os.environ.copy()
        env["CALL_LOG"] = call_log
        env["RESET_LOG"] = reset_log
        env["PATCH_BODY"] = patch_body
        env["PANEL_FILE"] = panel_file
        env["TRAFFIC_ADDON_CLOCK_FILE"] = clock_file
        proc = subprocess.run(
            ["bash", script],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        patch = None
        if Path(patch_body).exists() and Path(patch_body).stat().st_size:
            patch = json.loads(Path(patch_body).read_text(encoding="utf-8"))
        resets = [
            line
            for line in Path(reset_log).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ] if Path(reset_log).exists() else []
        calls = Path(call_log).read_text(encoding="utf-8").splitlines() if Path(call_log).exists() else []
        return {"code": proc.returncode, "stderr": proc.stderr, "patch": patch, "resets": resets, "calls": calls}


class ParentAddonTests(unittest.TestCase):
    def test_standard_without_pack_keeps_catalog_limit(self) -> None:
        result = _run("PROLONGATE")
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["patch"]["trafficLimitBytes"], BASE)
        self.assertEqual(result["patch"]["trafficLimitStrategy"], "MONTH")
        self.assertEqual(result["patch"]["status"], "ACTIVE")
        self.assertEqual(result["patch"]["id"], 77)
        self.assertIn("expireAt", result["patch"])
        self.assertEqual(result["resets"], [])

    def test_paid_active_pack(self) -> None:
        result = _run("PROLONGATE", rows=[_row(status="ACTIVE")], withdraws=[_withdraw()])
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["patch"]["trafficLimitBytes"], TARGET)

    def test_paid_removed_pack(self) -> None:
        result = _run("PROLONGATE", rows=[_row(status="REMOVED")], withdraws=[_withdraw()])
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["patch"]["trafficLimitBytes"], TARGET)

    def test_activate_with_pack_enables_and_keeps_total(self) -> None:
        result = _run("ACTIVATE", rows=[_row()], withdraws=[_withdraw()])
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["patch"]["trafficLimitBytes"], TARGET)
        self.assertTrue(any(line.endswith("/actions/enable") for line in result["calls"]))

    def test_prolongate_with_pack_does_not_reset_month_traffic(self) -> None:
        result = _run("PROLONGATE", rows=[_row()], withdraws=[_withdraw()])
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["patch"]["trafficLimitBytes"], TARGET)
        self.assertEqual(result["resets"], [])

    def test_paid_progress_already_at_applied_limit(self) -> None:
        result = _run(
            "PROLONGATE",
            panel_limit=TARGET,
            rows=[_row(status="PROGRESS")],
            withdraws=[_withdraw()],
        )
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["patch"]["trafficLimitBytes"], TARGET)

    def test_paid_progress_before_patch_uses_stored_total(self) -> None:
        result = _run(
            "PROLONGATE",
            panel_limit=BASE,
            rows=[_row(status="PROGRESS")],
            withdraws=[_withdraw()],
        )
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["patch"]["trafficLimitBytes"], TARGET)

    def test_unpaid_pack_keeps_catalog_limit(self) -> None:
        result = _run("PROLONGATE", rows=[_row()], withdraws=[_withdraw(paid=False)])
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["patch"]["trafficLimitBytes"], BASE)

    def test_previous_period_is_not_carried_after_confirmed_reset(self) -> None:
        result = _run(
            "PROLONGATE",
            rows=[_row(period="2026-10", status="REMOVED")],
            withdraws=[_withdraw()],
        )
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["patch"]["trafficLimitBytes"], BASE)

    def test_period_boundary_without_confirmed_reset_keeps_pack(self) -> None:
        result = _run(
            "PROLONGATE",
            clock="2026-12-01T00:30:00Z",
            reset="2026-11-01T00:20:00.000Z",
            rows=[_row(period="2026-11", status="REMOVED")],
            withdraws=[_withdraw()],
        )
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["patch"]["trafficLimitBytes"], TARGET)

    def test_unreadable_history_does_not_lower_elevated_limit(self) -> None:
        result = _run("PROLONGATE", panel_limit=TARGET, rows_raw="null")
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["patch"]["trafficLimitBytes"], TARGET)
        missing = _run("PROLONGATE", panel_limit=None, rows_raw="null")
        self.assertEqual(missing["code"], 1, missing["stderr"])
        self.assertIsNone(missing["patch"])

    def test_amount_mismatch_does_not_grant_or_lower(self) -> None:
        rows = [_row(status="REMOVED")]
        withdraws = [_withdraw(total="100.00")]
        elevated = _run("PROLONGATE", panel_limit=TARGET, rows=rows, withdraws=withdraws)
        self.assertEqual(elevated["code"], 0, elevated["stderr"])
        self.assertEqual(elevated["patch"]["trafficLimitBytes"], TARGET)
        base = _run("PROLONGATE", panel_limit=BASE, rows=rows, withdraws=withdraws)
        self.assertEqual(base["code"], 0, base["stderr"])
        self.assertEqual(base["patch"]["trafficLimitBytes"], BASE)

    def test_catalog_change_after_purchase_uses_stored_total(self) -> None:
        catalog = _catalog()
        catalog["traffic_addon"]["bytes"] = 1000
        result = _run(
            "PROLONGATE",
            catalog=catalog,
            rows=[_row(status="REMOVED", expected="150.00", applied=TARGET)],
            withdraws=[_withdraw(total="150.00")],
        )
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["patch"]["trafficLimitBytes"], TARGET)

    def test_pack_of_another_subscription_is_ignored(self) -> None:
        result = _run(
            "PROLONGATE",
            rows=[_row(parent="99999", status="REMOVED")],
            withdraws=[_withdraw()],
        )
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["patch"]["trafficLimitBytes"], BASE)

    def test_friends_connect_and_premium_are_unchanged(self) -> None:
        friends = _run(
            "PROLONGATE",
            category="vpn-mz-fc",
            service_id="8",
            limit="111",
            rows=[_row(status="REMOVED")],
            withdraws=[_withdraw()],
        )
        self.assertEqual(friends["code"], 0, friends["stderr"])
        self.assertEqual(friends["patch"]["trafficLimitBytes"], 111)
        premium = _run(
            "PROLONGATE",
            category="vpn-mz-test",
            service_id="14",
            limit="222",
            rows=[_row(status="REMOVED")],
            withdraws=[_withdraw()],
        )
        self.assertEqual(premium["code"], 0, premium["stderr"])
        self.assertEqual(premium["patch"]["trafficLimitBytes"], 222)

    def test_bad_withdraw_total_on_removed_is_not_a_grant(self) -> None:
        rows = [_row(status="REMOVED")]
        withdraws = [_withdraw(total="bad")]
        elevated = _run("PROLONGATE", panel_limit=TARGET, rows=rows, withdraws=withdraws)
        self.assertEqual(elevated["code"], 0, elevated["stderr"])
        self.assertEqual(elevated["patch"]["trafficLimitBytes"], TARGET)
        base = _run("PROLONGATE", panel_limit=BASE, rows=rows, withdraws=withdraws)
        self.assertEqual(base["code"], 0, base["stderr"])
        self.assertEqual(base["patch"]["trafficLimitBytes"], BASE)

    def test_scalar_service_map_still_finds_the_pack(self) -> None:
        row = _row(status="REMOVED")
        result = _run(
            "PROLONGATE",
            rows_raw=json.dumps({str(row["user_service_id"]): row}),
            withdraws_raw=json.dumps({"775501": _withdraw()}),
        )
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["patch"]["trafficLimitBytes"], TARGET)


if __name__ == "__main__":
    unittest.main()
