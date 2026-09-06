#!/usr/bin/env python3
"""Reconcile Remnawave traffic limits from SHM service settings.

Target is taken from the catalog service settings
``us.service.settings.remnawave.traffic_limit_bytes`` and
``us.service.settings.remnawave.traffic_limit_strategy``.

Unlike HWID reconciliation, an absent remnawave block or absent
``traffic_limit_bytes`` is **unmanaged_service** — never inferred as
target=0. Only services that explicitly configure a traffic limit are
mutated.

Dry-run is the default. Apply requires ``--apply``,
``--confirm RECONCILE_TRAFFIC_LIMITS``, and a scoped filter
(``--category`` and/or ``--service-id``). Optional ``--apply-username``
is an extra allow-list and never replaces that scope. Users already at
or over the target limit are skipped unless ``--include-over-limit``.

Reconciliation never resets traffic and never writes usedTrafficBytes,
lastTrafficResetAt, status, expireAt, squads, or hwidDeviceLimit.
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
from collections import Counter
from dataclasses import dataclass, field
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
from reconcile_hwid_limits import (
    extract_remnawave_settings,
    load_service_catalog,
    parse_apply_usernames,
    resolve_catalog_service,
)


CONFIRM_PHRASE = "RECONCILE_TRAFFIC_LIMITS"
HTTP_TIMEOUT_SEC = 30
VERIFY_RETRY_ATTEMPTS = 5
VERIFY_RETRY_DELAY_SEC = 1.0
GIB = 1024 ** 3

CLASS_ALREADY_CORRECT = "already_correct"
CLASS_NEEDS_LIMIT = "needs_set_limit"
CLASS_NEEDS_STRATEGY = "needs_set_strategy"
CLASS_NEEDS_BOTH = "needs_set_limit_and_strategy"
CLASS_UNMANAGED = "unmanaged_service"
CLASS_MISSING = "missing_in_remnawave"
CLASS_INVALID = "invalid_shm_setting"
CLASS_ERROR = "error"

APPLY_CLASSES = frozenset(
    {CLASS_NEEDS_LIMIT, CLASS_NEEDS_STRATEGY, CLASS_NEEDS_BOTH}
)
UNCLASSIFIABLE = frozenset({CLASS_INVALID, CLASS_ERROR})

SHM_STRATEGIES = frozenset({"NO_RESET", "DAY", "WEEK", "MONTH"})
# Backend 3.2.3 also accepts MONTH_ROLLING; the SHM template does not.
REMNAWAVE_STRATEGIES = SHM_STRATEGIES | {"MONTH_ROLLING"}
MONTH_ROLLING = "MONTH_ROLLING"

PATCH_KEYS = frozenset({"id", "trafficLimitBytes", "trafficLimitStrategy"})
RESET_TRAFFIC_MARKER = "reset-traffic"

TARGET_UNRESOLVED = object()
NON_NEGATIVE_INT_RE = re.compile(r"^[0-9]+$")


class TrafficSettingError(ValueError):
    """SHM traffic_limit_* cannot be parsed."""


@dataclass
class TrafficShmExtract:
    remnawave_present: bool
    bytes_key_present: bool
    strategy_key_present: bool
    bytes_raw: Any = None
    strategy_raw: Any = None

    @property
    def bytes_explicit(self) -> bool:
        if not self.remnawave_present or not self.bytes_key_present:
            return False
        raw = self.bytes_raw
        if raw is None:
            return False
        if isinstance(raw, str) and raw.strip() in ("", "null"):
            return False
        return True


@dataclass
class RemnawaveTrafficState:
    numeric_id: Optional[int]
    traffic_limit_bytes: Any
    traffic_limit_strategy: Optional[str]
    status: Optional[str]
    used_traffic_bytes: Optional[int]
    expire_at: Optional[str] = None
    hwid_device_limit: Any = None
    external_squad_uuid: Optional[str] = None
    active_internal_squads: Any = None
    last_traffic_reset_at: Optional[str] = None
    field_paths: Dict[str, str] = field(default_factory=dict)


SKIP_ALREADY_CORRECT = "already_correct"
SKIP_OVER_LIMIT = "over_limit"
ACTION_PATCH = "patch"
ACTION_SKIP = "skip"
ACTION_ERROR = "error"


@dataclass
class PlanRow:
    user_service_id: Any
    username: str
    shm_category: Optional[str]
    service_id: Any
    service_name: Optional[str]
    current_traffic_limit_bytes: Any
    target_traffic_limit_bytes: Any
    current_traffic_limit_strategy: Optional[str]
    target_traffic_limit_strategy: Any
    used_traffic_bytes: Optional[int]
    remnawave_user_id: Any
    remnawave_status: Optional[str]
    classification: str
    would_be_over_limit_now: bool = False
    error_message: Optional[str] = None
    shm_traffic_limit_bytes_raw: Any = None
    shm_traffic_limit_strategy_raw: Any = None
    managed: bool = False
    month_rolling_in_shm: bool = False
    month_rolling_in_remnawave: bool = False

    def to_report_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "user_service_id": self.user_service_id,
            "username": self.username,
            "shm_category": self.shm_category,
            "service_id": self.service_id,
            "service_name": self.service_name,
            "current_traffic_limit_bytes": self.current_traffic_limit_bytes,
            "current_traffic_limit_strategy": self.current_traffic_limit_strategy,
            "used_traffic_bytes": self.used_traffic_bytes,
            "used_traffic_gib": bytes_to_gib(self.used_traffic_bytes),
            "would_be_over_limit_now": self.would_be_over_limit_now,
            "classification": self.classification,
            "error_message": self.error_message,
            "remnawave_user_id": self.remnawave_user_id,
            "remnawave_status": self.remnawave_status,
            "managed": self.managed,
            "shm_traffic_limit_bytes_raw": self.shm_traffic_limit_bytes_raw,
            "shm_traffic_limit_strategy_raw": self.shm_traffic_limit_strategy_raw,
        }
        if self.target_traffic_limit_bytes is TARGET_UNRESOLVED:
            data["target_resolved"] = False
        else:
            data["target_resolved"] = True
            data["target_traffic_limit_bytes"] = self.target_traffic_limit_bytes
            data["target_traffic_limit_strategy"] = (
                self.target_traffic_limit_strategy
            )
            data["target_limit_gib"] = bytes_to_gib(self.target_traffic_limit_bytes)
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
    service_ids: Tuple[str, ...] = ()
    page_size: int = 250
    request_delay_ms: int = 50
    apply: bool = False
    confirm: Optional[str] = None
    apply_usernames: Tuple[str, ...] = ()
    include_over_limit: bool = False
    http_timeout: float = HTTP_TIMEOUT_SEC
    verify_retry_attempts: int = VERIFY_RETRY_ATTEMPTS
    verify_retry_delay_sec: float = VERIFY_RETRY_DELAY_SEC


def bytes_to_gib(value: Any) -> Optional[float]:
    if value is None or value is TARGET_UNRESOLVED:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value / GIB


def over_limit_now(
    *,
    target_bytes: Any,
    used_bytes: Optional[int],
) -> bool:
    if target_bytes is TARGET_UNRESOLVED or target_bytes is None:
        return False
    if not isinstance(target_bytes, int) or isinstance(target_bytes, bool):
        return False
    if target_bytes <= 0 or used_bytes is None:
        return False
    return used_bytes >= target_bytes


def parse_non_negative_int(raw: Any, *, field_name: str) -> int:
    if isinstance(raw, bool):
        raise TrafficSettingError(
            f"invalid {field_name}: {raw!r} (must be integer >= 0)"
        )
    if isinstance(raw, int):
        if raw < 0:
            raise TrafficSettingError(
                f"invalid {field_name}: {raw!r} (must be integer >= 0)"
            )
        return raw
    if isinstance(raw, float):
        raise TrafficSettingError(
            f"invalid {field_name}: {raw!r} (must be integer >= 0)"
        )
    if isinstance(raw, str):
        value = raw.strip()
        if NON_NEGATIVE_INT_RE.fullmatch(value):
            return int(value)
        raise TrafficSettingError(
            f"invalid {field_name}: {raw!r} (must be integer >= 0)"
        )
    raise TrafficSettingError(
        f"invalid {field_name}: {raw!r} (must be integer >= 0)"
    )


def parse_shm_traffic_limit_bytes(raw: Any) -> int:
    return parse_non_negative_int(raw, field_name="traffic_limit_bytes")


def parse_shm_traffic_limit_strategy(raw: Any) -> str:
    if not isinstance(raw, str):
        raise TrafficSettingError(
            f"invalid traffic_limit_strategy: {raw!r} "
            f"(allowed: {', '.join(sorted(SHM_STRATEGIES))})"
        )
    value = raw.strip().upper()
    if value == MONTH_ROLLING:
        raise TrafficSettingError(
            "invalid traffic_limit_strategy: 'MONTH_ROLLING' "
            "(Remnawave 3.2.3 accepts it; current SHM template does not)"
        )
    if value not in SHM_STRATEGIES:
        raise TrafficSettingError(
            f"invalid traffic_limit_strategy: {raw!r} "
            f"(allowed: {', '.join(sorted(SHM_STRATEGIES))})"
        )
    return value


def extract_traffic_settings(service: Any) -> TrafficShmExtract:
    remnawave = extract_remnawave_settings(service)
    if remnawave is None:
        return TrafficShmExtract(
            remnawave_present=False,
            bytes_key_present=False,
            strategy_key_present=False,
        )
    bytes_key = "traffic_limit_bytes" in remnawave
    strategy_key = "traffic_limit_strategy" in remnawave
    return TrafficShmExtract(
        remnawave_present=True,
        bytes_key_present=bytes_key,
        strategy_key_present=strategy_key,
        bytes_raw=remnawave.get("traffic_limit_bytes") if bytes_key else None,
        strategy_raw=(
            remnawave.get("traffic_limit_strategy") if strategy_key else None
        ),
    )


def parse_managed_shm_target(
    extract: TrafficShmExtract,
) -> Tuple[int, str]:
    """Return (bytes, strategy). Caller must ensure extract.bytes_explicit."""
    target_bytes = parse_shm_traffic_limit_bytes(extract.bytes_raw)
    if not extract.strategy_key_present:
        raise TrafficSettingError(
            "traffic_limit_bytes is set but traffic_limit_strategy is missing"
        )
    if extract.strategy_raw is None or (
        isinstance(extract.strategy_raw, str)
        and extract.strategy_raw.strip() in ("", "null")
    ):
        raise TrafficSettingError(
            "traffic_limit_bytes is set but traffic_limit_strategy is empty"
        )
    target_strategy = parse_shm_traffic_limit_strategy(extract.strategy_raw)
    return target_bytes, target_strategy


def service_name_from_service(service: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(service, dict):
        return None
    for key in ("name", "service_name", "title"):
        value = service.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def normalize_current_bytes(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    return parse_non_negative_int(value, field_name="trafficLimitBytes")


def normalize_current_strategy(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"unexpected trafficLimitStrategy: {value!r}")
    strategy = value.strip().upper()
    if strategy not in REMNAWAVE_STRATEGIES:
        raise ValueError(f"unexpected trafficLimitStrategy: {value!r}")
    return strategy


def normalize_used_bytes(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    return parse_non_negative_int(value, field_name="usedTrafficBytes")


def classify_traffic(
    *,
    target_bytes: int,
    target_strategy: str,
    current_bytes: Any,
    current_strategy: Optional[str],
) -> str:
    bytes_match = current_bytes == target_bytes
    strategy_match = current_strategy == target_strategy
    if bytes_match and strategy_match:
        return CLASS_ALREADY_CORRECT
    if (not bytes_match) and (not strategy_match):
        return CLASS_NEEDS_BOTH
    if not bytes_match:
        return CLASS_NEEDS_LIMIT
    return CLASS_NEEDS_STRATEGY


def build_traffic_patch_payload(
    user_id: int, target_bytes: int, target_strategy: str
) -> Dict[str, Any]:
    return {
        "id": user_id,
        "trafficLimitBytes": target_bytes,
        "trafficLimitStrategy": target_strategy,
    }


def encode_traffic_patch_body(
    user_id: int, target_bytes: int, target_strategy: str
) -> bytes:
    payload = build_traffic_patch_payload(user_id, target_bytes, target_strategy)
    if set(payload.keys()) != PATCH_KEYS:
        raise FatalError("internal error: PATCH payload keys drifted")
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def _user_sources(payload: Any) -> List[Tuple[str, Dict[str, Any]]]:
    if not isinstance(payload, dict):
        return []
    sources: List[Tuple[str, Dict[str, Any]]] = []
    response = payload.get("response", payload)
    prefix = "response" if "response" in payload else ""
    if isinstance(response, dict):
        user = response.get("user")
        if isinstance(user, dict):
            user_prefix = f"{prefix}.user" if prefix else "user"
            sources.append((user_prefix, user))
        src_prefix = prefix or "response"
        sources.append((src_prefix, response))
    return sources


def parse_remnawave_user(payload: Any) -> RemnawaveTrafficState:
    """Normalize Remnawave 3.2.3 GET /api/users/by-username response.

    Contract paths (3.2.3):
      response.id
      response.trafficLimitBytes
      response.trafficLimitStrategy
      response.status
      response.userTraffic.usedTrafficBytes
    Legacy top-level usedTrafficBytes is accepted as a fallback.
    """
    state = RemnawaveTrafficState(
        numeric_id=None,
        traffic_limit_bytes=None,
        traffic_limit_strategy=None,
        status=None,
        used_traffic_bytes=None,
    )
    for prefix, src in _user_sources(payload):
        if state.numeric_id is None:
            value = src.get("id")
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                state.numeric_id = value
                state.field_paths["id"] = f"{prefix}.id"
            elif isinstance(value, str) and NON_NEGATIVE_INT_RE.fullmatch(value):
                parsed = int(value)
                if parsed > 0:
                    state.numeric_id = parsed
                    state.field_paths["id"] = f"{prefix}.id"
        if "trafficLimitBytes" in src and "trafficLimitBytes" not in state.field_paths:
            state.traffic_limit_bytes = src.get("trafficLimitBytes")
            state.field_paths["trafficLimitBytes"] = f"{prefix}.trafficLimitBytes"
        if (
            "trafficLimitStrategy" in src
            and "trafficLimitStrategy" not in state.field_paths
        ):
            state.traffic_limit_strategy = src.get("trafficLimitStrategy")
            state.field_paths["trafficLimitStrategy"] = (
                f"{prefix}.trafficLimitStrategy"
            )
        if "status" in src and "status" not in state.field_paths:
            status = src.get("status")
            state.status = status if isinstance(status, str) and status else None
            state.field_paths["status"] = f"{prefix}.status"
        if "usedTrafficBytes" not in state.field_paths:
            nested = src.get("userTraffic")
            if isinstance(nested, dict) and "usedTrafficBytes" in nested:
                state.used_traffic_bytes = nested.get("usedTrafficBytes")
                state.field_paths["usedTrafficBytes"] = (
                    f"{prefix}.userTraffic.usedTrafficBytes"
                )
            elif "usedTrafficBytes" in src:
                state.used_traffic_bytes = src.get("usedTrafficBytes")
                state.field_paths["usedTrafficBytes"] = f"{prefix}.usedTrafficBytes"
        if "expireAt" in src and "expireAt" not in state.field_paths:
            value = src.get("expireAt")
            state.expire_at = value if isinstance(value, str) and value else None
            state.field_paths["expireAt"] = f"{prefix}.expireAt"
        if "hwidDeviceLimit" in src and "hwidDeviceLimit" not in state.field_paths:
            state.hwid_device_limit = src.get("hwidDeviceLimit")
            state.field_paths["hwidDeviceLimit"] = f"{prefix}.hwidDeviceLimit"
        if "externalSquadUuid" in src and "externalSquadUuid" not in state.field_paths:
            value = src.get("externalSquadUuid")
            state.external_squad_uuid = (
                None if value in (None, "") else str(value)
            )
            state.field_paths["externalSquadUuid"] = f"{prefix}.externalSquadUuid"
        if (
            "activeInternalSquads" in src
            and "activeInternalSquads" not in state.field_paths
        ):
            state.active_internal_squads = src.get("activeInternalSquads")
            state.field_paths["activeInternalSquads"] = (
                f"{prefix}.activeInternalSquads"
            )
        if (
            "lastTrafficResetAt" in src
            and "lastTrafficResetAt" not in state.field_paths
        ):
            value = src.get("lastTrafficResetAt")
            state.last_traffic_reset_at = (
                value if isinstance(value, str) and value else None
            )
            state.field_paths["lastTrafficResetAt"] = f"{prefix}.lastTrafficResetAt"
    return state


def fetch_remnawave_user(
    client: HttpClient,
    cfg: ReconcileConfig,
    username: str,
) -> Tuple[str, Optional[RemnawaveTrafficState], Optional[str]]:
    """Return (status_kind, state, error_message).

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
        return "error", None, str(exc)
    if status == 404:
        return "missing", None, None
    if status >= 400:
        return "error", None, f"HTTP {status}"
    state = parse_remnawave_user(payload)
    if state.numeric_id is None:
        return "error", None, "numeric user id missing in response"
    try:
        state.traffic_limit_bytes = normalize_current_bytes(state.traffic_limit_bytes)
        state.traffic_limit_strategy = normalize_current_strategy(
            state.traffic_limit_strategy
        )
        state.used_traffic_bytes = normalize_used_bytes(state.used_traffic_bytes)
    except (TrafficSettingError, ValueError) as exc:
        return "error", state, str(exc)
    return "ok", state, None


def used_bytes_safe_vs_baseline(
    baseline: Optional[int], current: Optional[int], *, label: str
) -> Optional[str]:
    """Allow equal or growing used traffic; refuse a decrease.

    ``usedTrafficBytes`` is a live monotonic counter. Growth is normal.
    A decrease means a concurrent reset or another unsafe mutation.
    """
    if baseline is None:
        return None
    if current is None:
        return f"{label}: usedTrafficBytes missing in live state"
    if current < baseline:
        return (
            f"{label}: usedTrafficBytes decreased "
            f"(previous={baseline} live={current})"
        )
    return None


def squad_fingerprint(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    uuids: List[str] = []
    for item in value:
        if isinstance(item, dict) and item.get("uuid"):
            uuids.append(str(item["uuid"]))
        elif isinstance(item, str) and item:
            uuids.append(item)
    return tuple(sorted(uuids))


def _normalize_parsed_state(state: RemnawaveTrafficState) -> RemnawaveTrafficState:
    state.traffic_limit_bytes = normalize_current_bytes(state.traffic_limit_bytes)
    state.traffic_limit_strategy = normalize_current_strategy(
        state.traffic_limit_strategy
    )
    state.used_traffic_bytes = normalize_used_bytes(state.used_traffic_bytes)
    return state


def verify_traffic(
    client: HttpClient,
    cfg: ReconcileConfig,
    *,
    username: str,
    expected_id: int,
    expected_bytes: int,
    expected_strategy: str,
    pre_used: Optional[int],
    pre_status: Optional[str],
    pre: RemnawaveTrafficState,
    patch_payload: Any,
) -> Tuple[Optional[str], Optional[RemnawaveTrafficState]]:
    """Return (error, verified_state). Growth of used traffic is allowed."""

    def _check(state: RemnawaveTrafficState) -> Optional[str]:
        if state.numeric_id != expected_id:
            return (
                f"id mismatch: got {state.numeric_id}, expected {expected_id}"
            )
        if state.traffic_limit_bytes != expected_bytes:
            return (
                "trafficLimitBytes mismatch: "
                f"got {state.traffic_limit_bytes!r}, expected {expected_bytes!r}"
            )
        if state.traffic_limit_strategy != expected_strategy:
            return (
                "trafficLimitStrategy mismatch: "
                f"got {state.traffic_limit_strategy!r}, expected {expected_strategy!r}"
            )
        used_err = used_bytes_safe_vs_baseline(
            pre_used, state.used_traffic_bytes, label="verify"
        )
        if used_err:
            return used_err
        if state.status != pre_status:
            return (
                "status changed unexpectedly: "
                f"got {state.status!r}, expected {pre_status!r}"
            )
        if state.expire_at != pre.expire_at:
            return (
                "expireAt changed unexpectedly: "
                f"got {state.expire_at!r}, expected {pre.expire_at!r}"
            )
        if state.hwid_device_limit != pre.hwid_device_limit:
            return (
                "hwidDeviceLimit changed unexpectedly: "
                f"got {state.hwid_device_limit!r}, expected {pre.hwid_device_limit!r}"
            )
        if state.external_squad_uuid != pre.external_squad_uuid:
            return (
                "externalSquadUuid changed unexpectedly: "
                f"got {state.external_squad_uuid!r}, expected {pre.external_squad_uuid!r}"
            )
        if squad_fingerprint(state.active_internal_squads) != squad_fingerprint(
            pre.active_internal_squads
        ):
            return (
                "activeInternalSquads changed unexpectedly: "
                f"got {state.active_internal_squads!r}, "
                f"expected {pre.active_internal_squads!r}"
            )
        if state.last_traffic_reset_at != pre.last_traffic_reset_at:
            return (
                "lastTrafficResetAt changed unexpectedly: "
                f"got {state.last_traffic_reset_at!r}, "
                f"expected {pre.last_traffic_reset_at!r}"
            )
        return None

    patched = parse_remnawave_user(patch_payload)
    try:
        patched = _normalize_parsed_state(patched)
    except (TrafficSettingError, ValueError):
        patched = RemnawaveTrafficState(
            numeric_id=patched.numeric_id,
            traffic_limit_bytes=None,
            traffic_limit_strategy=None,
            status=patched.status,
            used_traffic_bytes=None,
        )
    if (
        patched.numeric_id == expected_id
        and "trafficLimitBytes" in patched.field_paths
        and "trafficLimitStrategy" in patched.field_paths
    ):
        err = _check(patched)
        if err is None:
            return None, patched

    last_err = "PATCH response traffic fields not confirmed"
    last_state: Optional[RemnawaveTrafficState] = None
    attempts = max(1, int(cfg.verify_retry_attempts))
    for attempt in range(1, attempts + 1):
        if cfg.verify_retry_delay_sec > 0:
            time.sleep(cfg.verify_retry_delay_sec)
        kind, state, err = fetch_remnawave_user(client, cfg, username)
        if kind == "ok" and state is not None:
            last_state = state
            check = _check(state)
            if check is None:
                return None, state
            last_err = (
                f"GET verify attempt {attempt}/{attempts} failed: {check}"
            )
        else:
            last_err = (
                f"GET verify attempt {attempt}/{attempts} failed: "
                f"kind={kind} error={err}"
            )
    return last_err, last_state


def _month_rolling_in_raw(raw: Any) -> bool:
    return isinstance(raw, str) and raw.strip().upper() == MONTH_ROLLING


def classify_row(
    *,
    user_service_id: Any,
    category: Optional[str],
    service_id: Any,
    service_name: Optional[str],
    extract: TrafficShmExtract,
    fetch_kind: str,
    remnawave: Optional[RemnawaveTrafficState],
    error_message: Optional[str],
) -> PlanRow:
    username = (
        username_for_user_service(user_service_id)
        if user_service_id not in (None, "")
        else ""
    )
    current_bytes = remnawave.traffic_limit_bytes if remnawave else None
    current_strategy = remnawave.traffic_limit_strategy if remnawave else None
    used = remnawave.used_traffic_bytes if remnawave else None
    user_id = remnawave.numeric_id if remnawave else None
    status = remnawave.status if remnawave else None
    month_rolling_rw = current_strategy == MONTH_ROLLING
    month_rolling_shm = _month_rolling_in_raw(extract.strategy_raw)

    base = dict(
        user_service_id=user_service_id,
        username=username,
        shm_category=category,
        service_id=service_id,
        service_name=service_name,
        current_traffic_limit_bytes=current_bytes,
        current_traffic_limit_strategy=current_strategy,
        used_traffic_bytes=used,
        remnawave_user_id=user_id,
        remnawave_status=status,
        shm_traffic_limit_bytes_raw=extract.bytes_raw,
        shm_traffic_limit_strategy_raw=extract.strategy_raw,
        month_rolling_in_shm=month_rolling_shm,
        month_rolling_in_remnawave=month_rolling_rw,
    )

    if not extract.bytes_explicit:
        return PlanRow(
            **base,
            target_traffic_limit_bytes=TARGET_UNRESOLVED,
            target_traffic_limit_strategy=TARGET_UNRESOLVED,
            classification=CLASS_UNMANAGED,
            managed=False,
            error_message=None,
        )

    try:
        target_bytes, target_strategy = parse_managed_shm_target(extract)
        parse_error: Optional[str] = None
    except TrafficSettingError as exc:
        target_bytes = TARGET_UNRESOLVED  # type: ignore[assignment]
        target_strategy = TARGET_UNRESOLVED  # type: ignore[assignment]
        parse_error = str(exc)

    if parse_error:
        return PlanRow(
            **base,
            target_traffic_limit_bytes=TARGET_UNRESOLVED,
            target_traffic_limit_strategy=TARGET_UNRESOLVED,
            classification=CLASS_INVALID,
            managed=True,
            error_message=parse_error,
        )

    risk = over_limit_now(target_bytes=target_bytes, used_bytes=used)
    if fetch_kind == "missing":
        return PlanRow(
            **base,
            target_traffic_limit_bytes=target_bytes,
            target_traffic_limit_strategy=target_strategy,
            classification=CLASS_MISSING,
            would_be_over_limit_now=False,
            managed=True,
            error_message=None,
        )
    if fetch_kind == "error":
        return PlanRow(
            **base,
            target_traffic_limit_bytes=target_bytes,
            target_traffic_limit_strategy=target_strategy,
            classification=CLASS_ERROR,
            would_be_over_limit_now=False,
            managed=True,
            error_message=error_message or "unexpected remnawave error",
        )
    return PlanRow(
        **base,
        target_traffic_limit_bytes=target_bytes,
        target_traffic_limit_strategy=target_strategy,
        classification=classify_traffic(
            target_bytes=target_bytes,
            target_strategy=target_strategy,
            current_bytes=current_bytes,
            current_strategy=current_strategy,
        ),
        would_be_over_limit_now=risk,
        managed=True,
        error_message=None,
    )


def _wanted_service_ids(cfg: ReconcileConfig) -> set:
    return set(cfg.service_ids)


def _service_id_wanted(service_id: Any, wanted: set) -> bool:
    if not wanted:
        return True
    if service_id is None or service_id == "":
        return False
    return str(service_id) in wanted


def build_plan(
    client: HttpClient,
    cfg: ReconcileConfig,
    session_id: str,
    catalog: Dict[Any, Dict[str, Any]],
) -> Tuple[List[PlanRow], Optional[Dict[str, str]]]:
    wanted_categories = set(cfg.categories)
    wanted_services = _wanted_service_ids(cfg)
    rows: List[PlanRow] = []
    schema: Optional[Dict[str, str]] = None

    for item in iter_shm_user_services(client, cfg, session_id):
        category = item.get("category")
        if wanted_categories and category not in wanted_categories:
            continue
        service_id = item.get("service_id")
        if not _service_id_wanted(service_id, wanted_services):
            continue
        user_service_id = item.get("user_service_id")
        if user_service_id is None or user_service_id == "":
            rows.append(
                PlanRow(
                    user_service_id=user_service_id,
                    username="",
                    shm_category=str(category) if category is not None else None,
                    service_id=service_id,
                    service_name=None,
                    current_traffic_limit_bytes=None,
                    target_traffic_limit_bytes=TARGET_UNRESOLVED,
                    current_traffic_limit_strategy=None,
                    target_traffic_limit_strategy=TARGET_UNRESOLVED,
                    used_traffic_bytes=None,
                    remnawave_user_id=None,
                    remnawave_status=None,
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
                    service_name=None,
                    current_traffic_limit_bytes=None,
                    target_traffic_limit_bytes=TARGET_UNRESOLVED,
                    current_traffic_limit_strategy=None,
                    target_traffic_limit_strategy=TARGET_UNRESOLVED,
                    used_traffic_bytes=None,
                    remnawave_user_id=None,
                    remnawave_status=None,
                    classification=CLASS_ERROR,
                    error_message=service_error,
                )
            )
            continue

        extract = extract_traffic_settings(service)
        username = username_for_user_service(user_service_id)
        kind, state, err = fetch_remnawave_user(client, cfg, username)
        if schema is None and state is not None and state.field_paths:
            schema = dict(state.field_paths)
        rows.append(
            classify_row(
                user_service_id=user_service_id,
                category=str(category) if category is not None else None,
                service_id=service_id,
                service_name=service_name_from_service(service),
                extract=extract,
                fetch_kind=kind,
                remnawave=state,
                error_message=err,
            )
        )
    return rows, schema


def _managed_service_keys(rows: Sequence[PlanRow]) -> List[Dict[str, Any]]:
    seen = set()
    services: List[Dict[str, Any]] = []
    for row in rows:
        if not row.managed:
            continue
        key = (row.service_id, row.service_name, row.shm_category)
        if key in seen:
            continue
        seen.add(key)
        services.append(
            {
                "service_id": row.service_id,
                "service_name": row.service_name,
                "shm_category": row.shm_category,
            }
        )
    return services


def summarize(
    rows: Sequence[PlanRow],
    *,
    remnawave_schema: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    counts = {
        CLASS_ALREADY_CORRECT: 0,
        CLASS_NEEDS_LIMIT: 0,
        CLASS_NEEDS_STRATEGY: 0,
        CLASS_NEEDS_BOTH: 0,
        CLASS_UNMANAGED: 0,
        CLASS_MISSING: 0,
        CLASS_INVALID: 0,
        CLASS_ERROR: 0,
    }
    combo_counter: Counter[Tuple[Any, Any]] = Counter()
    over_limit = 0
    shm_month_rolling = 0
    rw_month_rolling = 0
    managed_users = 0
    for row in rows:
        counts[row.classification] = counts.get(row.classification, 0) + 1
        if row.managed:
            managed_users += 1
        if row.would_be_over_limit_now:
            over_limit += 1
        if row.month_rolling_in_shm:
            shm_month_rolling += 1
        if row.month_rolling_in_remnawave:
            rw_month_rolling += 1
        if (
            row.target_traffic_limit_bytes is not TARGET_UNRESOLVED
            and row.target_traffic_limit_strategy is not TARGET_UNRESOLVED
            and row.classification not in UNCLASSIFIABLE
            and row.classification != CLASS_UNMANAGED
        ):
            combo_counter[
                (row.target_traffic_limit_bytes, row.target_traffic_limit_strategy)
            ] += 1

    combinations = [
        {
            "target_traffic_limit_bytes": bytes_value,
            "target_traffic_limit_strategy": strategy,
            "users_count": count,
            "target_limit_gib": bytes_to_gib(bytes_value),
        }
        for (bytes_value, strategy), count in sorted(
            combo_counter.items(), key=lambda item: (-item[1], item[0][0], item[0][1])
        )
    ]
    return {
        "total": len(rows),
        "total_user_services_inspected": len(rows),
        "managed_users": managed_users,
        "managed_services": _managed_service_keys(rows),
        "plan_counts": counts,
        "risk": {"would_be_over_limit_now_count": over_limit},
        "target_combinations": combinations,
        "remnawave_schema": remnawave_schema
        or {
            "id": "response.id",
            "trafficLimitBytes": "response.trafficLimitBytes",
            "trafficLimitStrategy": "response.trafficLimitStrategy",
            "status": "response.status",
            "usedTrafficBytes": "response.userTraffic.usedTrafficBytes",
        },
        "month_rolling": {
            "supported_by_remnawave_3_2_3": True,
            "accepted_by_shm_template": False,
            "shm_invalid_month_rolling_count": shm_month_rolling,
            "remnawave_current_month_rolling_count": rw_month_rolling,
            "note": (
                "MONTH_ROLLING is not added to the SHM contract; "
                "an SHM value of MONTH_ROLLING is invalid_shm_setting."
            ),
        },
        "apply_default": False,
        "note": (
            "Dry-run by default. Target comes from explicit SHM "
            "service.settings.remnawave.traffic_limit_*; absent settings are "
            "unmanaged_service, not target=0. Traffic is never reset."
        ),
    }


REPORT_FIELDS = [
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
    "error_message",
]


def write_csv(path: str, rows: Sequence[PlanRow], fieldnames: Sequence[str]) -> None:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(fieldnames), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row.to_report_dict())
    atomic_write_text(path, buf.getvalue())


def write_reports(
    output_dir: str,
    rows: Sequence[PlanRow],
    *,
    remnawave_schema: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    ensure_output_dir(output_dir)
    summary = summarize(rows, remnawave_schema=remnawave_schema)
    atomic_write_json(os.path.join(output_dir, "summary.json"), summary)
    atomic_write_json(
        os.path.join(output_dir, "plan.json"),
        [row.to_report_dict() for row in rows],
    )
    write_csv(os.path.join(output_dir, "plan.csv"), rows, REPORT_FIELDS)
    errors = [r for r in rows if r.classification in {CLASS_ERROR, CLASS_INVALID}]
    missing = [r for r in rows if r.classification == CLASS_MISSING]
    over_limit = [r for r in rows if r.would_be_over_limit_now]
    write_csv(os.path.join(output_dir, "errors.csv"), errors, REPORT_FIELDS)
    write_csv(os.path.join(output_dir, "missing.csv"), missing, REPORT_FIELDS)
    write_csv(os.path.join(output_dir, "over_limit.csv"), over_limit, REPORT_FIELDS)
    return summary


def parse_repeatable_ids(raw: Sequence[str], *, flag: str) -> Tuple[str, ...]:
    seen = set()
    values: List[str] = []
    for item in raw:
        value = str(item).strip()
        if not value:
            raise FatalError(f"{flag} must be a non-empty value")
        if value in seen:
            continue
        seen.add(value)
        values.append(value)
    return tuple(values)


def apply_is_scoped(cfg: ReconcileConfig) -> bool:
    return bool(cfg.categories) or bool(cfg.service_ids)


def select_apply_rows(
    rows: Sequence[PlanRow],
    *,
    include_over_limit: bool,
    apply_usernames: Sequence[str] = (),
) -> List[PlanRow]:
    allow = set(apply_usernames) if apply_usernames else None
    selected: List[PlanRow] = []
    for row in rows:
        if allow is not None and row.username not in allow:
            continue
        if row.classification not in APPLY_CLASSES:
            continue
        if row.would_be_over_limit_now and not include_over_limit:
            continue
        selected.append(row)
    return selected


def skipped_over_limit_rows(
    rows: Sequence[PlanRow], *, apply_usernames: Sequence[str] = ()
) -> List[PlanRow]:
    allow = set(apply_usernames) if apply_usernames else None
    return [
        row
        for row in rows
        if (allow is None or row.username in allow)
        and row.classification in APPLY_CLASSES
        and row.would_be_over_limit_now
    ]


def allowlist_stats(
    rows: Sequence[PlanRow],
    applied: Sequence[Dict[str, Any]],
    apply_usernames: Sequence[str],
    *,
    include_over_limit: bool,
) -> Dict[str, Any]:
    requested = list(apply_usernames)
    if not requested:
        return {
            "enabled": False,
            "requested": 0,
            "matched": 0,
            "applied": 0,
            "skipped_nonmutation": 0,
            "not_found": 0,
            "skipped_over_limit": 0,
        }
    by_name = {row.username: row for row in rows}
    matched = [name for name in requested if name in by_name]
    applied_names = {item.get("username") for item in applied}
    skipped_nonmutation = 0
    skipped_over = 0
    for name in matched:
        row = by_name[name]
        if row.classification not in APPLY_CLASSES:
            skipped_nonmutation += 1
        elif row.would_be_over_limit_now and not include_over_limit:
            skipped_over += 1
    return {
        "enabled": True,
        "requested": len(requested),
        "matched": len(matched),
        "applied": sum(1 for name in requested if name in applied_names),
        "skipped_nonmutation": skipped_nonmutation,
        "not_found": len(requested) - len(matched),
        "skipped_over_limit": skipped_over,
        "usernames": list(requested),
    }


def precheck_apply_row(
    row: PlanRow,
    *,
    live: Optional[RemnawaveTrafficState],
    fetch_kind: str,
    fetch_error: Optional[str],
    apply_usernames: Sequence[str] = (),
    include_over_limit: bool = False,
) -> Tuple[str, Optional[str], Optional[str]]:
    """Return (action, skip_reason_or_none, error_or_none).

    action: patch | skip | error
    Used-traffic growth vs the plan value is allowed. A decrease is error.
    Over-limit and already_correct are decided from fresh live state.
    """
    if apply_usernames and row.username not in set(apply_usernames):
        return (
            ACTION_ERROR,
            None,
            "refusing PATCH: username not in --apply-username allow-list "
            f"({row.username})",
        )
    if row.target_traffic_limit_bytes is TARGET_UNRESOLVED:
        return ACTION_ERROR, None, "refusing PATCH: target unresolved"
    if row.target_traffic_limit_strategy is TARGET_UNRESOLVED:
        return ACTION_ERROR, None, "refusing PATCH: target strategy unresolved"
    if fetch_kind != "ok" or live is None:
        return (
            ACTION_ERROR,
            None,
            f"refusing PATCH: live fetch {fetch_kind}"
            + (f" ({fetch_error})" if fetch_error else ""),
        )
    if live.numeric_id != row.remnawave_user_id:
        return (
            ACTION_ERROR,
            None,
            "refusing PATCH: remnawave user id drift "
            f"(plan={row.remnawave_user_id!r} live={live.numeric_id!r})",
        )
    used_err = used_bytes_safe_vs_baseline(
        row.used_traffic_bytes, live.used_traffic_bytes, label="precheck"
    )
    if used_err:
        return ACTION_ERROR, None, used_err
    try:
        target_bytes = parse_shm_traffic_limit_bytes(row.target_traffic_limit_bytes)
        target_strategy = parse_shm_traffic_limit_strategy(
            row.target_traffic_limit_strategy
        )
    except TrafficSettingError as exc:
        return (
            ACTION_ERROR,
            None,
            f"refusing PATCH: target re-check failed ({exc})",
        )
    if (
        target_bytes != row.target_traffic_limit_bytes
        or target_strategy != row.target_traffic_limit_strategy
    ):
        return ACTION_ERROR, None, "refusing PATCH: target re-check mismatch"
    live_class = classify_traffic(
        target_bytes=target_bytes,
        target_strategy=target_strategy,
        current_bytes=live.traffic_limit_bytes,
        current_strategy=live.traffic_limit_strategy,
    )
    if live_class == CLASS_ALREADY_CORRECT:
        return ACTION_SKIP, SKIP_ALREADY_CORRECT, None
    if live_class not in APPLY_CLASSES:
        return (
            ACTION_ERROR,
            None,
            f"refusing PATCH: fresh classification is {live_class}",
        )
    if over_limit_now(
        target_bytes=target_bytes, used_bytes=live.used_traffic_bytes
    ) and not include_over_limit:
        return ACTION_SKIP, SKIP_OVER_LIMIT, None
    return ACTION_PATCH, None, None


def write_apply_summary(
    output_dir: str,
    rows: Sequence[PlanRow],
    applied: Sequence[Dict[str, Any]],
    errors: Sequence[Dict[str, Any]],
    *,
    requested: int,
    skipped_over_limit: int,
    remnawave_schema: Optional[Dict[str, str]] = None,
    apply_usernames: Sequence[str] = (),
    include_over_limit: bool = False,
    skipped_already_correct: int = 0,
    skipped_over_limit_live: int = 0,
) -> Dict[str, Any]:
    summary = summarize(rows, remnawave_schema=remnawave_schema)
    allow = allowlist_stats(
        rows,
        applied,
        apply_usernames,
        include_over_limit=include_over_limit,
    )
    settled = len(applied) + skipped_over_limit_live + skipped_already_correct
    summary["apply"] = {
        "requested": requested,
        "applied": len(applied),
        "failed": len(errors),
        "skipped_over_limit": skipped_over_limit,
        "skipped_already_correct": skipped_already_correct,
        "complete": len(errors) == 0 and settled == requested,
        "allowlist": allow,
    }
    atomic_write_json(os.path.join(output_dir, "summary.json"), summary)
    return summary


def apply_updates(
    client: HttpClient,
    cfg: ReconcileConfig,
    rows: Sequence[PlanRow],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    if not apply_is_scoped(cfg):
        raise FatalError(
            "apply refused: require at least one --category or --service-id"
        )
    needs = select_apply_rows(
        rows,
        include_over_limit=cfg.include_over_limit,
        apply_usernames=cfg.apply_usernames,
    )
    applied: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    url = cfg.remnawave_panel_url.rstrip("/") + "/api/users"

    for row in needs:
        if RESET_TRAFFIC_MARKER in url:
            raise FatalError("internal error: reset-traffic URL must not be used")
        if not row.remnawave_user_id:
            errors.append(
                {
                    "stage": "precheck",
                    "username": row.username,
                    "user_service_id": row.user_service_id,
                    "error": "missing remnawave_user_id",
                }
            )
            return applied, errors, skipped

        kind, live, fetch_error = fetch_remnawave_user(client, cfg, row.username)
        action, skip_reason, precheck_error = precheck_apply_row(
            row,
            live=live,
            fetch_kind=kind,
            fetch_error=fetch_error,
            apply_usernames=cfg.apply_usernames,
            include_over_limit=cfg.include_over_limit,
        )
        if action == ACTION_ERROR:
            errors.append(
                {
                    "stage": "precheck",
                    "username": row.username,
                    "user_service_id": row.user_service_id,
                    "user_id": row.remnawave_user_id,
                    "error": precheck_error or "precheck failed",
                    "plan_used_traffic_bytes": row.used_traffic_bytes,
                    "prepatch_used_traffic_bytes": (
                        live.used_traffic_bytes if live else None
                    ),
                }
            )
            return applied, errors, skipped
        if action == ACTION_SKIP:
            skipped.append(
                {
                    "username": row.username,
                    "user_service_id": row.user_service_id,
                    "reason": skip_reason,
                    "plan_used_traffic_bytes": row.used_traffic_bytes,
                    "prepatch_used_traffic_bytes": (
                        live.used_traffic_bytes if live else None
                    ),
                }
            )
            continue
        assert live is not None

        target_bytes = parse_shm_traffic_limit_bytes(row.target_traffic_limit_bytes)
        target_strategy = parse_shm_traffic_limit_strategy(
            row.target_traffic_limit_strategy
        )

        body = encode_traffic_patch_body(
            int(row.remnawave_user_id), target_bytes, target_strategy
        )
        decoded = json.loads(body.decode("utf-8"))
        if set(decoded.keys()) != PATCH_KEYS:
            raise FatalError("internal error: PATCH payload keys drifted")
        if RESET_TRAFFIC_MARKER in url:
            raise FatalError("internal error: reset-traffic URL must not be used")

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
                    "target_traffic_limit_bytes": target_bytes,
                    "target_traffic_limit_strategy": target_strategy,
                    "error": str(exc),
                }
            )
            return applied, errors, skipped

        if status >= 400:
            errors.append(
                {
                    "stage": "patch",
                    "username": row.username,
                    "user_id": row.remnawave_user_id,
                    "target_traffic_limit_bytes": target_bytes,
                    "target_traffic_limit_strategy": target_strategy,
                    "error": f"HTTP {status}",
                }
            )
            return applied, errors, skipped

        verify_error, verified = verify_traffic(
            client,
            cfg,
            username=row.username,
            expected_id=int(row.remnawave_user_id),
            expected_bytes=target_bytes,
            expected_strategy=target_strategy,
            pre_used=live.used_traffic_bytes,
            pre_status=live.status,
            pre=live,
            patch_payload=payload,
        )
        if verify_error:
            errors.append(
                {
                    "stage": "verify",
                    "username": row.username,
                    "user_id": row.remnawave_user_id,
                    "target_traffic_limit_bytes": target_bytes,
                    "target_traffic_limit_strategy": target_strategy,
                    "error": verify_error,
                    "plan_used_traffic_bytes": row.used_traffic_bytes,
                    "prepatch_used_traffic_bytes": live.used_traffic_bytes,
                    "postpatch_used_traffic_bytes": (
                        verified.used_traffic_bytes if verified else None
                    ),
                }
            )
            return applied, errors, skipped

        post_used = (
            verified.used_traffic_bytes if verified is not None else live.used_traffic_bytes
        )
        growth = None
        if (
            isinstance(row.used_traffic_bytes, int)
            and not isinstance(row.used_traffic_bytes, bool)
            and isinstance(post_used, int)
            and not isinstance(post_used, bool)
        ):
            growth = post_used - row.used_traffic_bytes
        applied.append(
            {
                "user_service_id": row.user_service_id,
                "username": row.username,
                "remnawave_user_id": row.remnawave_user_id,
                "target_traffic_limit_bytes": target_bytes,
                "target_traffic_limit_strategy": target_strategy,
                "plan_used_traffic_bytes": row.used_traffic_bytes,
                "prepatch_used_traffic_bytes": live.used_traffic_bytes,
                "postpatch_used_traffic_bytes": post_used,
                "traffic_growth_during_apply_bytes": growth,
                "status": live.status,
                "classification": row.classification,
            }
        )

    return applied, errors, skipped


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile Remnawave trafficLimitBytes/Strategy from SHM "
            "(dry-run by default)"
        )
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
    parser.add_argument(
        "--service-id",
        action="append",
        default=[],
        dest="service_ids",
        help="Limit to this SHM service_id (repeatable)",
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
            "Username allowed to mutate on --apply (repeatable). Extra "
            "constraint: does not replace --category/--service-id scope, "
            "and the user must still be in a mutation class."
        ),
    )
    parser.add_argument(
        "--include-over-limit",
        action="store_true",
        help=(
            "Allow PATCH for users whose used traffic is already at/over "
            "the target limit (default: skip)"
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
    service_ids = parse_repeatable_ids(args.service_ids or (), flag="--service-id")
    categories = tuple(args.category or ())
    apply_usernames = parse_apply_usernames(args.apply_usernames or ())
    if bool(args.apply) and not categories and not service_ids:
        raise FatalError(
            "apply refused: require at least one --category or --service-id"
        )
    return ReconcileConfig(
        shm_base_url=args.shm_base_url.rstrip("/"),
        shm_login=args.shm_login,
        shm_password=shm_password,
        remnawave_panel_url=args.remnawave_panel_url.rstrip("/"),
        remnawave_token=remnawave_token,
        output=args.output,
        categories=categories,
        service_ids=service_ids,
        page_size=args.page_size,
        request_delay_ms=args.request_delay_ms,
        apply=bool(args.apply),
        confirm=args.confirm,
        apply_usernames=apply_usernames,
        include_over_limit=bool(args.include_over_limit),
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
            if not apply_is_scoped(cfg):
                raise FatalError(
                    "apply refused: require at least one --category or --service-id"
                )

        log("Authenticating to SHM...")
        session_id = shm_authenticate(http, cfg)
        log("Loading SHM service catalog...")
        catalog = load_service_catalog(http, cfg, session_id)
        log(
            "Service catalog entries: "
            f"{len({k for k in catalog if not isinstance(k, str)})}"
        )

        log("Building traffic-limit reconciliation plan...")
        rows, schema = build_plan(http, cfg, session_id, catalog)
        summary = write_reports(cfg.output, rows, remnawave_schema=schema)
        plan_counts = summary["plan_counts"]
        log(
            "Plan written: "
            f"total={summary['total_user_services_inspected']} "
            f"managed_users={summary['managed_users']} "
            f"already_correct={plan_counts[CLASS_ALREADY_CORRECT]} "
            f"needs_set_limit={plan_counts[CLASS_NEEDS_LIMIT]} "
            f"needs_set_strategy={plan_counts[CLASS_NEEDS_STRATEGY]} "
            f"needs_set_limit_and_strategy={plan_counts[CLASS_NEEDS_BOTH]} "
            f"unmanaged_service={plan_counts[CLASS_UNMANAGED]} "
            f"missing={plan_counts[CLASS_MISSING]} "
            f"invalid={plan_counts[CLASS_INVALID]} "
            f"error={plan_counts[CLASS_ERROR]}"
        )
        log(
            "Risk: would_be_over_limit_now="
            f"{summary['risk']['would_be_over_limit_now_count']}"
        )
        for combo in summary["target_combinations"]:
            log(
                "Target combo: "
                f"{combo['target_traffic_limit_bytes']} "
                f"{combo['target_traffic_limit_strategy']} "
                f"users={combo['users_count']}"
            )

        if not cfg.apply:
            log("Dry-run complete (no Remnawave changes).")
            return 0

        plan_over = skipped_over_limit_rows(
            rows, apply_usernames=cfg.apply_usernames
        )
        if plan_over and not cfg.include_over_limit:
            log(
                f"Skipping {len(plan_over)} plan-time over-limit user(s) "
                "(pass --include-over-limit to apply)."
            )

        allow_note = (
            f" allow-list={len(cfg.apply_usernames)}"
            if cfg.apply_usernames
            else ""
        )
        log(
            "Applying traffic-limit updates via sequential PATCH /api/users "
            f"(traffic fields only; no reset-traffic;{allow_note})..."
        )
        requested = len(
            select_apply_rows(
                rows,
                include_over_limit=cfg.include_over_limit,
                apply_usernames=cfg.apply_usernames,
            )
        )
        applied, errors, skipped_live = apply_updates(http, cfg, rows)
        atomic_write_json(os.path.join(cfg.output, "applied.json"), applied)
        if errors:
            atomic_write_json(os.path.join(cfg.output, "errors.json"), errors)
        if skipped_live:
            atomic_write_json(os.path.join(cfg.output, "skipped.json"), skipped_live)
        live_over = sum(
            1 for item in skipped_live if item.get("reason") == SKIP_OVER_LIMIT
        )
        live_correct = sum(
            1 for item in skipped_live if item.get("reason") == SKIP_ALREADY_CORRECT
        )
        apply_summary = write_apply_summary(
            cfg.output,
            rows,
            applied,
            errors,
            requested=requested,
            skipped_over_limit=(
                (0 if cfg.include_over_limit else len(plan_over)) + live_over
            ),
            remnawave_schema=schema,
            apply_usernames=cfg.apply_usernames,
            include_over_limit=cfg.include_over_limit,
            skipped_already_correct=live_correct,
            skipped_over_limit_live=live_over,
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
