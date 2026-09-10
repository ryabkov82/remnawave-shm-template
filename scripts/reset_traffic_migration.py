#!/usr/bin/env python3
"""One-time reset of accumulated traffic after Standard MONTH migration.

This tool never changes SHM service settings and never writes Remnawave
user fields (no PATCH). It only plans, and on explicit apply POSTs
``/api/users/{id}/actions/reset-traffic``.

Eligibility requires live SHM and live Remnawave to already be:

  traffic_limit_bytes / trafficLimitBytes = 322122547200
  traffic_limit_strategy / trafficLimitStrategy = MONTH

plus createdAt < --migration-cutoff and no reset since that cutoff.

Dry-run is the default. ``potential_after_month_migration`` is
informational only and never authorizes apply.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import signal
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
    load_service_catalog,
    parse_apply_usernames,
    resolve_catalog_service,
)
from reconcile_traffic_limits import (
    GIB,
    TrafficSettingError,
    extract_traffic_settings,
    normalize_current_bytes,
    normalize_current_strategy,
    normalize_used_bytes,
    parse_managed_shm_target,
    parse_remnawave_user,
    parse_repeatable_ids,
    service_name_from_service,
    squad_fingerprint,
    _user_sources,
)


CONFIRM_PHRASE = "RESET_MONTHLY_TRAFFIC_MIGRATION"
HTTP_TIMEOUT_SEC = 30
VERIFY_RETRY_ATTEMPTS = 5
VERIFY_RETRY_DELAY_SEC = 1.0

EXPECTED_LIMIT_BYTES = 322122547200
EXPECTED_STRATEGY = "MONTH"
ELIGIBLE_STATUSES = frozenset({"ACTIVE", "DISABLED"})
USED_DECREASE_THRESHOLD_BYTES = 10 * 1024 * 1024


def status_is_reset_eligible(status: Optional[str]) -> bool:
    return status in ELIGIBLE_STATUSES

CLASS_ELIGIBLE = "eligible_for_reset"
CLASS_ALREADY_RESET = "already_reset_since_cutoff"
CLASS_CREATED_AFTER = "created_after_cutoff"
CLASS_SERVICE_NOT_MONTH = "service_not_month"
CLASS_SERVICE_WRONG_LIMIT = "service_wrong_limit"
CLASS_REMNA_NOT_MONTH = "remna_not_month"
CLASS_REMNA_WRONG_LIMIT = "remna_wrong_limit"
CLASS_MISSING = "missing_in_remnawave"
CLASS_INACTIVE = "inactive_or_unexpected_status"
CLASS_INVALID = "invalid_shm_setting"
CLASS_ERROR = "error"

ALL_CLASSES = (
    CLASS_ELIGIBLE,
    CLASS_ALREADY_RESET,
    CLASS_CREATED_AFTER,
    CLASS_SERVICE_NOT_MONTH,
    CLASS_SERVICE_WRONG_LIMIT,
    CLASS_REMNA_NOT_MONTH,
    CLASS_REMNA_WRONG_LIMIT,
    CLASS_MISSING,
    CLASS_INACTIVE,
    CLASS_INVALID,
    CLASS_ERROR,
)

ACTION_RESET = "reset"
ACTION_SKIP = "skip"
ACTION_ERROR = "error"

REPORT_FIELDS = (
    "user_service_id",
    "username",
    "service_id",
    "service_name",
    "category",
    "shm_traffic_limit_bytes",
    "shm_traffic_limit_strategy",
    "remna_id",
    "remna_created_at",
    "remna_last_traffic_reset_at",
    "remna_used_traffic_bytes",
    "remna_used_traffic_gib",
    "remna_traffic_limit_bytes",
    "remna_traffic_limit_strategy",
    "remna_status",
    "migration_cutoff",
    "classification",
    "eligible_for_reset",
    "potential_after_month_migration",
    "error_message",
)


@dataclass
class ResetConfig:
    shm_base_url: str
    shm_login: str
    shm_password: str
    remnawave_panel_url: str
    remnawave_token: str
    output: str
    migration_cutoff_raw: str
    migration_cutoff: datetime
    categories: Tuple[str, ...] = ()
    service_ids: Tuple[str, ...] = ()
    page_size: int = 250
    request_delay_ms: int = 50
    apply: bool = False
    confirm: Optional[str] = None
    apply_usernames: Tuple[str, ...] = ()
    http_timeout: float = HTTP_TIMEOUT_SEC
    verify_retry_attempts: int = VERIFY_RETRY_ATTEMPTS
    verify_retry_delay_sec: float = VERIFY_RETRY_DELAY_SEC


@dataclass
class ResetUserState:
    numeric_id: Optional[int]
    traffic_limit_bytes: Any
    traffic_limit_strategy: Optional[str]
    status: Optional[str]
    used_traffic_bytes: Optional[int]
    created_at: Optional[str]
    last_traffic_reset_at: Optional[str]
    expire_at: Optional[str] = None
    hwid_device_limit: Any = None
    external_squad_uuid: Optional[str] = None
    active_internal_squads: Any = None
    field_paths: Dict[str, str] = field(default_factory=dict)


@dataclass
class PlanRow:
    user_service_id: Any
    username: str
    service_id: Any
    service_name: Optional[str]
    category: Optional[str]
    shm_traffic_limit_bytes: Any
    shm_traffic_limit_strategy: Any
    remna_id: Any
    remna_created_at: Optional[str]
    remna_last_traffic_reset_at: Optional[str]
    remna_used_traffic_bytes: Optional[int]
    remna_traffic_limit_bytes: Any
    remna_traffic_limit_strategy: Optional[str]
    remna_status: Optional[str]
    migration_cutoff: str
    classification: str
    eligible_for_reset: bool
    potential_after_month_migration: bool = False
    error_message: Optional[str] = None

    def to_report_dict(self) -> Dict[str, Any]:
        return {
            "user_service_id": self.user_service_id,
            "username": self.username,
            "service_id": self.service_id,
            "service_name": self.service_name,
            "category": self.category,
            "shm_traffic_limit_bytes": self.shm_traffic_limit_bytes,
            "shm_traffic_limit_strategy": self.shm_traffic_limit_strategy,
            "remna_id": self.remna_id,
            "remna_created_at": self.remna_created_at,
            "remna_last_traffic_reset_at": self.remna_last_traffic_reset_at,
            "remna_used_traffic_bytes": self.remna_used_traffic_bytes,
            "remna_used_traffic_gib": bytes_to_gib(self.remna_used_traffic_bytes),
            "remna_traffic_limit_bytes": self.remna_traffic_limit_bytes,
            "remna_traffic_limit_strategy": self.remna_traffic_limit_strategy,
            "remna_status": self.remna_status,
            "migration_cutoff": self.migration_cutoff,
            "classification": self.classification,
            "eligible_for_reset": self.eligible_for_reset,
            "potential_after_month_migration": self.potential_after_month_migration,
            "error_message": self.error_message,
        }


class MutationGuard:
    """Refuse every Remnawave write except POST reset-traffic."""

    def __init__(self, client: HttpClient) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[bytes] = None,
        expect_json: bool = True,
    ) -> Tuple[int, Any, Dict[str, str]]:
        upper = method.upper()
        if upper == "PATCH":
            raise FatalError("internal error: PATCH must not be used")
        if upper in {"PUT", "DELETE"}:
            raise FatalError(f"internal error: {upper} must not be used")
        if upper == "POST" and not _post_url_allowed(url):
            raise FatalError(
                "internal error: POST is only allowed for SHM auth or reset-traffic"
            )
        return self._client.request(
            method,
            url,
            headers=headers,
            body=body,
            expect_json=expect_json,
        )


def _post_url_allowed(url: str) -> bool:
    path = urllib.parse.urlparse(url).path
    return path.endswith("/shm/user/auth.cgi") or path.endswith(
        "/actions/reset-traffic"
    )


def bytes_to_gib(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool) or not isinstance(value, int):
        return None
    return value / GIB


def cutoff_iso(cfg: ResetConfig) -> str:
    return cfg.migration_cutoff_raw


def parse_migration_cutoff(raw: str) -> datetime:
    if raw is None or not str(raw).strip():
        raise FatalError("--migration-cutoff is required (ISO8601 UTC)")
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise FatalError(
            f"invalid --migration-cutoff {raw!r}: expected ISO8601 UTC"
        ) from exc
    if parsed.tzinfo is None:
        raise FatalError("--migration-cutoff must include a UTC timezone (Z)")
    return parsed.astimezone(timezone.utc)


def parse_optional_timestamp(raw: Optional[str], *, field_name: str) -> Optional[datetime]:
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise ValueError(f"invalid {field_name}: {raw!r}")
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid {field_name}: {raw!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone: {raw!r}")
    return parsed.astimezone(timezone.utc)


def extract_created_at(payload: Any) -> Optional[str]:
    for _prefix, src in _user_sources(payload):
        value = src.get("createdAt")
        if isinstance(value, str) and value:
            return value
    return None


def apply_is_scoped(cfg: ResetConfig) -> bool:
    return bool(cfg.categories) or bool(cfg.service_ids)


def _wanted_service_ids(cfg: ResetConfig) -> set:
    return set(cfg.service_ids)


def _service_id_wanted(service_id: Any, wanted: set) -> bool:
    if not wanted:
        return True
    if service_id is None or service_id == "":
        return False
    return str(service_id) in wanted


def snapshot_identity(state: ResetUserState) -> Dict[str, Any]:
    return {
        "traffic_limit_bytes": state.traffic_limit_bytes,
        "traffic_limit_strategy": state.traffic_limit_strategy,
        "status": state.status,
        "expire_at": state.expire_at,
        "hwid_device_limit": state.hwid_device_limit,
        "external_squad_uuid": state.external_squad_uuid,
        "active_internal_squads": squad_fingerprint(state.active_internal_squads),
    }


def parse_reset_user(payload: Any) -> ResetUserState:
    traffic = parse_remnawave_user(payload)
    created_at = extract_created_at(payload)
    return ResetUserState(
        numeric_id=traffic.numeric_id,
        traffic_limit_bytes=traffic.traffic_limit_bytes,
        traffic_limit_strategy=traffic.traffic_limit_strategy,
        status=traffic.status,
        used_traffic_bytes=traffic.used_traffic_bytes,
        created_at=created_at,
        last_traffic_reset_at=traffic.last_traffic_reset_at,
        expire_at=traffic.expire_at,
        hwid_device_limit=traffic.hwid_device_limit,
        external_squad_uuid=traffic.external_squad_uuid,
        active_internal_squads=traffic.active_internal_squads,
        field_paths=dict(traffic.field_paths),
    )


def _normalize_reset_user(state: ResetUserState) -> ResetUserState:
    state.traffic_limit_bytes = normalize_current_bytes(state.traffic_limit_bytes)
    state.traffic_limit_strategy = normalize_current_strategy(
        state.traffic_limit_strategy
    )
    state.used_traffic_bytes = normalize_used_bytes(state.used_traffic_bytes)
    return state


def fetch_remnawave_reset_user(
    client: HttpClient,
    cfg: ResetConfig,
    username: str,
) -> Tuple[str, Optional[ResetUserState], Optional[str]]:
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
    state = parse_reset_user(payload)
    if state.numeric_id is None:
        return "error", None, "numeric user id missing in response"
    try:
        state = _normalize_reset_user(state)
    except (TrafficSettingError, ValueError) as exc:
        return "error", state, str(exc)
    return "ok", state, None


def reset_user_traffic(client: HttpClient, cfg: ResetConfig, user_id: int) -> None:
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise FatalError(f"invalid Remnawave user id for reset: {user_id!r}")
    url = (
        cfg.remnawave_panel_url.rstrip("/")
        + f"/api/users/{user_id}/actions/reset-traffic"
    )
    path = urllib.parse.urlparse(url).path
    if not path.endswith("/actions/reset-traffic"):
        raise FatalError("internal error: reset-traffic URL is required")
    status, _payload, _ = client.request(
        "POST",
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {cfg.remnawave_token}",
            "Content-Type": "application/json",
        },
        body=b"{}",
    )
    if status >= 400:
        raise FatalError(f"reset-traffic failed with HTTP {status}")


def _row(
    *,
    user_service_id: Any,
    username: str,
    service_id: Any,
    service_name: Optional[str],
    category: Optional[str],
    cutoff: str,
    classification: str,
    shm_bytes: Any = None,
    shm_strategy: Any = None,
    remna: Optional[ResetUserState] = None,
    potential: bool = False,
    error_message: Optional[str] = None,
) -> PlanRow:
    return PlanRow(
        user_service_id=user_service_id,
        username=username,
        service_id=service_id,
        service_name=service_name,
        category=category,
        shm_traffic_limit_bytes=shm_bytes,
        shm_traffic_limit_strategy=shm_strategy,
        remna_id=remna.numeric_id if remna else None,
        remna_created_at=remna.created_at if remna else None,
        remna_last_traffic_reset_at=remna.last_traffic_reset_at if remna else None,
        remna_used_traffic_bytes=remna.used_traffic_bytes if remna else None,
        remna_traffic_limit_bytes=remna.traffic_limit_bytes if remna else None,
        remna_traffic_limit_strategy=remna.traffic_limit_strategy if remna else None,
        remna_status=remna.status if remna else None,
        migration_cutoff=cutoff,
        classification=classification,
        eligible_for_reset=classification == CLASS_ELIGIBLE,
        potential_after_month_migration=potential,
        error_message=error_message,
    )


def potential_after_month_migration(
    *,
    shm_bytes: Any,
    remna: Optional[ResetUserState],
    cutoff: datetime,
    classification: str,
) -> bool:
    """Informational: would be eligible if both strategies were already MONTH.

    Never authorizes apply.
    """
    if classification == CLASS_ELIGIBLE:
        return False
    if remna is None or remna.numeric_id is None:
        return False
    if shm_bytes != EXPECTED_LIMIT_BYTES:
        return False
    if remna.traffic_limit_bytes != EXPECTED_LIMIT_BYTES:
        return False
    if not status_is_reset_eligible(remna.status):
        return False
    try:
        created = parse_optional_timestamp(remna.created_at, field_name="createdAt")
        last_reset = parse_optional_timestamp(
            remna.last_traffic_reset_at, field_name="lastTrafficResetAt"
        )
    except ValueError:
        return False
    if created is None or created >= cutoff:
        return False
    if last_reset is not None and last_reset >= cutoff:
        return False
    return True


def classify_reset_row(
    *,
    user_service_id: Any,
    username: str,
    service_id: Any,
    service_name: Optional[str],
    category: Optional[str],
    cutoff: datetime,
    cutoff_raw: str,
    extract: Any,
    fetch_kind: str,
    remna: Optional[ResetUserState],
    error_message: Optional[str],
) -> PlanRow:
    common = dict(
        user_service_id=user_service_id,
        username=username,
        service_id=service_id,
        service_name=service_name,
        category=category,
        cutoff=cutoff_raw,
        remna=remna,
    )

    if not extract.bytes_explicit:
        return _row(
            **common,
            classification=CLASS_INVALID,
            error_message="traffic_limit_bytes is missing; live MONTH policy not confirmed",
        )

    try:
        shm_bytes, shm_strategy = parse_managed_shm_target(extract)
    except TrafficSettingError as exc:
        return _row(
            **common,
            classification=CLASS_INVALID,
            shm_bytes=extract.bytes_raw,
            shm_strategy=extract.strategy_raw,
            error_message=str(exc),
        )

    common["shm_bytes"] = shm_bytes
    common["shm_strategy"] = shm_strategy

    if shm_bytes != EXPECTED_LIMIT_BYTES:
        return _row(**common, classification=CLASS_SERVICE_WRONG_LIMIT)
    if shm_strategy != EXPECTED_STRATEGY:
        return _row(
            **common,
            classification=CLASS_SERVICE_NOT_MONTH,
            potential=potential_after_month_migration(
                shm_bytes=shm_bytes,
                remna=remna,
                cutoff=cutoff,
                classification=CLASS_SERVICE_NOT_MONTH,
            ),
        )

    if fetch_kind == "missing":
        return _row(**common, classification=CLASS_MISSING)
    if fetch_kind == "error" or remna is None:
        return _row(
            **common,
            classification=CLASS_ERROR,
            error_message=error_message or "unexpected remnawave error",
        )

    if remna.traffic_limit_bytes != EXPECTED_LIMIT_BYTES:
        return _row(**common, classification=CLASS_REMNA_WRONG_LIMIT)
    if remna.traffic_limit_strategy != EXPECTED_STRATEGY:
        return _row(
            **common,
            classification=CLASS_REMNA_NOT_MONTH,
            potential=potential_after_month_migration(
                shm_bytes=shm_bytes,
                remna=remna,
                cutoff=cutoff,
                classification=CLASS_REMNA_NOT_MONTH,
            ),
        )
    if not status_is_reset_eligible(remna.status):
        return _row(**common, classification=CLASS_INACTIVE)

    try:
        created = parse_optional_timestamp(remna.created_at, field_name="createdAt")
        last_reset = parse_optional_timestamp(
            remna.last_traffic_reset_at, field_name="lastTrafficResetAt"
        )
    except ValueError as exc:
        return _row(**common, classification=CLASS_ERROR, error_message=str(exc))

    if created is None:
        return _row(
            **common,
            classification=CLASS_ERROR,
            error_message="createdAt missing; cannot apply migration cutoff",
        )
    if created >= cutoff:
        return _row(**common, classification=CLASS_CREATED_AFTER)
    if last_reset is not None and last_reset >= cutoff:
        return _row(**common, classification=CLASS_ALREADY_RESET)
    return _row(**common, classification=CLASS_ELIGIBLE)


def build_plan(
    client: HttpClient,
    cfg: ResetConfig,
    session_id: str,
    catalog: Dict[Any, Dict[str, Any]],
) -> List[PlanRow]:
    wanted_categories = set(cfg.categories)
    wanted_services = _wanted_service_ids(cfg)
    rows: List[PlanRow] = []

    for item in iter_shm_user_services(client, cfg, session_id):
        category = item.get("category")
        if wanted_categories and category not in wanted_categories:
            continue
        service_id = item.get("service_id")
        if not _service_id_wanted(service_id, wanted_services):
            continue
        user_service_id = item.get("user_service_id")
        category_s = str(category) if category is not None else None
        if user_service_id is None or user_service_id == "":
            rows.append(
                _row(
                    user_service_id=user_service_id,
                    username="",
                    service_id=service_id,
                    service_name=None,
                    category=category_s,
                    cutoff=cutoff_iso(cfg),
                    classification=CLASS_ERROR,
                    error_message="missing user_service_id",
                )
            )
            continue

        username = username_for_user_service(user_service_id)
        service, service_error = resolve_catalog_service(item, catalog)
        if service_error:
            rows.append(
                _row(
                    user_service_id=user_service_id,
                    username=username,
                    service_id=service_id,
                    service_name=None,
                    category=category_s,
                    cutoff=cutoff_iso(cfg),
                    classification=CLASS_ERROR,
                    error_message=service_error,
                )
            )
            continue

        extract = extract_traffic_settings(service)
        kind, state, err = fetch_remnawave_reset_user(client, cfg, username)
        rows.append(
            classify_reset_row(
                user_service_id=user_service_id,
                username=username,
                service_id=service_id,
                service_name=service_name_from_service(service),
                category=category_s,
                cutoff=cfg.migration_cutoff,
                cutoff_raw=cutoff_iso(cfg),
                extract=extract,
                fetch_kind=kind,
                remna=state,
                error_message=err,
            )
        )
    return rows


def select_apply_rows(
    rows: Sequence[PlanRow],
    apply_usernames: Sequence[str] = (),
) -> List[PlanRow]:
    allow = set(apply_usernames) if apply_usernames else None
    selected: List[PlanRow] = []
    for row in rows:
        if row.classification != CLASS_ELIGIBLE:
            continue
        if allow is not None and row.username not in allow:
            continue
        selected.append(row)
    return selected


def _percentile(sorted_values: Sequence[int], pct: float) -> Optional[float]:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (len(sorted_values) - 1) * (pct / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return float(sorted_values[low])
    frac = rank - low
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * frac


def _used_int_values(rows: Sequence[PlanRow]) -> List[int]:
    values = [
        row.remna_used_traffic_bytes
        for row in rows
        if isinstance(row.remna_used_traffic_bytes, int)
        and not isinstance(row.remna_used_traffic_bytes, bool)
    ]
    values.sort()
    return values


def used_traffic_stats(rows: Sequence[PlanRow]) -> Dict[str, Any]:
    used_values = _used_int_values(rows)
    return {
        "count": len(used_values),
        "sum_gib": (sum(used_values) / GIB) if used_values else 0.0,
        "p50_gib": (
            bytes_to_gib(int(round(_percentile(used_values, 50) or 0)))
            if used_values
            else None
        ),
        "p90_gib": (
            bytes_to_gib(int(round(_percentile(used_values, 90) or 0)))
            if used_values
            else None
        ),
        "max_gib": bytes_to_gib(used_values[-1]) if used_values else None,
        "used_ge_250_gib": sum(1 for value in used_values if value >= 250 * GIB),
        "used_ge_300_gib": sum(1 for value in used_values if value >= 300 * GIB),
    }


def used_bucket(used: Optional[int]) -> Optional[str]:
    if used is None:
        return None
    if used == 0:
        return "0"
    ten = 10 * GIB
    hundred = 100 * GIB
    two_hundred = 200 * GIB
    two_fifty = 250 * GIB
    three_hundred = 300 * GIB
    if used < ten:
        return "0-10 GiB"
    if used < hundred:
        return "10-100 GiB"
    if used < two_hundred:
        return "100-200 GiB"
    if used < two_fifty:
        return "200-250 GiB"
    if used < three_hundred:
        return "250-300 GiB"
    return ">=300 GiB"


def planned_strategy_impact(rows: Sequence[PlanRow]) -> Dict[str, Any]:
    """Informational only. Does not rewrite live SHM targets or authorize apply."""
    found = 0
    no_reset = 0
    already_month = 0
    other = 0
    missing = 0
    other_values: Dict[str, int] = {}
    for row in rows:
        if row.remna_id is None:
            missing += 1
            continue
        found += 1
        strategy = row.remna_traffic_limit_strategy
        if strategy == "NO_RESET":
            no_reset += 1
        elif strategy == "MONTH":
            already_month += 1
        else:
            other += 1
            key = strategy if strategy else "null"
            other_values[key] = other_values.get(key, 0) + 1
    return {
        "note": (
            "informational only; live SHM target was not rewritten; "
            "this field never authorizes reset or PATCH"
        ),
        "assumption": (
            "if Standard service strategy becomes MONTH and "
            "reconcile_traffic_limits PATCHes strategy only"
        ),
        "found_in_remnawave": found,
        "would_need_set_strategy_from_NO_RESET": no_reset,
        "already_MONTH": already_month,
        "other_strategy": other,
        "other_strategy_values": other_values,
        "missing": missing,
    }


def summarize(rows: Sequence[PlanRow], cfg: ResetConfig) -> Dict[str, Any]:
    counts = {name: 0 for name in ALL_CLASSES}
    for row in rows:
        counts[row.classification] = counts.get(row.classification, 0) + 1

    eligible = [r for r in rows if r.classification == CLASS_ELIGIBLE]
    eligible_active = [r for r in eligible if r.remna_status == "ACTIVE"]
    eligible_disabled = [r for r in eligible if r.remna_status == "DISABLED"]
    used_values = _used_int_values(eligible)
    buckets = {
        "0": 0,
        "0-10 GiB": 0,
        "10-100 GiB": 0,
        "100-200 GiB": 0,
        "200-250 GiB": 0,
        "250-300 GiB": 0,
        ">=300 GiB": 0,
    }
    for value in used_values:
        bucket = used_bucket(value)
        if bucket is not None:
            buckets[bucket] += 1

    found_rows = [r for r in rows if r.remna_id is not None]
    found = len(found_rows)
    found_by_status: Dict[str, int] = {}
    for row in found_rows:
        key = row.remna_status or "null"
        found_by_status[key] = found_by_status.get(key, 0) + 1
    potential_rows = [r for r in rows if r.potential_after_month_migration]
    potential_by_status: Dict[str, int] = {}
    for row in potential_rows:
        key = row.remna_status or "null"
        potential_by_status[key] = potential_by_status.get(key, 0) + 1
    found_disabled = [r for r in found_rows if r.remna_status == "DISABLED"]
    ge_250 = [
        r
        for r in rows
        if isinstance(r.remna_used_traffic_bytes, int)
        and r.remna_used_traffic_bytes >= 250 * GIB
    ]
    ge_300 = [
        r
        for r in rows
        if isinstance(r.remna_used_traffic_bytes, int)
        and r.remna_used_traffic_bytes >= 300 * GIB
    ]
    limited = [r for r in rows if r.remna_status == "LIMITED"]
    eligible_stats = used_traffic_stats(eligible)
    eligible_stats["p95_gib"] = (
        bytes_to_gib(int(round(_percentile(used_values, 95) or 0)))
        if used_values
        else None
    )
    eligible_stats["buckets"] = buckets

    return {
        "migration_cutoff": cfg.migration_cutoff_raw,
        "migration_cutoff_utc": cfg.migration_cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expected_traffic_limit_bytes": EXPECTED_LIMIT_BYTES,
        "expected_traffic_limit_strategy": EXPECTED_STRATEGY,
        "eligible_statuses": sorted(ELIGIBLE_STATUSES),
        "inspected": len(rows),
        "found_in_remnawave": found,
        "found_by_status": found_by_status,
        "classification_counts": counts,
        "eligible_for_reset": counts[CLASS_ELIGIBLE],
        "eligible_active": len(eligible_active),
        "eligible_disabled": len(eligible_disabled),
        "already_reset_since_cutoff": counts[CLASS_ALREADY_RESET],
        "created_after_cutoff": counts[CLASS_CREATED_AFTER],
        "service_not_month": counts[CLASS_SERVICE_NOT_MONTH],
        "remna_not_month": counts[CLASS_REMNA_NOT_MONTH],
        "wrong_limit": {
            "service_wrong_limit": counts[CLASS_SERVICE_WRONG_LIMIT],
            "remna_wrong_limit": counts[CLASS_REMNA_WRONG_LIMIT],
        },
        "missing": counts[CLASS_MISSING],
        "non_active": counts[CLASS_INACTIVE],
        "errors": counts[CLASS_ERROR] + counts[CLASS_INVALID],
        "potential_after_month_migration": len(potential_rows),
        "potential_active": potential_by_status.get("ACTIVE", 0),
        "potential_disabled": potential_by_status.get("DISABLED", 0),
        "potential_by_status": potential_by_status,
        "disabled_found": {
            "count": len(found_disabled),
            "used_traffic": used_traffic_stats(found_disabled),
        },
        "eligible_used_traffic": eligible_stats,
        "risk": {
            "used_ge_250_gib": len(ge_250),
            "used_ge_300_gib": len(ge_300),
            "limited": len(limited),
            "used_ge_250_usernames": [r.username for r in ge_250],
            "used_ge_300_usernames": [r.username for r in ge_300],
            "limited_usernames": [r.username for r in limited],
        },
        "planned_strategy_impact": planned_strategy_impact(rows),
        "apply_never_uses_potential_field": True,
    }


def write_csv(path: str, rows: Sequence[PlanRow], fields: Sequence[str]) -> None:
    buf = []
    writer_file = []

    class _Buf:
        def write(self, text: str) -> int:
            buf.append(text)
            return len(text)

    out = _Buf()
    writer = csv.DictWriter(out, fieldnames=list(fields), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row.to_report_dict())
        writer_file.append(row)
    atomic_write_text(path, "".join(buf))


def write_reports(
    output_dir: str,
    rows: Sequence[PlanRow],
    cfg: ResetConfig,
) -> Dict[str, Any]:
    ensure_output_dir(output_dir)
    summary = summarize(rows, cfg)
    atomic_write_json(os.path.join(output_dir, "summary.json"), summary)
    atomic_write_json(
        os.path.join(output_dir, "plan.json"),
        [row.to_report_dict() for row in rows],
    )
    write_csv(os.path.join(output_dir, "plan.csv"), rows, REPORT_FIELDS)
    errors = [r for r in rows if r.classification in {CLASS_ERROR, CLASS_INVALID}]
    blocked = [
        r
        for r in rows
        if r.classification not in {CLASS_ELIGIBLE, CLASS_ERROR, CLASS_INVALID}
    ]
    write_csv(os.path.join(output_dir, "errors.csv"), errors, REPORT_FIELDS)
    write_csv(os.path.join(output_dir, "blocked.csv"), blocked, REPORT_FIELDS)
    atomic_write_json(
        os.path.join(output_dir, "planned-strategy-impact.json"),
        summary["planned_strategy_impact"],
    )
    impact_rows = []
    for row in rows:
        impact_rows.append(
            {
                "user_service_id": row.user_service_id,
                "username": row.username,
                "service_id": row.service_id,
                "remna_traffic_limit_strategy": row.remna_traffic_limit_strategy,
                "remna_traffic_limit_bytes": row.remna_traffic_limit_bytes,
                "classification": row.classification,
                "would_need_set_strategy": (
                    row.remna_id is not None
                    and row.remna_traffic_limit_strategy == "NO_RESET"
                ),
                "already_MONTH": row.remna_traffic_limit_strategy == "MONTH",
                "missing": row.classification == CLASS_MISSING,
            }
        )
    _write_impact_csv(os.path.join(output_dir, "planned-strategy-impact.csv"), impact_rows)
    return summary


def _write_impact_csv(path: str, rows: Sequence[Dict[str, Any]]) -> None:
    fields = (
        "user_service_id",
        "username",
        "service_id",
        "remna_traffic_limit_strategy",
        "remna_traffic_limit_bytes",
        "classification",
        "would_need_set_strategy",
        "already_MONTH",
        "missing",
    )
    buf: List[str] = []

    class _Buf:
        def write(self, text: str) -> int:
            buf.append(text)
            return len(text)

    writer = csv.DictWriter(_Buf(), fieldnames=list(fields), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    atomic_write_text(path, "".join(buf))


def reload_service_extract(
    client: HttpClient,
    cfg: ResetConfig,
    session_id: str,
    service_id: Any,
) -> Tuple[Any, Optional[str], Optional[str]]:
    catalog = load_service_catalog(client, cfg, session_id)
    fake_item = {"service_id": service_id}
    service, err = resolve_catalog_service(fake_item, catalog)
    if err or service is None:
        return None, None, err or "service not found on live re-check"
    return extract_traffic_settings(service), service_name_from_service(service), None


def precheck_apply_row(
    row: PlanRow,
    *,
    cfg: ResetConfig,
    extract: Any,
    fetch_kind: str,
    live: Optional[ResetUserState],
    fetch_error: Optional[str],
) -> Tuple[str, Optional[str], Optional[str]]:
    if extract is None:
        return ACTION_SKIP, "service_missing", fetch_error
    fresh = classify_reset_row(
        user_service_id=row.user_service_id,
        username=row.username,
        service_id=row.service_id,
        service_name=row.service_name,
        category=row.category,
        cutoff=cfg.migration_cutoff,
        cutoff_raw=cutoff_iso(cfg),
        extract=extract,
        fetch_kind=fetch_kind,
        remna=live,
        error_message=fetch_error,
    )
    if fresh.classification == CLASS_ELIGIBLE:
        return ACTION_RESET, None, None
    if fresh.classification in {CLASS_ERROR, CLASS_INVALID}:
        return ACTION_ERROR, None, fresh.error_message or fresh.classification
    return ACTION_SKIP, fresh.classification, None


def verify_reset(
    client: HttpClient,
    cfg: ResetConfig,
    *,
    username: str,
    pre: ResetUserState,
) -> Tuple[Optional[str], Optional[ResetUserState]]:
    last_err = "reset verify failed"
    last_state: Optional[ResetUserState] = None
    attempts = max(1, int(cfg.verify_retry_attempts))
    for attempt in range(1, attempts + 1):
        if cfg.verify_retry_delay_sec > 0 and attempt > 1:
            time.sleep(cfg.verify_retry_delay_sec)
        kind, state, err = fetch_remnawave_reset_user(client, cfg, username)
        if kind != "ok" or state is None:
            last_err = (
                f"GET verify attempt {attempt}/{attempts} failed: "
                f"kind={kind} error={err}"
            )
            continue
        last_state = state
        check = _verify_reset_state(pre, state, cfg.migration_cutoff)
        if check is None:
            return None, state
        last_err = f"GET verify attempt {attempt}/{attempts} failed: {check}"
    return last_err, last_state


def _verify_reset_state(
    pre: ResetUserState,
    post: ResetUserState,
    cutoff: datetime,
) -> Optional[str]:
    if post.numeric_id != pre.numeric_id:
        return f"id mismatch: got {post.numeric_id}, expected {pre.numeric_id}"
    if post.traffic_limit_bytes != EXPECTED_LIMIT_BYTES:
        return (
            "trafficLimitBytes mismatch: "
            f"got {post.traffic_limit_bytes!r}, expected {EXPECTED_LIMIT_BYTES}"
        )
    if pre.traffic_limit_bytes != EXPECTED_LIMIT_BYTES:
        return "pre-reset trafficLimitBytes was not the expected Standard limit"
    if post.traffic_limit_strategy != EXPECTED_STRATEGY:
        return (
            "trafficLimitStrategy mismatch: "
            f"got {post.traffic_limit_strategy!r}, expected {EXPECTED_STRATEGY}"
        )
    if pre.traffic_limit_strategy != EXPECTED_STRATEGY:
        return "pre-reset trafficLimitStrategy was not MONTH"
    if post.status != pre.status:
        if pre.status == "DISABLED" and post.status == "ACTIVE":
            return (
                "CRITICAL: DISABLED became ACTIVE after reset "
                f"(pre={pre.status!r} post={post.status!r})"
            )
        return (
            "status changed unexpectedly after reset "
            f"(pre={pre.status!r} post={post.status!r})"
        )
    if not status_is_reset_eligible(pre.status):
        return f"pre-reset status is not eligible: {pre.status!r}"
    if snapshot_identity(post) != snapshot_identity(pre):
        return (
            "user identity fields changed after reset "
            f"(pre={snapshot_identity(pre)} post={snapshot_identity(post)})"
        )
    try:
        post_reset = parse_optional_timestamp(
            post.last_traffic_reset_at, field_name="lastTrafficResetAt"
        )
        pre_reset = parse_optional_timestamp(
            pre.last_traffic_reset_at, field_name="lastTrafficResetAt"
        )
    except ValueError as exc:
        return str(exc)
    if post_reset is None:
        return "lastTrafficResetAt is still null after reset"
    if post_reset < cutoff:
        return (
            "lastTrafficResetAt did not reach migration cutoff "
            f"(got {post.last_traffic_reset_at}, cutoff {cutoff.isoformat()})"
        )
    if pre_reset is not None and post_reset <= pre_reset:
        return (
            "lastTrafficResetAt did not advance "
            f"(pre={pre.last_traffic_reset_at} post={post.last_traffic_reset_at})"
        )
    pre_used = pre.used_traffic_bytes
    post_used = post.used_traffic_bytes
    if (
        isinstance(pre_used, int)
        and not isinstance(pre_used, bool)
        and pre_used > USED_DECREASE_THRESHOLD_BYTES
        and (
            post_used is None
            or not isinstance(post_used, int)
            or isinstance(post_used, bool)
            or post_used >= pre_used
        )
    ):
        return (
            "usedTrafficBytes did not decrease after reset "
            f"(pre={pre_used} post={post_used})"
        )
    return None


def apply_resets(
    client: HttpClient,
    cfg: ResetConfig,
    session_id: str,
    rows: Sequence[PlanRow],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    applied: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for row in rows:
        extract, _name, extract_err = reload_service_extract(
            client, cfg, session_id, row.service_id
        )
        kind, live, fetch_error = fetch_remnawave_reset_user(client, cfg, row.username)
        action, skip_reason, err = precheck_apply_row(
            row,
            cfg=cfg,
            extract=extract,
            fetch_kind=kind,
            live=live,
            fetch_error=extract_err or fetch_error,
        )
        if action == ACTION_SKIP:
            skipped.append(
                {
                    "username": row.username,
                    "user_service_id": row.user_service_id,
                    "reason": skip_reason,
                }
            )
            continue
        if action == ACTION_ERROR or live is None or live.numeric_id is None:
            errors.append(
                {
                    "stage": "precheck",
                    "username": row.username,
                    "user_service_id": row.user_service_id,
                    "error": err or "precheck failed",
                }
            )
            return applied, errors, skipped

        pre = live
        try:
            reset_user_traffic(client, cfg, pre.numeric_id)
        except FatalError as exc:
            errors.append(
                {
                    "stage": "reset",
                    "username": row.username,
                    "user_service_id": row.user_service_id,
                    "error": str(exc),
                }
            )
            return applied, errors, skipped

        verify_error, verified = verify_reset(
            client, cfg, username=row.username, pre=pre
        )
        if verify_error:
            errors.append(
                {
                    "stage": "verify",
                    "username": row.username,
                    "user_service_id": row.user_service_id,
                    "error": verify_error,
                    "pre_used_traffic_bytes": pre.used_traffic_bytes,
                    "pre_last_traffic_reset_at": pre.last_traffic_reset_at,
                    "post_used_traffic_bytes": (
                        verified.used_traffic_bytes if verified else None
                    ),
                    "post_last_traffic_reset_at": (
                        verified.last_traffic_reset_at if verified else None
                    ),
                    "pre_status": pre.status,
                    "post_status": verified.status if verified else None,
                }
            )
            return applied, errors, skipped

        applied.append(
            {
                "user_service_id": row.user_service_id,
                "username": row.username,
                "remnawave_user_id": pre.numeric_id,
                "pre_used_traffic_bytes": pre.used_traffic_bytes,
                "post_used_traffic_bytes": (
                    verified.used_traffic_bytes if verified else None
                ),
                "pre_last_traffic_reset_at": pre.last_traffic_reset_at,
                "post_last_traffic_reset_at": (
                    verified.last_traffic_reset_at if verified else None
                ),
                "pre_status": pre.status,
                "post_status": verified.status if verified else None,
                "status": pre.status,
            }
        )
    return applied, errors, skipped


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "One-time MONTH traffic reset after Standard policy migration "
            "(dry-run by default; never PATCHes users)"
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
        "--migration-cutoff",
        required=True,
        help="ISO8601 UTC start of this migration (example: 2026-09-10T18:00:00Z)",
    )
    parser.add_argument(
        "--category",
        action="append",
        default=[],
        help="Limit to this SHM category (repeatable)",
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
            "Username allowed to reset on --apply (repeatable). Extra "
            "constraint: does not replace --category/--service-id scope."
        ),
    )
    return parser.parse_args(argv)


def config_from_args(
    args: argparse.Namespace, environ: Optional[Dict[str, str]] = None
) -> ResetConfig:
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
    cutoff = parse_migration_cutoff(args.migration_cutoff)
    if bool(args.apply) and not categories and not service_ids:
        raise FatalError(
            "apply refused: require at least one --category or --service-id"
        )
    return ResetConfig(
        shm_base_url=args.shm_base_url.rstrip("/"),
        shm_login=args.shm_login,
        shm_password=shm_password,
        remnawave_panel_url=args.remnawave_panel_url.rstrip("/"),
        remnawave_token=remnawave_token,
        output=args.output,
        migration_cutoff_raw=str(args.migration_cutoff).strip(),
        migration_cutoff=cutoff,
        categories=categories,
        service_ids=service_ids,
        page_size=args.page_size,
        request_delay_ms=args.request_delay_ms,
        apply=bool(args.apply),
        confirm=args.confirm,
        apply_usernames=apply_usernames,
    )


def run(cfg: ResetConfig, client: Optional[HttpClient] = None) -> int:
    interrupted_flag = {"value": False}

    def _on_signal(signum: int, frame: Any) -> None:  # noqa: ARG001
        interrupted_flag["value"] = True

    previous_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[sig] = signal.signal(sig, _on_signal)

    raw_client = client or HttpClient(
        delay_ms=cfg.request_delay_ms,
        timeout=cfg.http_timeout,
        interrupted=lambda: interrupted_flag["value"],
    )
    http: HttpClient = MutationGuard(raw_client)  # type: ignore[assignment]

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

        log(
            "Monthly traffic migration plan; cutoff="
            f"{cfg.migration_cutoff_raw} apply={cfg.apply}"
        )
        log("Authenticating to SHM...")
        session_id = shm_authenticate(http, cfg)
        log("Loading SHM service catalog...")
        catalog = load_service_catalog(http, cfg, session_id)
        log("Building one-time reset plan from live SHM/Remnawave state...")
        rows = build_plan(http, cfg, session_id, catalog)
        summary = write_reports(cfg.output, rows, cfg)
        counts = summary["classification_counts"]
        log(
            "Plan written: "
            f"inspected={summary['inspected']} "
            f"found={summary['found_in_remnawave']} "
            f"eligible_for_reset={summary['eligible_for_reset']} "
            f"eligible_active={summary['eligible_active']} "
            f"eligible_disabled={summary['eligible_disabled']} "
            f"service_not_month={counts[CLASS_SERVICE_NOT_MONTH]} "
            f"remna_not_month={counts[CLASS_REMNA_NOT_MONTH]} "
            f"already_reset={counts[CLASS_ALREADY_RESET]} "
            f"created_after_cutoff={counts[CLASS_CREATED_AFTER]} "
            f"missing={counts[CLASS_MISSING]} "
            f"potential_after_month_migration="
            f"{summary['potential_after_month_migration']} "
            f"potential_active={summary['potential_active']} "
            f"potential_disabled={summary['potential_disabled']}"
        )
        impact = summary["planned_strategy_impact"]
        log(
            "Informational strategy impact (not a mutation): "
            f"NO_RESET={impact['would_need_set_strategy_from_NO_RESET']} "
            f"already_MONTH={impact['already_MONTH']} "
            f"other={impact['other_strategy']} "
            f"missing={impact['missing']}"
        )
        log(
            "Risk: "
            f">=250GiB={summary['risk']['used_ge_250_gib']} "
            f">=300GiB={summary['risk']['used_ge_300_gib']} "
            f"LIMITED={summary['risk']['limited']}"
        )

        if not cfg.apply:
            log("Dry-run only; 0 reset-traffic calls.")
            return 0

        selected = select_apply_rows(rows, apply_usernames=cfg.apply_usernames)
        allow_note = (
            f" allow_list={len(cfg.apply_usernames)}" if cfg.apply_usernames else ""
        )
        log(
            f"Applying {len(selected)} reset-traffic calls "
            f"(eligible={summary['eligible_for_reset']};{allow_note})..."
        )
        applied, errors, skipped = apply_resets(http, cfg, session_id, selected)
        atomic_write_json(os.path.join(cfg.output, "applied.json"), applied)
        atomic_write_json(os.path.join(cfg.output, "apply_errors.json"), errors)
        atomic_write_json(os.path.join(cfg.output, "apply_skipped.json"), skipped)
        log(
            f"Apply finished: reset={len(applied)} skipped={len(skipped)} "
            f"errors={len(errors)}"
        )
        if errors:
            return 2
        return 0
    except Interrupted as exc:
        log(redact_secrets(str(exc), [cfg.shm_password, cfg.remnawave_token]))
        return 130
    except FatalError as exc:
        log(redact_secrets(str(exc), [cfg.shm_password, cfg.remnawave_token]))
        return 1
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        cfg = config_from_args(parse_args(argv))
    except FatalError as exc:
        log(str(exc))
        return 1
    return run(cfg)


if __name__ == "__main__":
    sys.exit(main())
