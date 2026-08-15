#!/bin/bash
#
# Remnawave ↔ SHM template (v1.4)
# Internal Squad определяется по имени:
# 1) us.service.settings.remnawave.internal_squad_name
# 2) fallback: server.settings.remnawave.default_internal_squad_name
# UUID внутреннего сквада в SHM не хранится и всегда резолвится через API панели.
#
# External Squad задаётся опционально через
# us.service.settings.remnawave.external_squad_name.
# Хранится точное имя сквада, а не UUID.
# Серверного fallback для External Squad нет.
# Отсутствие или значение null означает, что External Squad не задан.
# Имя резолвится в UUID через GET /api/external-squads только при CREATE.
#
set -euo pipefail

# ---- SHM placeholders ----
EVENT="{{ event_name }}"
SESSION_ID="{{ user.gen_session.id }}"
API_URL="{{ config.api.url }}"

# ---- server.settings.remnawave.* ----
PANEL_URL="{{ server.settings.remnawave.api }}"
REMNAWAVE_API_TOKEN="{{ server.settings.remnawave.token }}"
DEFAULT_INTERNAL_SQUAD_NAME="{{ server.settings.remnawave.default_internal_squad_name }}"

# ---- us.service.settings.remnawave.* ----
SERVICE_INTERNAL_SQUAD_NAME="{{ us.service.settings.remnawave.internal_squad_name }}"
SERVICE_EXTERNAL_SQUAD_NAME="{{ us.service.settings.remnawave.external_squad_name }}"
SERVICE_TRAFFIC_LIMIT_BYTES="{{ us.service.settings.remnawave.traffic_limit_bytes }}"
SERVICE_TRAFFIC_LIMIT_STRATEGY="{{ us.service.settings.remnawave.traffic_limit_strategy }}"
SERVICE_HWID_DEVICE_LIMIT="{{ us.service.settings.remnawave.hwid_device_limit }}"

# ---- server.settings.remnawave optional ----
REMNAWAVE_SHM_TZ="{{ server.settings.remnawave.shm_tz }}"
REMNAWAVE_EXPIRE_SAFETY_MINUTES="{{ server.settings.remnawave.expire_safety_minutes }}"

USERNAME="us_{{ us.id }}"
SANITIZE_USERNAME="{{ server.settings.remnawave.sanitize_username }}"
SANITIZE_USERNAME="${SANITIZE_USERNAME:-false}"
STATUS_ACTIVE="ACTIVE"
STATUS_DISABLED="DISABLED"

log() { echo "[$(date +'%F %T')] $*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }

_auth_header() {
  [[ -n "${REMNAWAVE_API_TOKEN:-}" ]] || fail "server.settings.remnawave.token is empty"
  echo "Authorization: Bearer ${REMNAWAVE_API_TOKEN}"
}

# HTTP helpers (Remnawave)
_http_get()    { local p="$1"; shift; curl -skS -H "$(_auth_header)" "$@" "${PANEL_URL}${p}"; }
_http_post()   { local p="$1"; shift; curl -skS --fail-with-body -X POST -H "$(_auth_header)" -H 'Content-Type: application/json' "$@" "${PANEL_URL}${p}"; }
_http_patch()  { local p="$1"; shift; curl -skS --fail-with-body -X PATCH -H "$(_auth_header)" -H 'Content-Type: application/json' "$@" "${PANEL_URL}${p}"; }
_http_delete() { local p="$1"; shift; curl -skS --fail-with-body -X DELETE -H "$(_auth_header)" "$@" "${PANEL_URL}${p}"; }

# Helpers

_sanitize_username() {
  local input="$1"
  local out=""
  local c
  for ((i=0; i<${#input}; i++)); do
    c="${input:$i:1}"
    if [[ "$c" =~ [a-zA-Z0-9_-] ]]; then
      out+="$c"
    else
      out+="_"
    fi
  done
  if (( ${#out} < 6 )); then
    out="${out}$(printf '%0.s_' $(seq $((6 - ${#out}))))"
  fi
  echo "$out"
}

if [[ "${SANITIZE_USERNAME}" == "true" ]]; then
  USERNAME_SANITIZED="$(_sanitize_username "${USERNAME}")"
else
  USERNAME_SANITIZED="${USERNAME}"
fi

# Expire: interpret {{ us.expire }} as LOCAL time in SHM TZ (or system TZ), then output UTC (Z)
_expire_iso() {
  local base="{{ us.expire }}"
  local mins="${REMNAWAVE_EXPIRE_SAFETY_MINUTES:-0}"

  [[ -n "${mins}" && "${mins}" != "null" ]] || mins=0

  local base_epoch
  if [[ -n "${REMNAWAVE_SHM_TZ:-}" && "${REMNAWAVE_SHM_TZ}" != "null" ]]; then
    base_epoch="$(TZ="${REMNAWAVE_SHM_TZ}" date -d "${base}" +%s)" || fail "cannot parse us.expire in ${REMNAWAVE_SHM_TZ}"
  else
    base_epoch="$(date -d "${base}" +%s)" || fail "cannot parse us.expire (system TZ)"
  fi

  local final_epoch=$(( base_epoch + mins*60 ))
  date -u -d "@${final_epoch}" +"%Y-%m-%dT%H:%M:%SZ"
}

_user_id_by_username() {
  local username="$1"
  local user_id
  user_id="$(_http_get "/api/users/by-username/${username}" | jq -r '.response.id // empty')"
  [[ "${user_id}" =~ ^[1-9][0-9]*$ ]] || fail "User not found: ${username}"
  echo "${user_id}"
}

_subscription_json_by_username() {
  local username="$1"
  _http_get "/api/subscriptions/by-username/${username}"
}

_normalize_subscription_json() {
  jq '.response |= (if has("subscriptionUrl") then . + {subscription_url: .subscriptionUrl} else . end)'
}

_effective_internal_squad_name() {
  if [[ -n "${SERVICE_INTERNAL_SQUAD_NAME:-}" && "${SERVICE_INTERNAL_SQUAD_NAME}" != "null" ]]; then
    echo "${SERVICE_INTERNAL_SQUAD_NAME}"
  elif [[ -n "${DEFAULT_INTERNAL_SQUAD_NAME:-}" && "${DEFAULT_INTERNAL_SQUAD_NAME}" != "null" ]]; then
    echo "${DEFAULT_INTERNAL_SQUAD_NAME}"
  else
    fail "No internal squad configured: neither us.service.settings.remnawave.internal_squad_name nor server.settings.remnawave.default_internal_squad_name is set"
  fi
}

_effective_external_squad_name() {
  if [[ -n "${SERVICE_EXTERNAL_SQUAD_NAME:-}" && "${SERVICE_EXTERNAL_SQUAD_NAME}" != "null" ]]; then
    echo "${SERVICE_EXTERNAL_SQUAD_NAME}"
  else
    echo ""
  fi
}

_effective_traffic_limit_bytes() {
  local v="${SERVICE_TRAFFIC_LIMIT_BYTES:-}"
  [[ -n "${v}" && "${v}" != "null" ]] || { echo "0"; return; }

  [[ "${v}" =~ ^[0-9]+$ ]] || fail "Invalid traffic_limit_bytes: '${v}' (must be non-negative integer)"
  echo "${v}"
}

_effective_traffic_limit_strategy() {
  local v="${SERVICE_TRAFFIC_LIMIT_STRATEGY:-}"
  [[ -n "${v}" && "${v}" != "null" ]] || { echo "NO_RESET"; return; }

  v="$(echo "${v}" | tr '[:lower:]' '[:upper:]')"
  case "${v}" in
    NO_RESET|DAY|WEEK|MONTH)
      echo "${v}"
      ;;
    *)
      fail "Invalid traffic_limit_strategy: '${v}' (allowed: NO_RESET, DAY, WEEK, MONTH)"
      ;;
  esac
}

_effective_hwid_device_limit() {
  local v="${SERVICE_HWID_DEVICE_LIMIT:-}"
  [[ -n "${v}" && "${v}" != "null" ]] || { echo "0"; return; }

  [[ "${v}" =~ ^[0-9]+$ ]] || fail "Invalid hwid_device_limit: '${v}' (must be non-negative integer)"
  echo "${v}"
}

_log_effective_limits() {
  log "Effective traffic limit bytes: $(_effective_traffic_limit_bytes)"
  log "Effective traffic reset strategy: $(_effective_traffic_limit_strategy)"
  log "Effective HWID device limit: $(_effective_hwid_device_limit)"
}

# Всегда резолвим UUID внутреннего сквада по ИМЕНИ (без кеша)
_resolve_internal_squad_uuid_by_name() {
  local squad_name="$1"
  [[ -n "${squad_name:-}" ]] || fail "internal squad name is empty"

  local uuid
  uuid="$(_http_get "/api/internal-squads" \
    | jq -r --arg NAME "${squad_name}" '.response.internalSquads[] | select(.name==$NAME) | .uuid' \
    | head -n1)"

  [[ -n "${uuid}" ]] || fail "Internal Squad '${squad_name}' not found on panel ${PANEL_URL}"
  echo "${uuid}"
}

# Резолвим UUID внешнего сквада по точному ИМЕНИ (без кеша, без серверного fallback)
_resolve_external_squad_uuid_by_name() {
  local squad_name="$1"
  [[ -n "${squad_name:-}" ]] || fail "external squad name is empty"

  local uuid
  uuid="$(_http_get "/api/external-squads" \
    | jq -r --arg NAME "${squad_name}" '.response.externalSquads[] | select(.name==$NAME) | .uuid' \
    | head -n1)"

  [[ -n "${uuid}" ]] || fail "External Squad '${squad_name}' not found on panel ${PANEL_URL}"
  echo "${uuid}"
}

# Actions (Remnawave 3.x numeric userId)
_require_user_id() {
  local user_id="$1"
  [[ "${user_id}" =~ ^[1-9][0-9]*$ ]] || fail "Invalid Remnawave user id: '${user_id}'"
}

_revoke_user_subscription() { local user_id="$1"; _require_user_id "${user_id}"; _http_post "/api/users/${user_id}/actions/revoke" --data '{}' >/dev/null; }
_delete_user()              { local user_id="$1"; _require_user_id "${user_id}"; _http_delete "/api/users/${user_id}" >/dev/null; }
_reset_user_traffic()       { local user_id="$1"; _require_user_id "${user_id}"; _http_post "/api/users/${user_id}/actions/reset-traffic" --data '{}' >/dev/null; }
_disable_user()             { local user_id="$1"; _require_user_id "${user_id}"; _http_post "/api/users/${user_id}/actions/disable" --data '{}' >/dev/null; }
_enable_user()              { local user_id="$1"; _require_user_id "${user_id}"; _http_post "/api/users/${user_id}/actions/enable" --data '{}' >/dev/null; }

# Payloads
_build_create_payload() {
  local expire_iso="$(_expire_iso)"
  local squad_name="$(_effective_internal_squad_name)"
  local squad_uuid="$(_resolve_internal_squad_uuid_by_name "${squad_name}")"
  local traffic_limit_bytes="$(_effective_traffic_limit_bytes)"
  local traffic_limit_strategy="$(_effective_traffic_limit_strategy)"
  local hwid_device_limit="$(_effective_hwid_device_limit)"
  local external_squad_name="$(_effective_external_squad_name)"
  local external_squad_uuid_json="null"

  if [[ -n "${external_squad_name}" ]]; then
    local external_squad_uuid
    external_squad_uuid="$(_resolve_external_squad_uuid_by_name "${external_squad_name}")"
    external_squad_uuid_json="\"${external_squad_uuid}\""
  fi

  cat <<JSON
{
  "username": "${USERNAME}",
  "status": "${STATUS_ACTIVE}",
  "trafficLimitBytes": ${traffic_limit_bytes},
  "trafficLimitStrategy": "${traffic_limit_strategy}",
  "expireAt": "${expire_iso}",
  "description": "SHM: login={{ user.login }}, name={{ user.full_name }}, url=https://t.me/{{ user.settings.telegram.login }}",
  "tag": null,
  "telegramId": null,
  "email": null,
  "hwidDeviceLimit": ${hwid_device_limit},
  "activeInternalSquads": ["${squad_uuid}"],
  "externalSquadUuid": ${external_squad_uuid_json}
}
JSON
}

_build_update_payload() {
  local user_id="$1"
  _require_user_id "${user_id}"
  local expire_iso="$(_expire_iso)"
  local traffic_limit_bytes="$(_effective_traffic_limit_bytes)"
  local traffic_limit_strategy="$(_effective_traffic_limit_strategy)"
  local hwid_device_limit="$(_effective_hwid_device_limit)"

  cat <<JSON
{
  "id": ${user_id},
  "status": "${STATUS_ACTIVE}",
  "trafficLimitBytes": ${traffic_limit_bytes},
  "trafficLimitStrategy": "${traffic_limit_strategy}",
  "expireAt": "${expire_iso}",
  "hwidDeviceLimit": ${hwid_device_limit}
}
JSON
}

log "Remnawave Template v1.4"
log "EVENT=${EVENT}"

case "${EVENT}" in
  INIT)
    log "Check SHM API: ${API_URL}"
    code="$(curl -sk -o /dev/null -w "%{http_code}" "${API_URL}/shm/v1/test")" || true
    [[ "${code}" == "200" ]] || fail "Incorrect SHM API URL: ${API_URL} (status ${code})"
    log "OK"
    ;;

  CREATE)
    log "Create user ${USERNAME}"
    log "Effective internal squad: $(_effective_internal_squad_name)"
    log "Effective external squad: $(_effective_external_squad_name)"
    _log_effective_limits
    payload="$(_build_create_payload)"
    resp="$(_http_post '/api/users' --data "${payload}")"
    user_id="$(echo "${resp}" | jq -r '.response.id // empty')"
    [[ "${user_id}" =~ ^[1-9][0-9]*$ ]] || fail "Create user failed: ${resp}"

    log "Fetch subscription JSON"
    sub_json="$(_subscription_json_by_username "${USERNAME_SANITIZED}")"
    sub_json_body="$(echo "${sub_json}" | _normalize_subscription_json | jq -c '.response')"

    log "Upload JSON to SHM key vpn_mrzb_{{ us.id }}"
    echo "${sub_json_body}" | jq -c '.' > /tmp/payload.json
    curl -skS -X PUT \
      -H "session-id: ${SESSION_ID}" \
      -H "Content-Type: application/json; charset=utf-8" \
      --data-binary @/tmp/payload.json \
      "${API_URL}/shm/v1/storage/manage/vpn_mrzb_{{ us.id }}" >/dev/null
    rm -f /tmp/payload.json

    log "done"
    ;;

  ACTIVATE)
    log "Activate ${USERNAME_SANITIZED}"
    user_id="$(_user_id_by_username "${USERNAME_SANITIZED}")"

    _enable_user "${user_id}"
    _log_effective_limits
    payload="$(_build_update_payload "${user_id}")"
    _http_patch "/api/users" --data "${payload}" >/dev/null
    log "done"
    ;;

  BLOCK)
    log "Block ${USERNAME_SANITIZED}"
    user_id="$(_user_id_by_username "${USERNAME_SANITIZED}")"
    _disable_user "${user_id}"
    log "done"
    ;;

  REMOVE)
    log "Remove ${USERNAME_SANITIZED}"
    user_id="$(_user_id_by_username "${USERNAME_SANITIZED}")"

    _revoke_user_subscription "${user_id}"
    _delete_user "${user_id}"

    log "Delete SHM key vpn_mrzb_{{ us.id }}"
    curl -skS -X DELETE -H "session-id: ${SESSION_ID}" "${API_URL}/shm/v1/storage/manage/vpn_mrzb_{{ us.id }}" >/dev/null || true
    log "done"
    ;;

  PROLONGATE)
    log "Prolongate ${USERNAME_SANITIZED} + reset traffic"
    user_id="$(_user_id_by_username "${USERNAME_SANITIZED}")"

    _reset_user_traffic "${user_id}"
    _log_effective_limits
    payload="$(_build_update_payload "${user_id}")"
    _http_patch "/api/users" --data "${payload}" >/dev/null
    log "done"
    ;;

  UPDATE)
    log "Update SHM JSON for ${USERNAME_SANITIZED}"
    sub_json="$(_subscription_json_by_username "${USERNAME_SANITIZED}")"
    sub_json_body="$(echo "${sub_json}" | _normalize_subscription_json | jq -c '.response')"
    echo "${sub_json_body}" | jq -c '.' > /tmp/payload.json
    curl -skS -X PUT \
      -H "session-id: ${SESSION_ID}" \
      -H "Content-Type: application/json; charset=utf-8" \
      --data-binary @/tmp/payload.json \
      "${API_URL}/shm/v1/storage/manage/vpn_mrzb_{{ us.id }}" >/dev/null
    rm -f /tmp/payload.json
    log "done"
    ;;

  *)
    log "Unknown event: ${EVENT}"
    ;;
esac