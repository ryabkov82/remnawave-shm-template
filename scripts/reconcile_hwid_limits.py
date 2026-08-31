#!/usr/bin/env python3
"""Reconcile Remnawave hwidDeviceLimit from SHM service settings.

Target is taken from the catalog service setting
``us.service.settings.remnawave.hwid_device_limit`` (never by blindly
resetting every Remnawave ``0``):

* absent / null -> target JSON null (inherit panel default)
* 0             -> target 0 (explicitly disable HWID limit)
* N > 0         -> target N

Dry-run is the default. Apply requires ``--apply``,
``--confirm RECONCILE_HWID_LIMITS``, and at least one ``--apply-username``.
The allow-list is an extra constraint: a listed user is PATCHed only if the
plan still classifies them as a mutation and live current/target match.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import signal
import sys
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from reconcile_external_squads import (
    FatalError,
    HttpClient,
    Interrupted,
    atomic_write_json,
    atomic_write_text,
    ensure_output_dir,
    iter_shm_user_services,
    log,
    redact_secrets,
    shm_authenticate,
    username_for_user_service,
)


CONFIRM_PHRASE = "RECONCILE_HWID_LIMITS"
HTTP_TIMEOUT_SEC = 30
VERIFY_RETRY_ATTEMPTS = 5
VERIFY_RETRY_DELAY_SEC = 1.0

CLASS_ALREADY_CORRECT = "already_correct"
CLASS_RESET_TO_PANEL_DEFAULT = "needs_reset_to_panel_default"
CLASS_SET_EXPLICIT = "needs_set_explicit_limit"
CLASS_DISABLE = "needs_disable_limit"
CLASS_MISSING = "missing_in_remnawave"
CLASS_INVALID = "invalid_shm_setting"
CLASS_ERROR = "error"

APPLY_CLASSES = frozenset(
    {CLASS_RESET_TO_PANEL_DEFAULT, CLASS_SET_EXPLICIT, CLASS_DISABLE}
)
UNCLASSIFIABLE = frozenset({CLASS_INVALID, CLASS_ERROR})

# Sentinel: SHM setting was not parsed. Distinct from valid target None (panel default).
TARGET_UNRESOLVED = object()

NON_NEGATIVE_INT_RE = re.compile(r"^[0-9]+$")


class HwidSettingError(ValueError):
    """SHM hwid_device_limit cannot be parsed."""


@dataclass
class PlanRow:
    user_service_id: Any
    username: str
    shm_category: Optional[str]
    service_id: Any
    shm_hwid_device_limit_raw: Any
    target_hwid_device_limit: Any
    current_hwid_device_limit: Any
    remnawave_user_id: Any
    classification: str
    error_message: Optional[str] = None

    def to_report_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "user_service_id": self.user_service_id,
            "username": self.username,
            "shm_category": self.shm_category,
            "service_id": self.service_id,
            "shm_hwid_device_limit_raw": self.shm_hwid_device_limit_raw,
            "current_hwid_device_limit": self.current_hwid_device_limit,
            "remnawave_user_id": self.remnawave_user_id,
            "classification": self.classification,
            "error_message": self.error_message,
        }
        if self.target_hwid_device_limit is TARGET_UNRESOLVED:
            # Omit target_hwid_device_limit: JSON null would mean panel default.
            data["target_resolved"] = False
        else:
            data["target_resolved"] = True
            data["target_hwid_device_limit"] = self.target_hwid_device_limit
        return data


@dataclass
class ReconcileConfig:
    shm_base_url: str
    shm_login: str
    shm_password: str
    remnawave_panel_url: str
    remnawave_token: str
    output: str
    categories: Tuple[str, ...] = ()
    page_size: int = 250
    request_delay_ms: int = 50
    apply: bool = False
    confirm: Optional[str] = None
    apply_usernames: Tuple[str, ...] = ()
    http_timeout: float = HTTP_TIMEOUT_SEC
    verify_retry_attempts: int = VERIFY_RETRY_ATTEMPTS
    verify_retry_delay_sec: float = VERIFY_RETRY_DELAY_SEC


def parse_shm_hwid_setting(raw: Any) -> Optional[int]:
    """Return target HWID limit.

    ``None`` means inherit the Remnawave panel default (JSON null).
    ``0`` means disable the per-user limit. ``N > 0`` is an explicit limit.

    Raises:
        HwidSettingError: negative, float, bool, or any other invalid value.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise HwidSettingError(
            f"invalid hwid_device_limit: {raw!r} (must be null or integer >= 0)"
        )
    if isinstance(raw, int):
        if raw < 0:
            raise HwidSettingError(
                f"invalid hwid_device_limit: {raw!r} (must be null or integer >= 0)"
            )
        return raw
    if isinstance(raw, float):
        raise HwidSettingError(
            f"invalid hwid_device_limit: {raw!r} (must be null or integer >= 0)"
        )
    if isinstance(raw, str):
        value = raw.strip()
        if value == "" or value == "null":
            return None
        if NON_NEGATIVE_INT_RE.fullmatch(value):
            return int(value)
        raise HwidSettingError(
            f"invalid hwid_device_limit: {raw!r} (must be null or integer >= 0)"
        )
    raise HwidSettingError(
        f"invalid hwid_device_limit: {raw!r} (must be null or integer >= 0)"
    )


def _as_mapping(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def extract_remnawave_settings(service: Any) -> Optional[Dict[str, Any]]:
    """Return the ``remnawave`` object from a SHM catalog service, if present."""
    obj = _as_mapping(service)
    if obj is None:
        return None
    for settings_key in ("settings", "config"):
        settings = _as_mapping(obj.get(settings_key))
        if settings is None:
            continue
        remnawave = _as_mapping(settings.get("remnawave"))
        if remnawave is not None:
            return remnawave
    remnawave = _as_mapping(obj.get("remnawave"))
    if remnawave is not None:
        return remnawave
    return None


def extract_hwid_raw_from_service(service: Any) -> Any:
    remnawave = extract_remnawave_settings(service)
    if remnawave is None:
        return None
    if "hwid_device_limit" not in remnawave:
        return None
    return remnawave.get("hwid_device_limit")


def nested_service_from_user_service(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for key in ("services", "service"):
        value = item.get(key)
        if isinstance(value, dict):
            return value
    return None


def resolve_catalog_service(
    item: Dict[str, Any],
    catalog: Dict[Any, Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return (service_object, error).

    A missing ``remnawave`` / ``hwid_device_limit`` key is valid (target null).
    A missing catalog service is not — we refuse to guess.
    """
    service_id = item.get("service_id")
    if service_id is not None and service_id != "":
        found = catalog.get(service_id)
        if found is None:
            found = catalog.get(str(service_id))
        if found is not None:
            return found, None
        nested = nested_service_from_user_service(item)
        if nested is not None:
            return nested, None
        return None, f"SHM service_id {service_id!r} not found in service catalog"
    nested = nested_service_from_user_service(item)
    if nested is not None:
        return nested, None
    return None, "missing service_id and nested service object"


def normalize_current_hwid(value: Any) -> Any:
    """Normalize a Remnawave-stored hwidDeviceLimit. Preserve null vs 0."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"unexpected boolean hwidDeviceLimit: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and NON_NEGATIVE_INT_RE.fullmatch(value.strip()):
        return int(value.strip())
    raise ValueError(f"unexpected hwidDeviceLimit: {value!r}")


def classify_hwid(
    *,
    target: Optional[int],
    current: Any,
) -> str:
    if current == target:
        return CLASS_ALREADY_CORRECT
    if target is None:
        return CLASS_RESET_TO_PANEL_DEFAULT
    if target == 0:
        return CLASS_DISABLE
    return CLASS_SET_EXPLICIT


def build_hwid_patch_payload(user_id: int, target: Optional[int]) -> Dict[str, Any]:
    """PATCH body. ``target is None`` must serialize as JSON null, not 0."""
    return {"id": user_id, "hwidDeviceLimit": target}


def encode_hwid_patch_body(user_id: int, target: Optional[int]) -> bytes:
    return json.dumps(
        build_hwid_patch_payload(user_id, target),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def iter_shm_services(
    client: HttpClient,
    cfg: ReconcileConfig,
    session_id: str,
) -> Iterable[Dict[str, Any]]:
    offset = 0
    total_items: Optional[int] = None
    while True:
        if client._interrupted():  # noqa: SLF001
            raise Interrupted("interrupted by signal")
        qs = urllib.parse.urlencode({"limit": cfg.page_size, "offset": offset})
        url = cfg.shm_base_url.rstrip("/") + f"/shm/v1/admin/service?{qs}"
        status, payload, _ = client.request(
            "GET",
            url,
            headers={
                "Accept": "application/json",
                "session-id": session_id,
            },
        )
        if status >= 400:
            raise FatalError(f"SHM service list failed with HTTP {status}")
        if not isinstance(payload, dict):
            raise FatalError("SHM service response is not an object")
        data = payload.get("data")
        if data is None:
            raise FatalError("SHM service response missing 'data'")
        if not isinstance(data, list):
            raise FatalError("SHM service 'data' is not a list")
        if "items" in payload and payload["items"] is not None:
            try:
                total_items = int(payload["items"])
            except (TypeError, ValueError) as exc:
                raise FatalError("SHM service 'items' is not an integer") from exc
        if not data:
            break
        for item in data:
            if isinstance(item, dict):
                yield item
            else:
                raise FatalError("SHM service item is not an object")
        offset += len(data)
        if total_items is not None and offset >= total_items:
            break
        if len(data) < cfg.page_size:
            break


def load_service_catalog(
    client: HttpClient,
    cfg: ReconcileConfig,
    session_id: str,
) -> Dict[Any, Dict[str, Any]]:
    catalog: Dict[Any, Dict[str, Any]] = {}
    try:
        for item in iter_shm_services(client, cfg, session_id):
            service_id = item.get("service_id")
            if service_id is None or service_id == "":
                continue
            catalog[service_id] = item
            catalog[str(service_id)] = item
    except FatalError as exc:
        log(f"SHM service catalog unavailable ({exc}); using nested service objects")
        return {}
    return catalog


def parse_remnawave_user(
    payload: Any,
) -> Tuple[Optional[int], Optional[str], Any, bool]:
    """Return (numeric_id, uuid, hwidDeviceLimit, hwid_key_present)."""
    if not isinstance(payload, dict):
        return None, None, None, False
    response = payload.get("response", payload)
    if not isinstance(response, dict):
        return None, None, None, False
    user = response.get("user") if isinstance(response.get("user"), dict) else None
    sources: List[Dict[str, Any]] = []
    if user is not None:
        sources.append(user)
    sources.append(response)

    numeric_id: Optional[int] = None
    uuid: Optional[str] = None
    hwid: Any = None
    hwid_present = False

    for src in sources:
        if numeric_id is None:
            value = src.get("id")
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                numeric_id = value
            elif isinstance(value, str) and NON_NEGATIVE_INT_RE.fullmatch(value):
                parsed = int(value)
                if parsed > 0:
                    numeric_id = parsed
        if uuid is None:
            value = src.get("uuid")
            if isinstance(value, str) and value:
                uuid = value
        if not hwid_present and "hwidDeviceLimit" in src:
            hwid_present = True
            hwid = src.get("hwidDeviceLimit")

    return numeric_id, uuid, hwid, hwid_present


def fetch_remnawave_user(
    client: HttpClient,
    cfg: ReconcileConfig,
    username: str,
) -> Tuple[str, Optional[int], Any, Optional[str]]:
    """Return (status_kind, numeric_id, current_hwid, error_message).

    status_kind: ok | missing | error
    """
    encoded = urllib.parse.quote(username, safe="")
    url = cfg.remnawave_panel_url.rstrip("/") + f"/api/users/by-username/{encoded}"
    try:
        status, payload, _ = client.request(
            "GET",
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {cfg.remnawave_token}",
            },
        )
    except FatalError as exc:
        return "error", None, None, str(exc)
    if status == 404:
        return "missing", None, None, None
    if status >= 400:
        return "error", None, None, f"HTTP {status}"
    numeric_id, _uuid, hwid, _present = parse_remnawave_user(payload)
    if numeric_id is None:
        return "error", None, None, "numeric user id missing in response"
    try:
        current = normalize_current_hwid(hwid)
    except ValueError as exc:
        return "error", numeric_id, None, str(exc)
    return "ok", numeric_id, current, None


def verify_hwid(
    client: HttpClient,
    cfg: ReconcileConfig,
    *,
    username: str,
    expected_id: int,
    expected_hwid: Optional[int],
    patch_payload: Any,
) -> Optional[str]:
    """Return None on success, or an error message string."""
    got_id, _uuid, raw_hwid, has_hwid = parse_remnawave_user(patch_payload)
    if got_id == expected_id and has_hwid:
        try:
            if normalize_current_hwid(raw_hwid) == expected_hwid:
                return None
        except ValueError:
            pass

    last_err = "PATCH response hwidDeviceLimit not confirmed"
    attempts = max(1, int(cfg.verify_retry_attempts))
    for attempt in range(1, attempts + 1):
        if cfg.verify_retry_delay_sec > 0:
            time.sleep(cfg.verify_retry_delay_sec)
        kind, get_id, get_hwid, err = fetch_remnawave_user(client, cfg, username)
        if kind == "ok" and get_id == expected_id and get_hwid == expected_hwid:
            return None
        last_err = (
            f"GET verify attempt {attempt}/{attempts} failed: "
            f"kind={kind} id={get_id} hwid={get_hwid!r} error={err}"
        )
    return last_err


def classify_row(
    *,
    user_service_id: Any,
    category: Optional[str],
    service_id: Any,
    raw_hwid: Any,
    fetch_kind: str,
    remnawave_user_id: Optional[int],
    current_hwid: Any,
    error_message: Optional[str],
) -> PlanRow:
    username = (
        username_for_user_service(user_service_id)
        if user_service_id not in (None, "")
        else ""
    )
    try:
        target: Any = parse_shm_hwid_setting(raw_hwid)
        parse_error: Optional[str] = None
    except HwidSettingError as exc:
        target = TARGET_UNRESOLVED
        parse_error = str(exc)

    base = dict(
        user_service_id=user_service_id,
        username=username,
        shm_category=category,
        service_id=service_id,
        shm_hwid_device_limit_raw=raw_hwid,
        target_hwid_device_limit=target,
        current_hwid_device_limit=current_hwid,
        remnawave_user_id=remnawave_user_id,
    )

    if parse_error:
        return PlanRow(
            **base,
            classification=CLASS_INVALID,
            error_message=parse_error,
        )
    if fetch_kind == "missing":
        return PlanRow(**base, classification=CLASS_MISSING, error_message=None)
    if fetch_kind == "error":
        return PlanRow(
            **base,
            classification=CLASS_ERROR,
            error_message=error_message or "unexpected remnawave error",
        )
    return PlanRow(
        **base,
        classification=classify_hwid(target=target, current=current_hwid),
        error_message=None,
    )


def build_plan(
    client: HttpClient,
    cfg: ReconcileConfig,
    session_id: str,
    catalog: Dict[Any, Dict[str, Any]],
) -> List[PlanRow]:
    wanted = set(cfg.categories)
    rows: List[PlanRow] = []
    for item in iter_shm_user_services(client, cfg, session_id):
        category = item.get("category")
        if wanted and category not in wanted:
            continue
        user_service_id = item.get("user_service_id")
        service_id = item.get("service_id")
        if user_service_id is None or user_service_id == "":
            rows.append(
                PlanRow(
                    user_service_id=user_service_id,
                    username="",
                    shm_category=str(category) if category is not None else None,
                    service_id=service_id,
                    shm_hwid_device_limit_raw=None,
                    target_hwid_device_limit=TARGET_UNRESOLVED,
                    current_hwid_device_limit=None,
                    remnawave_user_id=None,
                    classification=CLASS_ERROR,
                    error_message="missing user_service_id",
                )
            )
            continue

        service, service_error = resolve_catalog_service(item, catalog)
        if service_error:
            rows.append(
                PlanRow(
                    user_service_id=user_service_id,
                    username=username_for_user_service(user_service_id),
                    shm_category=str(category) if category is not None else None,
                    service_id=service_id,
                    shm_hwid_device_limit_raw=None,
                    target_hwid_device_limit=TARGET_UNRESOLVED,
                    current_hwid_device_limit=None,
                    remnawave_user_id=None,
                    classification=CLASS_ERROR,
                    error_message=service_error,
                )
            )
            continue

        raw_hwid = extract_hwid_raw_from_service(service)
        username = username_for_user_service(user_service_id)
        kind, user_id, current_hwid, err = fetch_remnawave_user(client, cfg, username)
        rows.append(
            classify_row(
                user_service_id=user_service_id,
                category=str(category) if category is not None else None,
                service_id=service_id,
                raw_hwid=raw_hwid,
                fetch_kind=kind,
                remnawave_user_id=user_id,
                current_hwid=current_hwid,
                error_message=err,
            )
        )
    return rows


def summarize(rows: Sequence[PlanRow]) -> Dict[str, Any]:
    counts = {
        CLASS_ALREADY_CORRECT: 0,
        CLASS_RESET_TO_PANEL_DEFAULT: 0,
        CLASS_SET_EXPLICIT: 0,
        CLASS_DISABLE: 0,
        CLASS_MISSING: 0,
        CLASS_INVALID: 0,
        CLASS_ERROR: 0,
    }
    current_zero = 0
    current_null = 0
    current_explicit = 0
    zero_to_null = 0
    zero_stay_zero = 0
    for row in rows:
        counts[row.classification] = counts.get(row.classification, 0) + 1
        current = row.current_hwid_device_limit
        if current == 0:
            current_zero += 1
            if (
                row.target_hwid_device_limit is None
                and row.target_hwid_device_limit is not TARGET_UNRESOLVED
                and row.classification not in UNCLASSIFIABLE
            ):
                zero_to_null += 1
            if (
                row.target_hwid_device_limit == 0
                and row.classification not in UNCLASSIFIABLE
            ):
                zero_stay_zero += 1
        elif current is None:
            if row.classification not in {CLASS_MISSING, CLASS_ERROR, CLASS_INVALID}:
                current_null += 1
        elif isinstance(current, int) and current > 0:
            current_explicit += 1
    return {
        "total": len(rows),
        "plan_counts": counts,
        "hwid_snapshot": {
            "current_is_zero": current_zero,
            "current_is_null": current_null,
            "current_is_explicit_positive": current_explicit,
            "zero_should_become_null": zero_to_null,
            "zero_should_stay_zero": zero_stay_zero,
            "already_matches_shm": counts[CLASS_ALREADY_CORRECT],
            "unclassifiable": counts[CLASS_INVALID] + counts[CLASS_ERROR],
        },
        "apply_default": False,
        "note": (
            "Dry-run by default. Target comes from the SHM service setting; "
            "hwidDeviceLimit=0 is not blindly reset to null."
        ),
    }


REPORT_FIELDS = [
    "user_service_id",
    "username",
    "shm_category",
    "service_id",
    "shm_hwid_device_limit_raw",
    "target_resolved",
    "target_hwid_device_limit",
    "current_hwid_device_limit",
    "remnawave_user_id",
    "classification",
    "error_message",
]


def write_csv(path: str, rows: Sequence[PlanRow], fieldnames: Sequence[str]) -> None:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(fieldnames), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row.to_report_dict())
    atomic_write_text(path, buf.getvalue())


def write_reports(output_dir: str, rows: Sequence[PlanRow]) -> None:
    ensure_output_dir(output_dir)
    summary = summarize(rows)
    atomic_write_json(os.path.join(output_dir, "summary.json"), summary)
    atomic_write_json(
        os.path.join(output_dir, "plan.json"),
        [row.to_report_dict() for row in rows],
    )
    write_csv(os.path.join(output_dir, "plan.csv"), rows, REPORT_FIELDS)
    errors = [
        r for r in rows if r.classification in {CLASS_ERROR, CLASS_INVALID}
    ]
    missing = [r for r in rows if r.classification == CLASS_MISSING]
    write_csv(os.path.join(output_dir, "errors.csv"), errors, REPORT_FIELDS)
    write_csv(os.path.join(output_dir, "missing.csv"), missing, REPORT_FIELDS)


def parse_apply_usernames(raw: Sequence[str]) -> Tuple[str, ...]:
    """Preserve order, drop duplicates, reject empty names."""
    seen = set()
    names: List[str] = []
    for item in raw:
        name = str(item).strip()
        if not name:
            raise FatalError("--apply-username must be a non-empty username")
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    return tuple(names)


def select_apply_rows(
    rows: Sequence[PlanRow], allow_usernames: Sequence[str]
) -> List[PlanRow]:
    """Allow-list ∩ mutation-class. Allow-list is an extra constraint, not a bypass."""
    allow = set(allow_usernames)
    return [
        row
        for row in rows
        if row.username in allow and row.classification in APPLY_CLASSES
    ]


def live_state_matches_plan(
    row: PlanRow,
    *,
    live_user_id: Optional[int],
    live_current: Any,
    fetch_kind: str,
    fetch_error: Optional[str],
) -> Optional[str]:
    """Return an error if live Remnawave state drifted from the plan row."""
    if row.classification not in APPLY_CLASSES:
        return f"refusing PATCH: classification is {row.classification}"
    if row.target_hwid_device_limit is TARGET_UNRESOLVED:
        return "refusing PATCH: target unresolved"
    if fetch_kind != "ok":
        return f"refusing PATCH: live fetch {fetch_kind}" + (
            f" ({fetch_error})" if fetch_error else ""
        )
    if live_user_id != row.remnawave_user_id:
        return (
            "refusing PATCH: remnawave user id drift "
            f"(plan={row.remnawave_user_id!r} live={live_user_id!r})"
        )
    if live_current != row.current_hwid_device_limit:
        return (
            "refusing PATCH: current hwidDeviceLimit drift "
            f"(plan={row.current_hwid_device_limit!r} live={live_current!r})"
        )
    live_class = classify_hwid(
        target=row.target_hwid_device_limit, current=live_current
    )
    if live_class != row.classification:
        return (
            "refusing PATCH: classification drift "
            f"(plan={row.classification} live={live_class})"
        )
    return None


def write_apply_summary(
    output_dir: str,
    rows: Sequence[PlanRow],
    applied: Sequence[Dict[str, Any]],
    errors: Sequence[Dict[str, Any]],
    *,
    requested: int,
) -> Dict[str, Any]:
    summary = summarize(rows)
    summary["apply"] = {
        "requested": requested,
        "applied": len(applied),
        "failed": len(errors),
        "complete": len(errors) == 0 and len(applied) == requested,
    }
    atomic_write_json(os.path.join(output_dir, "summary.json"), summary)
    return summary


def apply_updates(
    client: HttpClient,
    cfg: ReconcileConfig,
    rows: Sequence[PlanRow],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if not cfg.apply_usernames:
        raise FatalError(
            "apply refused: --apply requires at least one --apply-username"
        )
    needs = select_apply_rows(rows, cfg.apply_usernames)
    applied: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    url = cfg.remnawave_panel_url.rstrip("/") + "/api/users"

    for row in needs:
        if not row.remnawave_user_id:
            errors.append(
                {
                    "stage": "precheck",
                    "username": row.username,
                    "user_service_id": row.user_service_id,
                    "error": "missing remnawave_user_id",
                }
            )
            return applied, errors
        if row.target_hwid_device_limit is TARGET_UNRESOLVED:
            errors.append(
                {
                    "stage": "precheck",
                    "username": row.username,
                    "user_service_id": row.user_service_id,
                    "error": "refusing PATCH: target unresolved",
                }
            )
            return applied, errors

        kind, live_id, live_current, fetch_error = fetch_remnawave_user(
            client, cfg, row.username
        )
        drift = live_state_matches_plan(
            row,
            live_user_id=live_id,
            live_current=live_current,
            fetch_kind=kind,
            fetch_error=fetch_error,
        )
        if drift:
            errors.append(
                {
                    "stage": "precheck",
                    "username": row.username,
                    "user_service_id": row.user_service_id,
                    "user_id": row.remnawave_user_id,
                    "error": drift,
                }
            )
            return applied, errors

        body = encode_hwid_patch_body(
            int(row.remnawave_user_id), row.target_hwid_device_limit
        )
        try:
            status, payload, _ = client.request(
                "PATCH",
                url,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {cfg.remnawave_token}",
                },
                body=body,
            )
        except FatalError as exc:
            errors.append(
                {
                    "stage": "patch",
                    "username": row.username,
                    "user_id": row.remnawave_user_id,
                    "target_hwid_device_limit": row.target_hwid_device_limit,
                    "error": str(exc),
                }
            )
            return applied, errors

        if status >= 400:
            errors.append(
                {
                    "stage": "patch",
                    "username": row.username,
                    "user_id": row.remnawave_user_id,
                    "target_hwid_device_limit": row.target_hwid_device_limit,
                    "error": f"HTTP {status}",
                }
            )
            return applied, errors

        verify_error = verify_hwid(
            client,
            cfg,
            username=row.username,
            expected_id=int(row.remnawave_user_id),
            expected_hwid=row.target_hwid_device_limit,
            patch_payload=payload,
        )
        if verify_error:
            errors.append(
                {
                    "stage": "verify",
                    "username": row.username,
                    "user_id": row.remnawave_user_id,
                    "target_hwid_device_limit": row.target_hwid_device_limit,
                    "error": verify_error,
                }
            )
            return applied, errors

        applied.append(
            {
                "user_service_id": row.user_service_id,
                "username": row.username,
                "remnawave_user_id": row.remnawave_user_id,
                "target_hwid_device_limit": row.target_hwid_device_limit,
                "classification": row.classification,
            }
        )

    return applied, errors


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reconcile Remnawave hwidDeviceLimit from SHM (dry-run by default)"
    )
    parser.add_argument("--shm-base-url", required=True)
    parser.add_argument("--shm-login", required=True)
    parser.add_argument(
        "--shm-password-env",
        required=True,
        help="Name of environment variable holding the SHM password",
    )
    parser.add_argument("--remnawave-panel-url", required=True)
    parser.add_argument(
        "--remnawave-token-env",
        required=True,
        help="Name of environment variable holding the Remnawave API token",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--category",
        action="append",
        default=[],
        help="Limit to this SHM category (repeatable). Default: all categories",
    )
    parser.add_argument("--page-size", type=int, default=250)
    parser.add_argument("--request-delay-ms", type=int, default=50)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default=None)
    parser.add_argument(
        "--apply-username",
        action="append",
        default=[],
        dest="apply_usernames",
        help=(
            "Username allowed to mutate on --apply (repeatable). "
            "Required with --apply. Additional constraint: the user must "
            "still be in a mutation class with the planned current/target."
        ),
    )
    return parser.parse_args(argv)


def config_from_args(
    args: argparse.Namespace, environ: Optional[Dict[str, str]] = None
) -> ReconcileConfig:
    env = environ if environ is not None else os.environ
    shm_password = env.get(args.shm_password_env, "")
    remnawave_token = env.get(args.remnawave_token_env, "")
    if not shm_password:
        raise FatalError(
            f"environment variable {args.shm_password_env} is empty or unset"
        )
    if not remnawave_token:
        raise FatalError(
            f"environment variable {args.remnawave_token_env} is empty or unset"
        )
    if args.page_size <= 0:
        raise FatalError("--page-size must be positive")
    if args.request_delay_ms < 0:
        raise FatalError("--request-delay-ms must be >= 0")
    apply_usernames = parse_apply_usernames(args.apply_usernames or ())
    if bool(args.apply) and not apply_usernames:
        raise FatalError(
            "apply refused: --apply requires at least one --apply-username"
        )
    return ReconcileConfig(
        shm_base_url=args.shm_base_url.rstrip("/"),
        shm_login=args.shm_login,
        shm_password=shm_password,
        remnawave_panel_url=args.remnawave_panel_url.rstrip("/"),
        remnawave_token=remnawave_token,
        output=args.output,
        categories=tuple(args.category or ()),
        page_size=args.page_size,
        request_delay_ms=args.request_delay_ms,
        apply=bool(args.apply),
        confirm=args.confirm,
        apply_usernames=apply_usernames,
    )


def run(cfg: ReconcileConfig, client: Optional[HttpClient] = None) -> int:
    interrupted_flag = {"value": False}

    def _on_signal(signum: int, frame: Any) -> None:  # noqa: ARG001
        interrupted_flag["value"] = True

    previous_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[sig] = signal.signal(sig, _on_signal)

    http = client or HttpClient(
        delay_ms=cfg.request_delay_ms,
        timeout=cfg.http_timeout,
        interrupted=lambda: interrupted_flag["value"],
    )

    try:
        if cfg.apply:
            if cfg.confirm != CONFIRM_PHRASE:
                raise FatalError(
                    "apply refused: require --apply and "
                    f"--confirm {CONFIRM_PHRASE}"
                )
            if not cfg.apply_usernames:
                raise FatalError(
                    "apply refused: --apply requires at least one --apply-username"
                )

        log("Authenticating to SHM...")
        session_id = shm_authenticate(http, cfg)
        log("Loading SHM service catalog...")
        catalog = load_service_catalog(http, cfg, session_id)
        log(f"Service catalog entries: {len({k for k in catalog if not isinstance(k, str)})}")

        log("Building HWID reconciliation plan...")
        rows = build_plan(http, cfg, session_id, catalog)
        write_reports(cfg.output, rows)
        summary = summarize(rows)
        plan_counts = summary["plan_counts"]
        snap = summary["hwid_snapshot"]
        log(
            "Plan written: "
            f"total={summary['total']} "
            f"already_correct={plan_counts[CLASS_ALREADY_CORRECT]} "
            f"reset_to_panel_default={plan_counts[CLASS_RESET_TO_PANEL_DEFAULT]} "
            f"set_explicit={plan_counts[CLASS_SET_EXPLICIT]} "
            f"disable={plan_counts[CLASS_DISABLE]} "
            f"missing={plan_counts[CLASS_MISSING]} "
            f"invalid={plan_counts[CLASS_INVALID]} "
            f"error={plan_counts[CLASS_ERROR]}"
        )
        log(
            "HWID snapshot: "
            f"current_zero={snap['current_is_zero']} "
            f"zero_to_null={snap['zero_should_become_null']} "
            f"zero_stay_zero={snap['zero_should_stay_zero']} "
            f"explicit_positive={snap['current_is_explicit_positive']} "
            f"already_matches={snap['already_matches_shm']} "
            f"unclassifiable={snap['unclassifiable']}"
        )

        if not cfg.apply:
            log("Dry-run complete (no Remnawave changes).")
            return 0

        log(
            "Applying HWID updates via sequential PATCH /api/users "
            f"(allow-list={len(cfg.apply_usernames)})..."
        )
        requested = len(select_apply_rows(rows, cfg.apply_usernames))
        applied, errors = apply_updates(http, cfg, rows)
        atomic_write_json(os.path.join(cfg.output, "applied.json"), applied)
        if errors:
            atomic_write_json(os.path.join(cfg.output, "errors.json"), errors)
        apply_summary = write_apply_summary(
            cfg.output, rows, applied, errors, requested=requested
        )
        apply_info = apply_summary["apply"]
        if errors:
            log(
                "Apply stopped with errors after "
                f"{apply_info['applied']}/{apply_info['requested']} confirmed updates."
            )
            return 1
        log(
            f"Apply complete: {apply_info['applied']}/"
            f"{apply_info['requested']} users updated."
        )
        return 0
    except Interrupted:
        log("Interrupted.")
        return 130
    except FatalError as exc:
        msg = redact_secrets(
            str(exc),
            [cfg.shm_password, cfg.remnawave_token],
        )
        log(f"ERROR: {msg}")
        return 1
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = parse_args(argv)
        cfg = config_from_args(args)
    except FatalError as exc:
        log(f"ERROR: {exc}")
        return 1
    except SystemExit:
        raise
    return run(cfg)


if __name__ == "__main__":
    sys.exit(main())
