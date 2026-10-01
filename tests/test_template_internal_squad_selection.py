#!/usr/bin/env python3
"""CREATE Internal Squad selection: fixed, least-members pool, server fallback.

Pool semantics (only when ``internal_squad_name`` is absent or the literal ``null``):

- rendered empty string or JSON ``null`` — pool is not configured; server fallback
- any other value is a configured pool and must be a non-empty JSON array of
  unique non-empty strings without leading or trailing whitespace
- not an array, empty array, empty string, whitespace-padded name, non-string,
  duplicate name — CREATE error (the pool is not ignored and not partially used)
- a configured name missing from ``GET /api/internal-squads`` — CREATE error
- ``membersCount`` must be a JSON number

A set ``internal_squad_name`` is fixed mode: the pool is not read and not
validated. Selection runs only on CREATE, from a single internal-squads snapshot.
"""

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

POOL_PLACEHOLDER = "{{ toJson(us.service.settings.remnawave.internal_squad_pool_names) }}"

UUID_DEFAULT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
UUID_S01 = "11111111-1111-1111-1111-111111111101"
UUID_S02 = "22222222-2222-2222-2222-222222222202"
UUID_S03 = "33333333-3333-3333-3333-333333333303"
UUID_S04 = "44444444-4444-4444-4444-444444444404"
UUID_S05 = "55555555-5555-5555-5555-555555555505"
UUID_TINY = "99999999-9999-9999-9999-999999999999"
UUID_ANTI = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
VFF_UUID = "cccccccc-cccc-cccc-cccc-cccccccccccc"

EXAMPLE_POOL = [
    "Default-Squad",
    "VFF-STD-S01",
    "VFF-STD-S02",
    "VFF-STD-S03",
    "VFF-STD-S04",
    "VFF-STD-S05",
]
EXAMPLE_LOAD = (
    "Default-Squad=222, VFF-STD-S01=210, VFF-STD-S02=203, "
    "VFF-STD-S03=290, VFF-STD-S04=289, VFF-STD-S05=242"
)

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
    cat "${SQUADS_FILE}"
    return 0
  fi
  if [[ "${p}" == /api/external-squads ]]; then
    printf '%s' '{"response":{"externalSquads":[{"name":"VPN-for-Friends","uuid":"cccccccc-cccc-cccc-cccc-cccccccccccc"}]}}'
    return 0
  fi
  echo "unexpected GET ${p}" >&2
  return 1
}

_http_post() {
  local p="$1"
  shift
  echo "POST ${p}" >> "${CALL_LOG}"
  local body=""
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--data" ]]; then
      body="$2"
      shift 2
      continue
    fi
    shift
  done
  if [[ "${p}" == "/api/users" ]]; then
    printf '%s' "${body}" > "${CREATE_BODY}"
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


def _squad(name: str, members: Any, uuid: str) -> Dict[str, Any]:
    return {
        "uuid": uuid,
        "name": name,
        "info": {"membersCount": members, "inboundsCount": 4},
    }


def _example_squads() -> List[Dict[str, Any]]:
    """Panel order is not pool order and includes a smaller squad outside the pool."""
    return [
        _squad("Tiny-Squad", 1, UUID_TINY),
        _squad("VFF-STD-S05", 242, UUID_S05),
        _squad("VFF-STD-S02", 203, UUID_S02),
        _squad("Default-Squad", 222, UUID_DEFAULT),
        _squad("VFF-STD-S04", 289, UUID_S04),
        _squad("VFF-STD-S01", 210, UUID_S01),
        _squad("VFF-STD-S03", 290, UUID_S03),
        _squad("AntiBlock-Squad", 999, UUID_ANTI),
    ]


def _render(
    *,
    event: str,
    internal_squad_name: str = "",
    pool_json: str = "",
    default_squad_name: str = "Default-Squad",
    external_squad_name: str = "",
    traffic_limit_bytes: str = "",
    traffic_limit_strategy: str = "",
) -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    replacements = {
        "{{ event_name }}": event,
        "{{ user.gen_session.id }}": "test-session",
        "{{ config.api.url }}": "https://shm.test",
        "{{ server.settings.remnawave.api }}": "https://panel.test",
        "{{ server.settings.remnawave.token }}": "test-token",
        "{{ server.settings.remnawave.default_internal_squad_name }}": default_squad_name,
        "{{ us.service.settings.remnawave.internal_squad_name }}": internal_squad_name,
        POOL_PLACEHOLDER: pool_json,
        "{{ us.service.settings.remnawave.external_squad_name }}": external_squad_name,
        "{{ us.service.settings.remnawave.traffic_limit_bytes }}": traffic_limit_bytes,
        "{{ us.service.settings.remnawave.traffic_limit_strategy }}": traffic_limit_strategy,
        "{{ us.service.settings.remnawave.hwid_device_limit }}": "",
        "{{ server.settings.remnawave.shm_tz }}": "UTC",
        "{{ server.settings.remnawave.expire_safety_minutes }}": "0",
        "{{ us.id }}": "42",
        "{{ server.settings.remnawave.sanitize_username }}": "false",
        "{{ us.expire }}": "2026-12-31 23:59:59",
        "{{ user.login }}": "alice",
        "{{ user.full_name }}": "Alice Example",
        "{{ user.settings.telegram.login }}": "alice_tg",
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
    squads: Optional[List[Dict[str, Any]]] = None,
    internal_squad_name: str = "",
    pool_json: str = "",
    default_squad_name: str = "Default-Squad",
    external_squad_name: str = "",
    traffic_limit_bytes: str = "",
    traffic_limit_strategy: str = "",
) -> Dict[str, Any]:
    rendered = _render(
        event=event,
        internal_squad_name=internal_squad_name,
        pool_json=pool_json,
        default_squad_name=default_squad_name,
        external_squad_name=external_squad_name,
        traffic_limit_bytes=traffic_limit_bytes,
        traffic_limit_strategy=traffic_limit_strategy,
    )
    if squads is None:
        squads = _example_squads()
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "rendered.sh")
        call_log = os.path.join(tmp, "calls.log")
        reset_log = os.path.join(tmp, "resets.log")
        create_body = os.path.join(tmp, "create.json")
        patch_body = os.path.join(tmp, "patch.json")
        squads_file = os.path.join(tmp, "squads.json")
        Path(script).write_text(rendered, encoding="utf-8")
        Path(call_log).write_text("", encoding="utf-8")
        Path(reset_log).write_text("", encoding="utf-8")
        Path(squads_file).write_text(
            json.dumps({"response": {"internalSquads": squads}}),
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["CALL_LOG"] = call_log
        env["RESET_LOG"] = reset_log
        env["CREATE_BODY"] = create_body
        env["PATCH_BODY"] = patch_body
        env["SQUADS_FILE"] = squads_file
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
        created: Optional[Dict[str, Any]] = None
        if Path(create_body).exists() and Path(create_body).stat().st_size:
            created = json.loads(Path(create_body).read_text(encoding="utf-8"))
        patch: Optional[Dict[str, Any]] = None
        if Path(patch_body).exists() and Path(patch_body).stat().st_size:
            patch = json.loads(Path(patch_body).read_text(encoding="utf-8"))
        return {
            "code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "calls": calls,
            "resets": resets,
            "created": created,
            "patch": patch,
        }


def _internal_gets(result: Dict[str, Any]) -> List[str]:
    return [line for line in result["calls"] if line == "GET /api/internal-squads"]


def _posts(result: Dict[str, Any]) -> List[str]:
    return [line for line in result["calls"] if line.startswith("POST ")]


class InternalSquadSelectionTests(unittest.TestCase):
    def test_fixed_name_wins_over_pool(self) -> None:
        result = _run(
            "CREATE",
            internal_squad_name="AntiBlock-Squad",
            pool_json=json.dumps(EXAMPLE_POOL),
        )
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(_internal_gets(result), ["GET /api/internal-squads"])
        self.assertIn("Internal squad selection mode: fixed", result["stdout"])
        self.assertNotIn("least_members", result["stdout"])
        self.assertNotIn("Internal squad pool load:", result["stdout"])
        self.assertIn(
            "Selected internal squad: AntiBlock-Squad (members=999)",
            result["stdout"],
        )
        self.assertEqual(result["created"]["activeInternalSquads"], [UUID_ANTI])
        self.assertNotIn("test-token", result["stdout"])
        self.assertNotIn("test-token", result["stderr"])

    def test_apostrophe_in_pool_name_is_selected(self) -> None:
        chosen = "VFF-STD-O'Brien"
        chosen_uuid = "dddddddd-dddd-dddd-dddd-dddddddddddd"
        others = [
            ("sq$uad", "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeee1"),
            ('sq"uad', "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeee2"),
            ("sq`uad", "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeee3"),
            ("sq\\uad", "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeee4"),
        ]
        squads = [_squad(chosen, 4, chosen_uuid)]
        squads.extend(_squad(name, 20 + index, uuid) for index, (name, uuid) in enumerate(others))
        result = _run(
            "CREATE",
            squads=squads,
            pool_json=json.dumps([chosen, *[name for name, _uuid in others]]),
        )
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertNotIn("syntax error", result["stderr"])
        self.assertIn("Internal squad selection mode: least_members", result["stdout"])
        self.assertIn(
            "Selected internal squad: VFF-STD-O'Brien (members=4)",
            result["stdout"],
        )
        self.assertEqual(result["created"]["activeInternalSquads"], [chosen_uuid])
        self.assertEqual(_internal_gets(result), ["GET /api/internal-squads"])

    def test_apostrophe_in_pool_does_not_break_fixed_mode(self) -> None:
        result = _run(
            "CREATE",
            internal_squad_name="AntiBlock-Squad",
            pool_json=json.dumps(
                ["VFF-STD-O'Brien", "sq$uad", 'sq"uad', "sq`uad", "sq\\uad"]
            ),
        )
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertNotIn("syntax error", result["stderr"])
        self.assertIn("Internal squad selection mode: fixed", result["stdout"])
        self.assertNotIn("least_members", result["stdout"])
        self.assertEqual(result["created"]["activeInternalSquads"], [UUID_ANTI])
        self.assertEqual(_internal_gets(result), ["GET /api/internal-squads"])

    def test_fixed_name_ignores_invalid_pool(self) -> None:
        result = _run(
            "CREATE",
            internal_squad_name="AntiBlock-Squad",
            pool_json="[",
        )
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(_internal_gets(result), ["GET /api/internal-squads"])
        self.assertIn("Internal squad selection mode: fixed", result["stdout"])
        self.assertEqual(result["created"]["activeInternalSquads"], [UUID_ANTI])

    def test_pool_picks_minimum_members_count(self) -> None:
        result = _run("CREATE", pool_json=json.dumps(EXAMPLE_POOL))
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(_internal_gets(result), ["GET /api/internal-squads"])
        self.assertIn("Internal squad selection mode: least_members", result["stdout"])
        self.assertIn(f"Internal squad pool load: {EXAMPLE_LOAD}", result["stdout"])
        self.assertIn(
            "Selected internal squad: VFF-STD-S02 (members=203)",
            result["stdout"],
        )
        self.assertEqual(result["created"]["activeInternalSquads"], [UUID_S02])
        self.assertNotIn("Tiny-Squad", result["stdout"])
        self.assertNotIn("test-token", result["stdout"])

    def test_pool_ignores_squads_outside_whitelist(self) -> None:
        result = _run(
            "CREATE",
            pool_json=json.dumps(["VFF-STD-S01", "VFF-STD-S05"]),
        )
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertIn(
            "Selected internal squad: VFF-STD-S01 (members=210)",
            result["stdout"],
        )
        self.assertEqual(result["created"]["activeInternalSquads"], [UUID_S01])
        self.assertNotIn("Tiny-Squad", result["stdout"])
        self.assertEqual(_internal_gets(result), ["GET /api/internal-squads"])

    def test_tie_breaks_by_name_ascending(self) -> None:
        squads = [
            _squad("VFF-STD-S02", 203, UUID_S02),
            _squad("VFF-STD-S01", 203, UUID_S01),
            _squad("VFF-STD-S03", 290, UUID_S03),
        ]
        result = _run(
            "CREATE",
            squads=squads,
            pool_json=json.dumps(["VFF-STD-S02", "VFF-STD-S01", "VFF-STD-S03"]),
        )
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertIn(
            "Selected internal squad: VFF-STD-S01 (members=203)",
            result["stdout"],
        )
        self.assertEqual(result["created"]["activeInternalSquads"], [UUID_S01])
        self.assertIn(
            "Internal squad pool load: VFF-STD-S02=203, VFF-STD-S01=203, VFF-STD-S03=290",
            result["stdout"],
        )

    def test_missing_configured_squad_fails_create(self) -> None:
        result = _run(
            "CREATE",
            pool_json=json.dumps(["VFF-STD-S02", "Missing-Squad", "Also-Missing"]),
        )
        self.assertNotEqual(result["code"], 0)
        self.assertIn("Configured internal squad not found on panel", result["stderr"])
        self.assertIn("Missing-Squad", result["stderr"])
        self.assertIn("Also-Missing", result["stderr"])
        self.assertNotIn("Selected internal squad:", result["stdout"])
        self.assertIsNone(result["created"])
        self.assertNotIn("POST /api/users", result["calls"])
        self.assertEqual(_internal_gets(result), ["GET /api/internal-squads"])

    def test_invalid_pool_shapes_fail_create(self) -> None:
        cases = {
            "object": ('{"name":"VFF-STD-S02"}', "must be a JSON array"),
            "string": ('"VFF-STD-S02"', "must be a JSON array"),
            "number": ("1", "must be a JSON array"),
            "bool": ("false", "must be a JSON array"),
            "empty-array": ("[]", "empty array"),
            "empty-string": ('["VFF-STD-S01",""]', "empty string"),
            "whitespace": ('[" VFF-STD-S01"]', "whitespace"),
            "non-string": ('["VFF-STD-S01",1]', "non-empty string"),
            "null-item": ("[null]", "non-empty string"),
            "duplicate": (
                '["VFF-STD-S01","VFF-STD-S02","VFF-STD-S01"]',
                "duplicate",
            ),
            "not-json": ("not-json", "not valid JSON"),
        }
        for label, (pool_json, needle) in cases.items():
            with self.subTest(label=label):
                result = _run("CREATE", pool_json=pool_json)
                self.assertNotEqual(result["code"], 0, result["stdout"])
                self.assertIn(needle, result["stderr"])
                self.assertIn("internal_squad_pool_names", result["stderr"])
                self.assertIsNone(result["created"])
                self.assertNotIn("POST /api/users", result["calls"])
                self.assertNotIn("Selected internal squad:", result["stdout"])

    def test_absent_pool_uses_server_fallback(self) -> None:
        squads = [
            _squad("Tiny-Squad", 1, UUID_TINY),
            _squad("Default-Squad", 500, UUID_DEFAULT),
        ]
        for label, pool_json in (("empty", ""), ("json-null", "null"), ("blank", "  ")):
            with self.subTest(label=label):
                result = _run(
                    "CREATE",
                    squads=squads,
                    pool_json=pool_json,
                )
                self.assertEqual(result["code"], 0, result["stderr"])
                self.assertIn("Internal squad selection mode: fallback", result["stdout"])
                self.assertIn(
                    "Selected internal squad: Default-Squad (members=500)",
                    result["stdout"],
                )
                self.assertNotIn("least_members", result["stdout"])
                self.assertEqual(
                    result["created"]["activeInternalSquads"], [UUID_DEFAULT]
                )
                self.assertEqual(_internal_gets(result), ["GET /api/internal-squads"])

    def test_literal_null_fixed_name_does_not_override_pool(self) -> None:
        result = _run(
            "CREATE",
            internal_squad_name="null",
            pool_json=json.dumps(EXAMPLE_POOL),
        )
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertIn("Internal squad selection mode: least_members", result["stdout"])
        self.assertEqual(result["created"]["activeInternalSquads"], [UUID_S02])

    def test_members_count_must_be_a_number(self) -> None:
        squads = [_squad("VFF-STD-S02", "203", UUID_S02)]
        result = _run(
            "CREATE",
            squads=squads,
            pool_json=json.dumps(["VFF-STD-S02"]),
        )
        self.assertNotEqual(result["code"], 0)
        self.assertIn("membersCount is not a number", result["stderr"])
        self.assertIn("VFF-STD-S02", result["stderr"])
        self.assertIsNone(result["created"])

    def test_no_fixed_pool_or_fallback_fails(self) -> None:
        result = _run(
            "CREATE",
            squads=[],
            default_squad_name="",
            pool_json="",
        )
        self.assertNotEqual(result["code"], 0)
        self.assertIn("No internal squad configured", result["stderr"])
        self.assertIsNone(result["created"])

    def test_external_squad_does_not_add_a_second_internal_get(self) -> None:
        result = _run(
            "CREATE",
            pool_json=json.dumps(EXAMPLE_POOL),
            external_squad_name="VPN-for-Friends",
        )
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(_internal_gets(result), ["GET /api/internal-squads"])
        self.assertEqual(
            [line for line in result["calls"] if line == "GET /api/external-squads"],
            ["GET /api/external-squads"],
        )
        self.assertEqual(result["created"]["activeInternalSquads"], [UUID_S02])
        self.assertEqual(result["created"]["externalSquadUuid"], VFF_UUID)

    def test_non_create_events_do_not_select_internal_squad(self) -> None:
        for event in ("ACTIVATE", "BLOCK", "REMOVE", "UPDATE"):
            with self.subTest(event=event):
                result = _run(
                    event,
                    pool_json=json.dumps(EXAMPLE_POOL),
                    traffic_limit_strategy="MONTH",
                )
                self.assertEqual(result["code"], 0, result["stderr"])
                self.assertEqual(_internal_gets(result), [])
                self.assertNotIn("Internal squad selection mode:", result["stdout"])
                self.assertNotIn("least_members", result["stdout"])
                self.assertEqual(result["resets"], [])
                if result["patch"] is not None:
                    self.assertNotIn("activeInternalSquads", result["patch"])

    def test_activate_and_prolongate_keep_strategy_reset_without_squad_selection(self) -> None:
        activate = _run(
            "ACTIVATE",
            pool_json=json.dumps(EXAMPLE_POOL),
            traffic_limit_bytes="107374182400",
            traffic_limit_strategy="MONTH",
        )
        self.assertEqual(activate["code"], 0, activate["stderr"])
        self.assertEqual(_internal_gets(activate), [])
        self.assertEqual(activate["resets"], [])
        self.assertEqual(activate["patch"]["trafficLimitStrategy"], "MONTH")
        self.assertNotIn("activeInternalSquads", activate["patch"])

        skipped = _run(
            "PROLONGATE",
            pool_json=json.dumps(EXAMPLE_POOL),
            traffic_limit_bytes="322122547200",
            traffic_limit_strategy="MONTH",
        )
        self.assertEqual(skipped["code"], 0, skipped["stderr"])
        self.assertEqual(_internal_gets(skipped), [])
        self.assertEqual(skipped["resets"], [])
        self.assertIn(
            "Traffic reset on PROLONGATE: skipped (strategy=MONTH; managed by Remnawave)",
            skipped["stdout"],
        )
        self.assertEqual(skipped["patch"]["trafficLimitStrategy"], "MONTH")
        self.assertEqual(skipped["patch"]["trafficLimitBytes"], 322122547200)
        self.assertNotIn("activeInternalSquads", skipped["patch"])
        self.assertNotIn("Internal squad selection mode:", skipped["stdout"])

        reset = _run(
            "PROLONGATE",
            pool_json=json.dumps(EXAMPLE_POOL),
            traffic_limit_bytes="322122547200",
            traffic_limit_strategy="NO_RESET",
        )
        self.assertEqual(reset["code"], 0, reset["stderr"])
        self.assertEqual(_internal_gets(reset), [])
        self.assertEqual(len(reset["resets"]), 1)
        self.assertIn(
            "Traffic reset on PROLONGATE: enabled (strategy=NO_RESET)",
            reset["stdout"],
        )
        self.assertEqual(reset["patch"]["trafficLimitStrategy"], "NO_RESET")
        self.assertNotIn("activeInternalSquads", reset["patch"])


class InternalSquadSelectionSourceTests(unittest.TestCase):
    def test_only_create_calls_selector(self) -> None:
        text = TEMPLATE.read_text(encoding="utf-8")
        mentions = [
            line
            for line in text.splitlines()
            if "_select_internal_squad_for_create" in line
            and not line.strip().startswith("#")
        ]
        definitions = [
            line for line in mentions if "_select_internal_squad_for_create()" in line
        ]
        calls = [line for line in mentions if line not in definitions]
        self.assertEqual(len(definitions), 1)
        self.assertEqual(calls, ["    _select_internal_squad_for_create"])
        create = text.split("CREATE)", 1)[1].split("ACTIVATE)", 1)[0]
        self.assertIn("_select_internal_squad_for_create", create)
        for event, end in (
            ("ACTIVATE)", "BLOCK)"),
            ("BLOCK)", "REMOVE)"),
            ("REMOVE)", "PROLONGATE)"),
            ("PROLONGATE)", "UPDATE)"),
            ("UPDATE)", "*)"),
        ):
            branch = text.split(event, 1)[1].split(end, 1)[0]
            self.assertNotIn("_select_internal_squad_for_create", branch)

    def test_create_payload_uses_selected_uuid_and_keeps_name_resolver(self) -> None:
        text = TEMPLATE.read_text(encoding="utf-8")
        payload = text.split("_build_create_payload()", 1)[1].split(
            "_build_update_payload()", 1
        )[0]
        self.assertIn("SELECTED_INTERNAL_SQUAD_UUID", payload)
        self.assertNotIn("_resolve_internal_squad_uuid_by_name", payload)
        self.assertNotIn("/api/internal-squads", payload)
        self.assertIn("_resolve_internal_squad_uuid_by_name()", text)
        self.assertIn('toJson(us.service.settings.remnawave.internal_squad_pool_names)', text)

    def test_prolongate_reset_helper_is_unchanged_in_place(self) -> None:
        text = TEMPLATE.read_text(encoding="utf-8")
        prolongate = text.split("PROLONGATE)", 1)[1].split("UPDATE)", 1)[0]
        self.assertIn("_reset_traffic_on_prolongate_if_needed", prolongate)
        self.assertNotIn("_reset_user_traffic", prolongate)
        self.assertNotIn("_select_internal_squad_for_create", prolongate)
        self.assertIn('_should_reset_traffic_on_prolongate', text)
        reset_fn = text.split("_should_reset_traffic_on_prolongate()", 1)[1].split(
            "_reset_traffic_on_prolongate_if_needed()", 1
        )[0]
        self.assertIn('[[ "${strategy}" == "NO_RESET" ]]', reset_fn)


if __name__ == "__main__":
    unittest.main()
