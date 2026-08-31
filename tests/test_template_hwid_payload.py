#!/usr/bin/env python3
"""Regression tests for SHM template CREATE/UPDATE JSON payloads."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "shm-remnawave.template.sh"

INTERNAL_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
VFF_UUID = "11111111-1111-1111-1111-111111111111"


def _render(
    *,
    hwid: Optional[str],
    omit_hwid_placeholder: bool = False,
    traffic_limit_bytes: str = "",
    traffic_limit_strategy: str = "",
    external_squad_name: str = "",
    expire: str = "2026-12-31 23:59:59",
    expire_safety_minutes: str = "0",
) -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    replacements = {
        "{{ event_name }}": "NOOP",
        "{{ user.gen_session.id }}": "test-session",
        "{{ config.api.url }}": "https://shm.test",
        "{{ server.settings.remnawave.api }}": "https://panel.test",
        "{{ server.settings.remnawave.token }}": "test-token",
        "{{ server.settings.remnawave.default_internal_squad_name }}": "Default-Squad",
        "{{ us.service.settings.remnawave.internal_squad_name }}": "",
        "{{ us.service.settings.remnawave.external_squad_name }}": external_squad_name,
        "{{ us.service.settings.remnawave.traffic_limit_bytes }}": traffic_limit_bytes,
        "{{ us.service.settings.remnawave.traffic_limit_strategy }}": traffic_limit_strategy,
        "{{ server.settings.remnawave.shm_tz }}": "UTC",
        "{{ server.settings.remnawave.expire_safety_minutes }}": expire_safety_minutes,
        "{{ us.id }}": "42",
        "{{ server.settings.remnawave.sanitize_username }}": "false",
        "{{ us.expire }}": expire,
        "{{ user.login }}": "alice",
        "{{ user.full_name }}": "Alice Example",
        "{{ user.settings.telegram.login }}": "alice_tg",
    }
    if omit_hwid_placeholder:
        text = text.replace(
            'SERVICE_HWID_DEVICE_LIMIT="{{ us.service.settings.remnawave.hwid_device_limit }}"',
            'SERVICE_HWID_DEVICE_LIMIT=""',
        )
    else:
        value = "" if hwid is None else hwid
        replacements[
            "{{ us.service.settings.remnawave.hwid_device_limit }}"
        ] = value

    for needle, value in replacements.items():
        text = text.replace(needle, value)

    leftover = re.findall(r"\{\{[^}]+\}\}", text)
    if leftover:
        raise AssertionError(f"unreplaced placeholders: {leftover}")

    cut = text.find('log "Remnawave Template')
    if cut < 0:
        raise AssertionError("could not find template case/start marker")
    return text[:cut]


def _run_payload(
    mode: str,
    *,
    hwid: Optional[str],
    omit_hwid_placeholder: bool = False,
    **kwargs: Any,
) -> Tuple[int, str, str]:
    rendered = _render(
        hwid=hwid, omit_hwid_placeholder=omit_hwid_placeholder, **kwargs
    )
    driver = r"""
_resolve_internal_squad_uuid_by_name() { echo '%s'; }
_resolve_external_squad_uuid_by_name() { echo '%s'; }
if [[ "${PAYLOAD_MODE}" == "create" ]]; then
  _build_create_payload
elif [[ "${PAYLOAD_MODE}" == "update" ]]; then
  _build_update_payload 99
elif [[ "${PAYLOAD_MODE}" == "log" ]]; then
  _hwid_device_limit_log_label
else
  echo "unknown mode" >&2
  exit 2
fi
""" % (
        INTERNAL_UUID,
        VFF_UUID,
    )
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "rendered.sh")
        Path(script).write_text(rendered + driver, encoding="utf-8")
        env = os.environ.copy()
        env["PAYLOAD_MODE"] = mode
        proc = subprocess.run(
            ["bash", script],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr


def _parse_json(stdout: str) -> Dict[str, Any]:
    return json.loads(stdout)


class TemplateHwidPayloadTests(unittest.TestCase):
    def test_create_absent_omits_hwid_key(self) -> None:
        code, out, err = _run_payload("create", hwid=None, omit_hwid_placeholder=True)
        self.assertEqual(code, 0, err)
        payload = _parse_json(out)
        self.assertNotIn("hwidDeviceLimit", payload)

    def test_create_empty_omits_hwid_key(self) -> None:
        code, out, err = _run_payload("create", hwid="")
        self.assertEqual(code, 0, err)
        payload = _parse_json(out)
        self.assertNotIn("hwidDeviceLimit", payload)

    def test_create_null_omits_hwid_key(self) -> None:
        code, out, err = _run_payload("create", hwid="null")
        self.assertEqual(code, 0, err)
        payload = _parse_json(out)
        self.assertNotIn("hwidDeviceLimit", payload)

    def test_create_zero_is_integer_zero(self) -> None:
        code, out, err = _run_payload("create", hwid="0")
        self.assertEqual(code, 0, err)
        payload = _parse_json(out)
        self.assertIn("hwidDeviceLimit", payload)
        self.assertIsInstance(payload["hwidDeviceLimit"], int)
        self.assertEqual(payload["hwidDeviceLimit"], 0)

    def test_create_three_is_integer_three(self) -> None:
        code, out, err = _run_payload("create", hwid="3")
        self.assertEqual(code, 0, err)
        payload = _parse_json(out)
        self.assertIsInstance(payload["hwidDeviceLimit"], int)
        self.assertEqual(payload["hwidDeviceLimit"], 3)

    def test_create_invalid_value_fails(self) -> None:
        for value in ("-1", "1.5", "abc", "3.0", "1e2"):
            with self.subTest(value=value):
                code, out, err = _run_payload("create", hwid=value)
                self.assertNotEqual(code, 0)
                self.assertIn("Invalid hwid_device_limit", err)
                self.assertEqual(out.strip(), "")

    def test_update_absent_omits_hwid_key(self) -> None:
        code, out, err = _run_payload("update", hwid=None, omit_hwid_placeholder=True)
        self.assertEqual(code, 0, err)
        payload = _parse_json(out)
        self.assertNotIn("hwidDeviceLimit", payload)
        self.assertNotIn("hwidDeviceLimit", out)

    def test_update_null_omits_hwid_key(self) -> None:
        code, out, err = _run_payload("update", hwid="null")
        self.assertEqual(code, 0, err)
        payload = _parse_json(out)
        self.assertNotIn("hwidDeviceLimit", payload)
        self.assertNotIn('"hwidDeviceLimit": null', out)
        self.assertNotIn('"hwidDeviceLimit": 0', out)

    def test_update_empty_omits_hwid_key(self) -> None:
        code, out, err = _run_payload("update", hwid="")
        self.assertEqual(code, 0, err)
        payload = _parse_json(out)
        self.assertNotIn("hwidDeviceLimit", payload)

    def test_update_zero_is_integer_zero(self) -> None:
        code, out, err = _run_payload("update", hwid="0")
        self.assertEqual(code, 0, err)
        payload = _parse_json(out)
        self.assertIsInstance(payload["hwidDeviceLimit"], int)
        self.assertEqual(payload["hwidDeviceLimit"], 0)

    def test_update_three_is_integer_three(self) -> None:
        code, out, err = _run_payload("update", hwid="3")
        self.assertEqual(code, 0, err)
        payload = _parse_json(out)
        self.assertIsInstance(payload["hwidDeviceLimit"], int)
        self.assertEqual(payload["hwidDeviceLimit"], 3)

    def test_update_invalid_value_fails(self) -> None:
        for value in ("-1", "1.5", "nope"):
            with self.subTest(value=value):
                code, _out, err = _run_payload("update", hwid=value)
                self.assertNotEqual(code, 0)
                self.assertIn("Invalid hwid_device_limit", err)

    def test_log_label_not_zero_for_absent(self) -> None:
        code, out, err = _run_payload("log", hwid="null")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "panel default")
        self.assertNotEqual(out.strip(), "0")

        code, out, err = _run_payload("log", hwid="0")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "disabled (0)")

        code, out, err = _run_payload("log", hwid="3")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "3")

    def test_create_keeps_traffic_squad_expire_external_defaults(self) -> None:
        code, out, err = _run_payload("create", hwid="null")
        self.assertEqual(code, 0, err)
        payload = _parse_json(out)
        self.assertEqual(payload["trafficLimitBytes"], 0)
        self.assertIsInstance(payload["trafficLimitBytes"], int)
        self.assertEqual(payload["trafficLimitStrategy"], "NO_RESET")
        self.assertEqual(payload["activeInternalSquads"], [INTERNAL_UUID])
        self.assertIsNone(payload["externalSquadUuid"])
        self.assertRegex(payload["expireAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(payload["username"], "us_42")
        self.assertEqual(payload["status"], "ACTIVE")

    def test_create_explicit_traffic_and_external_squad(self) -> None:
        code, out, err = _run_payload(
            "create",
            hwid="3",
            traffic_limit_bytes="53687091200",
            traffic_limit_strategy="MONTH",
            external_squad_name="VPN-for-Friends",
        )
        self.assertEqual(code, 0, err)
        payload = _parse_json(out)
        self.assertEqual(payload["trafficLimitBytes"], 53687091200)
        self.assertEqual(payload["trafficLimitStrategy"], "MONTH")
        self.assertEqual(payload["externalSquadUuid"], VFF_UUID)
        self.assertEqual(payload["hwidDeviceLimit"], 3)
        self.assertEqual(payload["activeInternalSquads"], [INTERNAL_UUID])

    def test_update_keeps_traffic_and_expire_and_numeric_id(self) -> None:
        code, out, err = _run_payload(
            "update",
            hwid="null",
            traffic_limit_bytes="100",
            traffic_limit_strategy="DAY",
        )
        self.assertEqual(code, 0, err)
        payload = _parse_json(out)
        self.assertEqual(payload["id"], 99)
        self.assertIsInstance(payload["id"], int)
        self.assertEqual(payload["trafficLimitBytes"], 100)
        self.assertEqual(payload["trafficLimitStrategy"], "DAY")
        self.assertNotIn("hwidDeviceLimit", payload)
        self.assertNotIn("externalSquadUuid", payload)
        self.assertNotIn("activeInternalSquads", payload)
        self.assertRegex(payload["expireAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_activate_and_prolongate_use_update_payload(self) -> None:
        text = TEMPLATE.read_text(encoding="utf-8")
        activate = text.split("ACTIVATE)", 1)[1].split("BLOCK)", 1)[0]
        prolongate = text.split("PROLONGATE)", 1)[1].split("UPDATE)", 1)[0]
        self.assertIn('_build_update_payload', activate)
        self.assertIn('_build_update_payload', prolongate)
        self.assertNotIn('_build_create_payload', activate)
        self.assertNotIn('_build_create_payload', prolongate)

    def test_lifecycle_update_does_not_migrate_existing_zero(self) -> None:
        """ACTIVATE/PROLONGATE omit hwid when SHM is absent/null.

        An existing Remnawave ``hwidDeviceLimit=0`` is therefore left
        unchanged by the lifecycle event. Reset to panel default is
        reconciliation-only.
        """
        for hwid, omit in ((None, True), ("null", False), ("", False)):
            with self.subTest(hwid=hwid, omit=omit):
                code, out, err = _run_payload(
                    "update", hwid=hwid, omit_hwid_placeholder=omit
                )
                self.assertEqual(code, 0, err)
                payload = _parse_json(out)
                self.assertNotIn("hwidDeviceLimit", payload)
                self.assertNotIn('"hwidDeviceLimit": null', out)
                self.assertNotIn('"hwidDeviceLimit": 0', out)

        code, out, err = _run_payload("update", hwid="0")
        self.assertEqual(code, 0, err)
        payload = _parse_json(out)
        self.assertEqual(payload["hwidDeviceLimit"], 0)

    def test_create_does_not_use_zero_as_absent_fallback(self) -> None:
        code, out, err = _run_payload("create", hwid="")
        self.assertEqual(code, 0, err)
        payload = _parse_json(out)
        self.assertNotIn("hwidDeviceLimit", payload)
        self.assertNotIn('"hwidDeviceLimit": 0', out)


if __name__ == "__main__":
    unittest.main()
