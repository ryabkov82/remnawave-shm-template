#!/usr/bin/env python3
"""Lifecycle tests: PROLONGATE reset is strategy-dependent."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "shm-remnawave.template.sh"

USER_ID = 77
INTERNAL_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
VFF_UUID = "11111111-1111-1111-1111-111111111111"

MOCKS = r"""
_http_get() {
  local p="$1"
  echo "GET ${p}" >> "${CALL_LOG}"
  if [[ "${p}" == /api/users/by-username/* ]]; then
    printf '%s' '{"response":{"id":77}}'
    return 0
  fi
  if [[ "${p}" == /api/subscriptions/by-username/* ]]; then
    printf '%s' '{"response":{"subscriptionUrl":"https://sub.test/x"}}'
    return 0
  fi
  if [[ "${p}" == /api/internal-squads ]]; then
    printf '%s' '{"response":{"internalSquads":[{"name":"Default-Squad","uuid":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}]}}'
    return 0
  fi
  if [[ "${p}" == /api/external-squads ]]; then
    printf '%s' '{"response":{"externalSquads":[{"name":"VPN-for-Friends","uuid":"11111111-1111-1111-1111-111111111111"}]}}'
    return 0
  fi
  echo "unexpected GET ${p}" >&2
  return 1
}

_http_post() {
  local p="$1"
  shift
  echo "POST ${p}" >> "${CALL_LOG}"
  if [[ "${p}" == "/api/users" ]]; then
    printf '%s' '{"response":{"id":77}}'
    return 0
  fi
  if [[ "${p}" == */actions/reset-traffic ]]; then
    echo "${p}" >> "${RESET_LOG}"
    printf '%s' '{"response":{"ok":true}}'
    return 0
  fi
  if [[ "${p}" == */actions/enable || "${p}" == */actions/disable || "${p}" == */actions/revoke ]]; then
    printf '%s' '{"response":{"ok":true}}'
    return 0
  fi
  echo "unexpected POST ${p}" >&2
  return 1
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
  local p="$1"
  echo "DELETE ${p}" >> "${CALL_LOG}"
  printf '%s' '{"response":{"ok":true}}'
}

curl() {
  echo "CURL $*" >> "${CALL_LOG}"
  echo 200
}
"""


def _render_full(
    *,
    event: str,
    traffic_limit_bytes: str = "",
    traffic_limit_strategy: str = "",
    hwid: str = "",
    expire: str = "2026-12-31 23:59:59",
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
        "{{ us.service.settings.remnawave.external_squad_name }}": "",
        "{{ us.service.settings.remnawave.traffic_limit_bytes }}": traffic_limit_bytes,
        "{{ us.service.settings.remnawave.traffic_limit_strategy }}": traffic_limit_strategy,
        "{{ us.service.settings.remnawave.hwid_device_limit }}": hwid,
        "{{ server.settings.remnawave.shm_tz }}": "UTC",
        "{{ server.settings.remnawave.expire_safety_minutes }}": "0",
        "{{ us.id }}": "42",
        "{{ server.settings.remnawave.sanitize_username }}": "false",
        "{{ us.expire }}": expire,
        "{{ user.login }}": "alice",
        "{{ user.full_name }}": "Alice Example",
        "{{ user.settings.telegram.login }}": "alice_tg",
    }
    for needle, value in replacements.items():
        text = text.replace(needle, value)
    leftover = re.findall(r"\{\{[^}]+\}\}", text)
    if leftover:
        raise AssertionError(f"unreplaced placeholders: {leftover}")
    marker = 'log "Remnawave Template'
    cut = text.find(marker)
    if cut < 0:
        raise AssertionError("could not find template case/start marker")
    return text[:cut] + MOCKS + text[cut:]


def _run_event(
    event: str,
    *,
    traffic_limit_bytes: str = "",
    traffic_limit_strategy: str = "",
    hwid: str = "",
) -> Dict[str, Any]:
    rendered = _render_full(
        event=event,
        traffic_limit_bytes=traffic_limit_bytes,
        traffic_limit_strategy=traffic_limit_strategy,
        hwid=hwid,
    )
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "rendered.sh")
        call_log = os.path.join(tmp, "calls.log")
        reset_log = os.path.join(tmp, "resets.log")
        patch_body = os.path.join(tmp, "patch.json")
        Path(script).write_text(rendered, encoding="utf-8")
        Path(call_log).write_text("", encoding="utf-8")
        Path(reset_log).write_text("", encoding="utf-8")
        env = os.environ.copy()
        env["CALL_LOG"] = call_log
        env["RESET_LOG"] = reset_log
        env["PATCH_BODY"] = patch_body
        proc = subprocess.run(
            ["bash", script],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        calls = Path(call_log).read_text(encoding="utf-8").splitlines()
        resets = [
            line
            for line in Path(reset_log).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        patch: Optional[Dict[str, Any]] = None
        if Path(patch_body).exists() and Path(patch_body).stat().st_size:
            patch = json.loads(Path(patch_body).read_text(encoding="utf-8"))
        return {
            "code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "calls": calls,
            "resets": resets,
            "patch": patch,
        }


def _post_paths(result: Dict[str, Any]) -> List[str]:
    return [line[5:] for line in result["calls"] if line.startswith("POST ")]


class TemplateProlongateResetTests(unittest.TestCase):
    def test_no_reset_prolongate_resets_once_then_patches(self) -> None:
        result = _run_event(
            "PROLONGATE",
            traffic_limit_bytes="322122547200",
            traffic_limit_strategy="NO_RESET",
        )
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(len(result["resets"]), 1)
        self.assertTrue(result["resets"][0].endswith("/actions/reset-traffic"))
        self.assertIn(f"/api/users/{USER_ID}/actions/reset-traffic", result["resets"][0])
        self.assertIsNotNone(result["patch"])
        self.assertEqual(result["patch"]["id"], USER_ID)
        self.assertEqual(result["patch"]["trafficLimitBytes"], 322122547200)
        self.assertEqual(result["patch"]["trafficLimitStrategy"], "NO_RESET")
        self.assertRegex(
            result["patch"]["expireAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
        )
        self.assertIn("status", result["patch"])
        posts = _post_paths(result)
        self.assertEqual(
            [p for p in posts if p.endswith("/actions/reset-traffic")],
            [f"/api/users/{USER_ID}/actions/reset-traffic"],
        )
        patch_idx = next(
            i for i, line in enumerate(result["calls"]) if line.startswith("PATCH ")
        )
        reset_idx = next(
            i for i, line in enumerate(result["calls"]) if "reset-traffic" in line
        )
        self.assertLess(reset_idx, patch_idx)
        self.assertIn("Traffic reset on PROLONGATE: enabled (strategy=NO_RESET)", result["stdout"])
        self.assertNotIn("+ reset traffic", result["stdout"])

    def test_month_prolongate_skips_reset(self) -> None:
        result = _run_event(
            "PROLONGATE",
            traffic_limit_bytes="322122547200",
            traffic_limit_strategy="MONTH",
        )
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["resets"], [])
        self.assertFalse(any("reset-traffic" in line for line in result["calls"]))
        self.assertIsNotNone(result["patch"])
        self.assertEqual(result["patch"]["trafficLimitBytes"], 322122547200)
        self.assertEqual(result["patch"]["trafficLimitStrategy"], "MONTH")
        self.assertRegex(
            result["patch"]["expireAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
        )
        self.assertIn(
            "Traffic reset on PROLONGATE: skipped (strategy=MONTH; managed by Remnawave)",
            result["stdout"],
        )

    def test_week_prolongate_skips_reset(self) -> None:
        result = _run_event("PROLONGATE", traffic_limit_strategy="WEEK")
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["resets"], [])

    def test_day_prolongate_skips_reset(self) -> None:
        result = _run_event("PROLONGATE", traffic_limit_strategy="DAY")
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["resets"], [])

    def test_lowercase_month_normalizes_and_skips_reset(self) -> None:
        result = _run_event("PROLONGATE", traffic_limit_strategy="month")
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["resets"], [])
        self.assertEqual(result["patch"]["trafficLimitStrategy"], "MONTH")
        self.assertIn("strategy=MONTH", result["stdout"])

    def test_invalid_strategy_fails_without_mutations(self) -> None:
        result = _run_event("PROLONGATE", traffic_limit_strategy="MONTH_ROLLING")
        self.assertNotEqual(result["code"], 0)
        self.assertIn("Invalid traffic_limit_strategy", result["stderr"])
        self.assertEqual(result["resets"], [])
        self.assertFalse(any(line.startswith("PATCH ") for line in result["calls"]))
        self.assertFalse(any(line.startswith("POST ") for line in result["calls"]))

    def test_create_month_does_not_reset(self) -> None:
        result = _run_event(
            "CREATE",
            traffic_limit_bytes="107374182400",
            traffic_limit_strategy="MONTH",
        )
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["resets"], [])
        self.assertFalse(any("reset-traffic" in line for line in result["calls"]))
        self.assertIn("POST /api/users", result["calls"])

    def test_activate_month_does_not_reset(self) -> None:
        result = _run_event(
            "ACTIVATE",
            traffic_limit_bytes="107374182400",
            traffic_limit_strategy="MONTH",
        )
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["resets"], [])
        self.assertTrue(any(line.startswith("PATCH ") for line in result["calls"]))

    def test_update_does_not_reset(self) -> None:
        result = _run_event(
            "UPDATE",
            traffic_limit_bytes="100",
            traffic_limit_strategy="NO_RESET",
        )
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["resets"], [])
        self.assertFalse(any("reset-traffic" in line for line in result["calls"]))
        self.assertFalse(any(line.startswith("PATCH ") for line in result["calls"]))

    def test_antiblock_month_skips_manual_reset(self) -> None:
        result = _run_event(
            "PROLONGATE",
            traffic_limit_bytes="107374182400",
            traffic_limit_strategy="MONTH",
        )
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["resets"], [])
        self.assertEqual(result["patch"]["trafficLimitBytes"], 107374182400)
        self.assertEqual(result["patch"]["trafficLimitStrategy"], "MONTH")

    def test_future_standard_month_skips_manual_reset(self) -> None:
        result = _run_event(
            "PROLONGATE",
            traffic_limit_bytes="322122547200",
            traffic_limit_strategy="MONTH",
        )
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["resets"], [])
        self.assertEqual(result["patch"]["trafficLimitBytes"], 322122547200)
        self.assertEqual(result["patch"]["trafficLimitStrategy"], "MONTH")
        self.assertIn("expireAt", result["patch"])


class TemplateProlongateSourceTests(unittest.TestCase):
    def test_reset_helper_uses_effective_strategy(self) -> None:
        text = TEMPLATE.read_text(encoding="utf-8")
        prolongate = text.split("PROLONGATE)", 1)[1].split("UPDATE)", 1)[0]
        self.assertIn("_reset_traffic_on_prolongate_if_needed", prolongate)
        self.assertNotIn("_reset_user_traffic", prolongate)
        self.assertNotIn("+ reset traffic", prolongate)
        self.assertIn("_effective_traffic_limit_strategy", text)
        self.assertIn("_should_reset_traffic_on_prolongate", text)

    def test_reset_user_traffic_call_sites(self) -> None:
        text = TEMPLATE.read_text(encoding="utf-8")
        calls = [
            line
            for line in text.splitlines()
            if "_reset_user_traffic" in line and not line.strip().startswith("#")
        ]
        definitions = [line for line in calls if line.strip().startswith("_reset_user_traffic()")]
        invocations = [line for line in calls if line not in definitions]
        self.assertEqual(len(definitions), 1)
        self.assertEqual(len(invocations), 1)
        self.assertIn("_reset_traffic_on_prolongate_if_needed", text)
        self.assertTrue(any("_reset_user_traffic" in line for line in invocations))


if __name__ == "__main__":
    unittest.main()
