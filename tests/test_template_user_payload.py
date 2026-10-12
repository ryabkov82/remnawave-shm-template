#!/usr/bin/env python3
"""CREATE description must keep untrusted user text as data.

SHM substitutes ``{{ ... }}`` before bash runs. Raw ``user.login``,
``user.full_name`` and ``user.settings.telegram.login`` inside the script
become shell source. A full_name such as ``(´_ゝ`)`` (ASCII backtick) makes
bash abort with ``bad substitution``; ``$(...)``, ``${...}``, quotes,
backslashes and newlines corrupt the JSON or run commands.

These tests render the template the way SHM does and execute the CREATE
payload builder. They require a valid payload whose ``description`` is the
original text, and they require that user text is not executed.
"""

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
EXPIRE_ISO = "2026-12-31T23:59:59Z"

# Production incident us_3640: full_name ends with ASCII backtick U+0060.
PRODUCTION_FULL_NAME = "(´_ゝ`)"
COMMAND_SENTINEL = Path("/tmp/SHOULD_NOT_EXIST")
COMMAND_TEXT = "$(touch /tmp/SHOULD_NOT_EXIST)"


def _description(login: str, full_name: str, telegram: str) -> str:
    return f"SHM: login={login}, name={full_name}, url=https://t.me/{telegram}"


def _render(
    *,
    login: str,
    full_name: str,
    telegram: str,
    hwid: Optional[str] = None,
    traffic_limit_bytes: str = "",
    traffic_limit_strategy: str = "",
    external_squad_name: str = "",
    expire: str = "2026-12-31 23:59:59",
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
        "{{ toJson(us.service.settings.remnawave.internal_squad_pool_names) }}": "",
        "{{ us.service.settings.remnawave.external_squad_name }}": external_squad_name,
        "{{ us.service.settings.remnawave.traffic_limit_bytes }}": traffic_limit_bytes,
        "{{ us.service.settings.remnawave.traffic_limit_strategy }}": traffic_limit_strategy,
        "{{ us.service.settings.remnawave.hwid_device_limit }}": "" if hwid is None else hwid,
        "{{ server.settings.remnawave.shm_tz }}": "UTC",
        "{{ server.settings.remnawave.expire_safety_minutes }}": "0",
        "{{ us.id }}": "42",
        "{{ server.settings.remnawave.sanitize_username }}": "false",
        "{{ us.expire }}": expire,
    }
    # SHM replaces {{ toJson(user.*) }} with a JSON string. The unfixed template
    # still interpolates the raw {{ user.* }} value into shell source.
    user_fields = {
        "user.login": login,
        "user.full_name": full_name,
        "user.settings.telegram.login": telegram,
    }
    for field, value in user_fields.items():
        encoded = "{{ toJson(" + field + ") }}"
        raw = "{{ " + field + " }}"
        if encoded in text:
            replacements[encoded] = json.dumps(value, ensure_ascii=False)
        elif raw in text:
            replacements[raw] = value
        else:
            raise AssertionError(f"user field is not passed into the template: {field}")

    for needle, value in replacements.items():
        if needle not in text:
            raise AssertionError(f"placeholder missing from template: {needle}")
        text = text.replace(needle, value)

    leftover = re.findall(r"\{\{[^}]+\}\}", text)
    if leftover:
        raise AssertionError(f"unreplaced placeholders: {leftover}")

    cut = text.find('log "Remnawave Template')
    if cut < 0:
        raise AssertionError("could not find template case/start marker")
    return text[:cut]


def _run_create(**kwargs: Any) -> Tuple[int, str, str]:
    rendered = _render(**kwargs)
    driver = """
SELECTED_INTERNAL_SQUAD_UUID='%s'
_resolve_internal_squad_uuid_by_name() { echo '%s'; }
_resolve_external_squad_uuid_by_name() { echo '%s'; }
_build_create_payload
""" % (
        INTERNAL_UUID,
        INTERNAL_UUID,
        VFF_UUID,
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "rendered.sh")
        Path(path).write_text(rendered + driver, encoding="utf-8")
        proc = subprocess.run(
            ["bash", path],
            check=False,
            capture_output=True,
            text=True,
        )
        return proc.returncode, proc.stdout, proc.stderr


def _expected_payload(
    login: str,
    full_name: str,
    telegram: str,
    *,
    hwid: Optional[int] = None,
    traffic_limit_bytes: int = 0,
    traffic_limit_strategy: str = "NO_RESET",
    external_squad_uuid: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "username": "us_42",
        "status": "ACTIVE",
        "trafficLimitBytes": traffic_limit_bytes,
        "trafficLimitStrategy": traffic_limit_strategy,
        "expireAt": EXPIRE_ISO,
        "description": _description(login, full_name, telegram),
        "tag": None,
        "telegramId": None,
        "email": None,
        "activeInternalSquads": [INTERNAL_UUID],
        "externalSquadUuid": external_squad_uuid,
    }
    if hwid is not None:
        payload["hwidDeviceLimit"] = hwid
    return payload


class TemplateUserPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        if COMMAND_SENTINEL.exists():
            COMMAND_SENTINEL.unlink()

    def tearDown(self) -> None:
        if COMMAND_SENTINEL.exists():
            COMMAND_SENTINEL.unlink()

    def _assert_create(
        self,
        login: str,
        full_name: str,
        telegram: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        code, out, err = _run_create(
            login=login, full_name=full_name, telegram=telegram, **kwargs
        )
        self.assertEqual(code, 0, err or out)
        try:
            payload = json.loads(out)
        except json.JSONDecodeError as exc:
            self.fail(f"CREATE payload is not JSON: {exc}\nstdout={out!r}\nstderr={err!r}")
        self.assertEqual(payload["description"], _description(login, full_name, telegram))
        return payload

    def test_create_description_preserves_user_text(self) -> None:
        cases = (
            ("plain", "alice", "Alice Example", "alice_tg"),
            ("production_backtick_name", "deadlinebun", PRODUCTION_FULL_NAME, "deadlinebun"),
            ("double_quote", 'say"hi', 'Alice "The" Example', 'tg"user'),
            ("backtick", "`", "`id`", "`"),
            ("dollar_command", "$(id)", "$(id)", "$(id)"),
            ("dollar_brace", "${HOME}", "${HOME}", "${HOME}"),
            ("backslash", "a\\b", "C:\\Users\\alice", "tg\\x"),
            ("newline", "a\nb", "line1\nline2", "tg\nlogin"),
            ("unicode", "иван", "Иван Петров 😀", "юзер"),
        )
        for name, login, full_name, telegram in cases:
            with self.subTest(name=name):
                payload = self._assert_create(login, full_name, telegram)
                self.assertEqual(payload, _expected_payload(login, full_name, telegram))
                self.assertNotIn("uid=", payload["description"])

    def test_create_does_not_execute_command_substitution(self) -> None:
        self.assertFalse(COMMAND_SENTINEL.exists())
        payload = self._assert_create("alice", COMMAND_TEXT, "alice_tg")
        self.assertEqual(
            payload,
            _expected_payload("alice", COMMAND_TEXT, "alice_tg"),
        )
        self.assertIn(COMMAND_TEXT, payload["description"])
        self.assertFalse(
            COMMAND_SENTINEL.exists(),
            "user text was executed as a shell command",
        )

    def test_create_other_fields_unchanged_with_hostile_user_text(self) -> None:
        login = "$(id)"
        telegram = 'a"b\\c'
        payload = self._assert_create(
            login,
            PRODUCTION_FULL_NAME,
            telegram,
            hwid="3",
            traffic_limit_bytes="53687091200",
            traffic_limit_strategy="MONTH",
            external_squad_name="VPN-for-Friends",
        )
        self.assertEqual(
            payload,
            _expected_payload(
                login,
                PRODUCTION_FULL_NAME,
                telegram,
                hwid=3,
                traffic_limit_bytes=53687091200,
                traffic_limit_strategy="MONTH",
                external_squad_uuid=VFF_UUID,
            ),
        )
        self.assertIsInstance(payload["trafficLimitBytes"], int)
        self.assertIsInstance(payload["hwidDeviceLimit"], int)
        self.assertFalse(COMMAND_SENTINEL.exists())

    def test_user_fields_use_tojson_quoted_heredoc(self) -> None:
        text = TEMPLATE.read_text(encoding="utf-8")
        for field, delimiter in (
            ("user.login", "__SHM_USER_LOGIN_JSON__"),
            ("user.full_name", "__SHM_USER_FULL_NAME_JSON__"),
            ("user.settings.telegram.login", "__SHM_USER_TELEGRAM_LOGIN_JSON__"),
        ):
            raw = "{{ " + field + " }}"
            encoded = "{{ toJson(" + field + ") }}"
            self.assertNotIn(raw, text)
            self.assertIn(encoded, text)
            self.assertIn(f"<<'{delimiter}'", text)
            self.assertIn("__SHM_JSON__" + encoded, text)


if __name__ == "__main__":
    unittest.main()
