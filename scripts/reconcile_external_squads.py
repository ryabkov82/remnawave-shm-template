#!/usr/bin/env python3
"""One-time External Squad reconciliation for existing Remnawave users.

Maps SHM user-service categories to Remnawave External Squads and optionally
assigns missing externalSquadUuid values via sequential PATCH /api/users.

Dry-run is the default. Apply requires --apply and --confirm ASSIGN_EXTERNAL_SQUADS.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import signal
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


CONFIRM_PHRASE = "ASSIGN_EXTERNAL_SQUADS"
HTTP_TIMEOUT_SEC = 30
VERIFY_RETRY_ATTEMPTS = 5
VERIFY_RETRY_DELAY_SEC = 1.0

CLASS_ALREADY_CORRECT = "already_correct"
CLASS_NEEDS_ASSIGNMENT = "needs_assignment"
CLASS_CONFLICT = "conflict"
CLASS_MISSING = "missing_in_remnawave"
CLASS_ERROR = "error"


class Interrupted(Exception):
    """Raised on Ctrl+C."""


class FatalError(Exception):
    """Unrecoverable error; stop before any Remnawave mutations."""


@dataclass
class PlanRow:
    user_service_id: Any
    username: str
    shm_category: str
    shm_status: Any
    remnawave_user_uuid: Optional[str]
    current_external_squad_uuid: Optional[str]
    target_external_squad_name: str
    target_external_squad_uuid: str
    classification: str
    error_message: Optional[str] = None

    def to_report_dict(self) -> Dict[str, Any]:
        return {
            "user_service_id": self.user_service_id,
            "username": self.username,
            "shm_category": self.shm_category,
            "shm_status": self.shm_status,
            "remnawave_user_uuid": self.remnawave_user_uuid,
            "current_external_squad_uuid": self.current_external_squad_uuid,
            "target_external_squad_name": self.target_external_squad_name,
            "target_external_squad_uuid": self.target_external_squad_uuid,
            "classification": self.classification,
            "error_message": self.error_message,
        }


@dataclass
class ReconcileConfig:
    shm_base_url: str
    shm_login: str
    shm_password: str
    remnawave_panel_url: str
    remnawave_token: str
    output: str
    vff_category: str = "vpn-mz-test"
    vff_squad: str = "VPN-for-Friends"
    fc_category: str = "vpn-mz-fc"
    fc_squad: str = "Friends-Connect"
    page_size: int = 250
    request_delay_ms: int = 50
    apply: bool = False
    confirm: Optional[str] = None
    http_timeout: float = HTTP_TIMEOUT_SEC
    verify_retry_attempts: int = VERIFY_RETRY_ATTEMPTS
    verify_retry_delay_sec: float = VERIFY_RETRY_DELAY_SEC


class HttpClient:
    """Sequential HTTP client with delay, timeout, and interrupt checks."""

    def __init__(
        self,
        delay_ms: int = 50,
        timeout: float = HTTP_TIMEOUT_SEC,
        interrupted: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.delay_ms = delay_ms
        self.timeout = timeout
        self._interrupted = interrupted or (lambda: False)
        self._cookie_jar = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookie_jar)
        )
        self._first = True

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[bytes] = None,
        expect_json: bool = True,
    ) -> Tuple[int, Any, Dict[str, str]]:
        if self._interrupted():
            raise Interrupted("interrupted by signal")
        if not self._first and self.delay_ms > 0:
            time.sleep(self.delay_ms / 1000.0)
        self._first = False

        req_headers = dict(headers or {})
        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                raw = resp.read()
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read() if exc.fp is not None else b""
            resp_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        except Interrupted:
            raise
        except Exception as exc:  # noqa: BLE001
            raise FatalError(f"HTTP {method} {url} failed: {exc}") from exc

        if self._interrupted():
            raise Interrupted("interrupted by signal")

        payload: Any = None
        if expect_json:
            text = raw.decode("utf-8", errors="replace").strip()
            if text:
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise FatalError(
                        f"HTTP {method} {url}: invalid JSON (status {status})"
                    ) from exc
            else:
                payload = None
        else:
            payload = raw
        return int(status), payload, resp_headers


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def redact_secrets(text: str, secrets: Sequence[str]) -> str:
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    return out


def atomic_write_bytes(path: str, data: bytes, mode: int = 0o600) -> None:
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_text(path: str, text: str, mode: int = 0o600) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def atomic_write_json(path: str, obj: Any, mode: int = 0o600) -> None:
    atomic_write_text(
        path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n", mode=mode
    )


def ensure_output_dir(path: str) -> None:
    if os.path.exists(path):
        if not os.path.isdir(path):
            raise FatalError(f"output path exists and is not a directory: {path}")
        if os.listdir(path):
            raise FatalError(f"output directory exists and is not empty: {path}")
        os.chmod(path, 0o700)
    else:
        os.makedirs(path, mode=0o700)
        os.chmod(path, 0o700)


def category_map(cfg: ReconcileConfig) -> Dict[str, str]:
    mapping = {
        cfg.vff_category: cfg.vff_squad,
        cfg.fc_category: cfg.fc_squad,
    }
    if len(set(mapping.keys())) != 2:
        raise FatalError("VFF and FC SHM categories must be distinct")
    if len(set(mapping.values())) != 2:
        raise FatalError("VFF and FC External Squad names must be distinct")
    return mapping


def username_for_user_service(user_service_id: Any) -> str:
    if user_service_id is None or user_service_id == "":
        raise ValueError("user_service_id is empty")
    return f"us_{user_service_id}"


def extract_session_id(payload: Any, headers: Dict[str, str]) -> str:
    if isinstance(payload, dict):
        for key in ("session_id", "sessionId", "id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("session_id", "sessionId", "id"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    return value
    set_cookie = headers.get("set-cookie", "")
    if "session_id=" in set_cookie:
        part = set_cookie.split("session_id=", 1)[1]
        return part.split(";", 1)[0].strip()
    raise FatalError("SHM auth did not return session_id")


def shm_authenticate(client: HttpClient, cfg: ReconcileConfig) -> str:
    url = cfg.shm_base_url.rstrip("/") + "/shm/user/auth.cgi"
    body = json.dumps(
        {"login": cfg.shm_login, "password": cfg.shm_password}
    ).encode("utf-8")
    status, payload, headers = client.request(
        "POST",
        url,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        body=body,
    )
    if status >= 400:
        raise FatalError(f"SHM auth failed with HTTP {status}")
    return extract_session_id(payload, headers)


def iter_shm_user_services(
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
        url = cfg.shm_base_url.rstrip("/") + f"/shm/v1/admin/user/service?{qs}"
        status, payload, _ = client.request(
            "GET",
            url,
            headers={
                "Accept": "application/json",
                "session-id": session_id,
            },
        )
        if status >= 400:
            raise FatalError(f"SHM user/service list failed with HTTP {status}")
        if not isinstance(payload, dict):
            raise FatalError("SHM user/service response is not an object")
        data = payload.get("data")
        if data is None:
            raise FatalError("SHM user/service response missing 'data'")
        if not isinstance(data, list):
            raise FatalError("SHM user/service 'data' is not a list")
        if "items" in payload and payload["items"] is not None:
            try:
                total_items = int(payload["items"])
            except (TypeError, ValueError) as exc:
                raise FatalError("SHM user/service 'items' is not an integer") from exc
        if not data:
            break
        for item in data:
            if isinstance(item, dict):
                yield item
            else:
                raise FatalError("SHM user/service item is not an object")
        offset += len(data)
        if total_items is not None and offset >= total_items:
            break
        if len(data) < cfg.page_size:
            break


def resolve_external_squads(
    client: HttpClient,
    cfg: ReconcileConfig,
    required_names: Sequence[str],
) -> Dict[str, str]:
    url = cfg.remnawave_panel_url.rstrip("/") + "/api/external-squads"
    status, payload, _ = client.request(
        "GET",
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {cfg.remnawave_token}",
        },
    )
    if status >= 400:
        raise FatalError(f"Remnawave external-squads failed with HTTP {status}")
    if not isinstance(payload, dict):
        raise FatalError("Remnawave external-squads response is not an object")
    response = payload.get("response")
    squads: Any
    if isinstance(response, dict):
        squads = response.get("externalSquads")
    else:
        squads = None
    if not isinstance(squads, list):
        raise FatalError(
            "Remnawave external-squads response missing externalSquads list"
        )

    by_name: Dict[str, List[str]] = {}
    for item in squads:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        uuid = item.get("uuid")
        if isinstance(name, str) and isinstance(uuid, str) and name and uuid:
            by_name.setdefault(name, []).append(uuid)

    resolved: Dict[str, str] = {}
    for name in required_names:
        matches = by_name.get(name, [])
        if not matches:
            raise FatalError(f"External Squad '{name}' not found on panel")
        if len(matches) > 1:
            raise FatalError(
                f"External Squad '{name}' is ambiguous ({len(matches)} matches)"
            )
        resolved[name] = matches[0]
    return resolved


def parse_remnawave_user(
    payload: Any,
) -> Tuple[Optional[str], Optional[str], bool]:
    """Return (uuid, externalSquadUuid, external_key_present)."""
    if not isinstance(payload, dict):
        return None, None, False
    response = payload.get("response", payload)
    if not isinstance(response, dict):
        return None, None, False
    user = response.get("user") if isinstance(response.get("user"), dict) else None
    sources = []
    if user is not None:
        sources.append(user)
    sources.append(response)

    uuid = None
    for src in sources:
        value = src.get("uuid")
        if isinstance(value, str) and value:
            uuid = value
            break

    external_key_present = False
    external_value: Optional[str] = None
    for src in sources:
        if "externalSquadUuid" in src:
            external_key_present = True
            value = src.get("externalSquadUuid")
            if value is None or value == "":
                external_value = None
            else:
                external_value = str(value)
            break
    return uuid, external_value, external_key_present


def fetch_remnawave_user(
    client: HttpClient,
    cfg: ReconcileConfig,
    username: str,
) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    """Return (status_kind, uuid, external_uuid, error_message).

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
    uuid, external, _ = parse_remnawave_user(payload)
    if not uuid:
        return "error", None, None, "user uuid missing in response"
    return "ok", uuid, external, None


def verify_assignment(
    client: HttpClient,
    cfg: ReconcileConfig,
    *,
    username: str,
    expected_uuid: str,
    expected_external: str,
    patch_payload: Any,
) -> Optional[str]:
    """Return None on success, or an error message string."""
    got_uuid, got_external, has_external = parse_remnawave_user(patch_payload)
    if got_uuid == expected_uuid and has_external and got_external == expected_external:
        return None
    if got_uuid is not None and got_uuid != expected_uuid:
        return (
            f"PATCH response uuid mismatch: got {got_uuid}, expected {expected_uuid}"
        )
    if has_external and got_external != expected_external:
        return (
            "PATCH response externalSquadUuid mismatch: "
            f"got {got_external}, expected {expected_external}"
        )

    # PATCH response lacks a usable externalSquadUuid — retry GET.
    last_err = "PATCH response missing externalSquadUuid"
    attempts = max(1, int(cfg.verify_retry_attempts))
    for attempt in range(1, attempts + 1):
        if cfg.verify_retry_delay_sec > 0:
            time.sleep(cfg.verify_retry_delay_sec)
        kind, get_uuid, get_external, err = fetch_remnawave_user(
            client, cfg, username
        )
        if (
            kind == "ok"
            and get_uuid == expected_uuid
            and get_external == expected_external
        ):
            return None
        last_err = (
            f"GET verify attempt {attempt}/{attempts} failed: "
            f"kind={kind} uuid={get_uuid} external={get_external} error={err}"
        )
    return last_err


def classify_row(
    *,
    user_service_id: Any,
    category: str,
    status: Any,
    target_name: str,
    target_uuid: str,
    fetch_kind: str,
    user_uuid: Optional[str],
    current_external: Optional[str],
    error_message: Optional[str],
) -> PlanRow:
    username = username_for_user_service(user_service_id)
    base_kwargs = dict(
        user_service_id=user_service_id,
        username=username,
        shm_category=category,
        shm_status=status,
        remnawave_user_uuid=user_uuid,
        current_external_squad_uuid=current_external,
        target_external_squad_name=target_name,
        target_external_squad_uuid=target_uuid,
    )
    if fetch_kind == "missing":
        return PlanRow(**base_kwargs, classification=CLASS_MISSING, error_message=None)
    if fetch_kind == "error":
        return PlanRow(
            **base_kwargs,
            classification=CLASS_ERROR,
            error_message=error_message or "unexpected remnawave error",
        )
    if current_external is None or current_external == "":
        return PlanRow(
            **base_kwargs, classification=CLASS_NEEDS_ASSIGNMENT, error_message=None
        )
    if current_external == target_uuid:
        return PlanRow(
            **base_kwargs, classification=CLASS_ALREADY_CORRECT, error_message=None
        )
    return PlanRow(**base_kwargs, classification=CLASS_CONFLICT, error_message=None)


def build_plan(
    client: HttpClient,
    cfg: ReconcileConfig,
    session_id: str,
    squad_uuids: Dict[str, str],
) -> List[PlanRow]:
    mapping = category_map(cfg)
    target_categories = set(mapping.keys())
    rows: List[PlanRow] = []
    for item in iter_shm_user_services(client, cfg, session_id):
        category = item.get("category")
        if category not in target_categories:
            continue
        user_service_id = item.get("user_service_id")
        target_name = mapping[str(category)]
        target_uuid = squad_uuids[target_name]
        if user_service_id is None or user_service_id == "":
            rows.append(
                PlanRow(
                    user_service_id=user_service_id,
                    username="",
                    shm_category=str(category),
                    shm_status=item.get("status"),
                    remnawave_user_uuid=None,
                    current_external_squad_uuid=None,
                    target_external_squad_name=target_name,
                    target_external_squad_uuid=target_uuid,
                    classification=CLASS_ERROR,
                    error_message="missing user_service_id",
                )
            )
            continue
        username = username_for_user_service(user_service_id)
        kind, user_uuid, current_external, err = fetch_remnawave_user(
            client, cfg, username
        )
        rows.append(
            classify_row(
                user_service_id=user_service_id,
                category=str(category),
                status=item.get("status"),
                target_name=target_name,
                target_uuid=target_uuid,
                fetch_kind=kind,
                user_uuid=user_uuid,
                current_external=current_external,
                error_message=err,
            )
        )
    return rows


def summarize(rows: Sequence[PlanRow]) -> Dict[str, Any]:
    counts = {
        CLASS_ALREADY_CORRECT: 0,
        CLASS_NEEDS_ASSIGNMENT: 0,
        CLASS_CONFLICT: 0,
        CLASS_MISSING: 0,
        CLASS_ERROR: 0,
    }
    for row in rows:
        counts[row.classification] = counts.get(row.classification, 0) + 1
    return {
        "total": len(rows),
        "plan_counts": counts,
        "apply_default": False,
        "note": "Dry-run by default. Conflicts are not modified.",
    }


REPORT_FIELDS = [
    "user_service_id",
    "username",
    "shm_category",
    "shm_status",
    "remnawave_user_uuid",
    "current_external_squad_uuid",
    "target_external_squad_name",
    "target_external_squad_uuid",
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
    conflicts = [r for r in rows if r.classification == CLASS_CONFLICT]
    missing = [r for r in rows if r.classification == CLASS_MISSING]
    write_csv(os.path.join(output_dir, "conflicts.csv"), conflicts, REPORT_FIELDS)
    write_csv(os.path.join(output_dir, "missing.csv"), missing, REPORT_FIELDS)


def write_apply_summary(
    output_dir: str,
    rows: Sequence[PlanRow],
    applied: Sequence[Dict[str, Any]],
    errors: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    summary = summarize(rows)
    requested = summary["plan_counts"][CLASS_NEEDS_ASSIGNMENT]
    summary["apply"] = {
        "requested": requested,
        "applied": len(applied),
        "failed": len(errors),
        "complete": len(errors) == 0 and len(applied) == requested,
    }
    atomic_write_json(os.path.join(output_dir, "summary.json"), summary)
    return summary


def apply_assignments(
    client: HttpClient,
    cfg: ReconcileConfig,
    rows: Sequence[PlanRow],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    needs = [r for r in rows if r.classification == CLASS_NEEDS_ASSIGNMENT]
    applied: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    url = cfg.remnawave_panel_url.rstrip("/") + "/api/users"

    for row in needs:
        if not row.remnawave_user_uuid:
            errors.append(
                {
                    "stage": "precheck",
                    "username": row.username,
                    "user_service_id": row.user_service_id,
                    "error": "missing remnawave_user_uuid",
                }
            )
            return applied, errors

        payload = {
            "uuid": row.remnawave_user_uuid,
            "externalSquadUuid": row.target_external_squad_uuid,
        }
        try:
            status, body, _ = client.request(
                "PATCH",
                url,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {cfg.remnawave_token}",
                },
                body=json.dumps(payload).encode("utf-8"),
            )
        except FatalError as exc:
            errors.append(
                {
                    "stage": "patch",
                    "username": row.username,
                    "user_uuid": row.remnawave_user_uuid,
                    "target_external_squad_uuid": row.target_external_squad_uuid,
                    "error": str(exc),
                }
            )
            return applied, errors

        if status >= 400:
            errors.append(
                {
                    "stage": "patch",
                    "username": row.username,
                    "user_uuid": row.remnawave_user_uuid,
                    "target_external_squad_uuid": row.target_external_squad_uuid,
                    "error": f"HTTP {status}",
                }
            )
            return applied, errors

        verify_error = verify_assignment(
            client,
            cfg,
            username=row.username,
            expected_uuid=row.remnawave_user_uuid,
            expected_external=row.target_external_squad_uuid,
            patch_payload=body,
        )
        if verify_error:
            errors.append(
                {
                    "stage": "verify",
                    "username": row.username,
                    "user_uuid": row.remnawave_user_uuid,
                    "target_external_squad_uuid": row.target_external_squad_uuid,
                    "error": verify_error,
                }
            )
            return applied, errors

        applied.append(
            {
                "user_service_id": row.user_service_id,
                "username": row.username,
                "remnawave_user_uuid": row.remnawave_user_uuid,
                "target_external_squad_name": row.target_external_squad_name,
                "target_external_squad_uuid": row.target_external_squad_uuid,
            }
        )

    return applied, errors


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One-time External Squad reconciliation (dry-run by default)"
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
    parser.add_argument("--vff-category", default="vpn-mz-test")
    parser.add_argument("--vff-squad", default="VPN-for-Friends")
    parser.add_argument("--fc-category", default="vpn-mz-fc")
    parser.add_argument("--fc-squad", default="Friends-Connect")
    parser.add_argument("--page-size", type=int, default=250)
    parser.add_argument("--request-delay-ms", type=int, default=50)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default=None)
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
    return ReconcileConfig(
        shm_base_url=args.shm_base_url.rstrip("/"),
        shm_login=args.shm_login,
        shm_password=shm_password,
        remnawave_panel_url=args.remnawave_panel_url.rstrip("/"),
        remnawave_token=remnawave_token,
        output=args.output,
        vff_category=args.vff_category,
        vff_squad=args.vff_squad,
        fc_category=args.fc_category,
        fc_squad=args.fc_squad,
        page_size=args.page_size,
        request_delay_ms=args.request_delay_ms,
        apply=bool(args.apply),
        confirm=args.confirm,
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

        mapping = category_map(cfg)
        log("Authenticating to SHM...")
        session_id = shm_authenticate(http, cfg)
        log("Resolving External Squads on Remnawave...")
        squad_uuids = resolve_external_squads(
            http, cfg, required_names=list(mapping.values())
        )
        for name, uuid in squad_uuids.items():
            log(f"Resolved External Squad {name} -> {uuid}")

        log("Building reconciliation plan...")
        rows = build_plan(http, cfg, session_id, squad_uuids)
        write_reports(cfg.output, rows)
        summary = summarize(rows)
        plan_counts = summary["plan_counts"]
        log(
            "Plan written: "
            f"total={summary['total']} "
            f"needs_assignment={plan_counts[CLASS_NEEDS_ASSIGNMENT]} "
            f"already_correct={plan_counts[CLASS_ALREADY_CORRECT]} "
            f"conflict={plan_counts[CLASS_CONFLICT]} "
            f"missing={plan_counts[CLASS_MISSING]} "
            f"error={plan_counts[CLASS_ERROR]}"
        )

        if not cfg.apply:
            log("Dry-run complete (no Remnawave changes).")
            return 0

        log("Applying needs_assignment via sequential PATCH /api/users...")
        applied, errors = apply_assignments(http, cfg, rows)
        atomic_write_json(os.path.join(cfg.output, "applied.json"), applied)
        if errors:
            atomic_write_json(os.path.join(cfg.output, "errors.json"), errors)
        apply_summary = write_apply_summary(cfg.output, rows, applied, errors)
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
