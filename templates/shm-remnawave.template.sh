#!/bin/bash
#
# Remnawave ↔ SHM template (v1.6)
# Internal Squad на CREATE (один GET /api/internal-squads, без кеша):
# 1) us.service.settings.remnawave.internal_squad_name          — fixed
# 2) us.service.settings.remnawave.internal_squad_pool_names    — least info.membersCount
# 3) server.settings.remnawave.default_internal_squad_name      — fallback
# ACTIVATE / PROLONGATE / BLOCK / REMOVE / UPDATE сквад не пересчитывают.
# UUID внутреннего сквада в SHM не хранится.
#
# External Squad задаётся опционально через
# us.service.settings.remnawave.external_squad_name.
# Хранится точное имя сквада, а не UUID.
# Серверного fallback для External Squad нет.
# Отсутствие или значение null означает, что External Squad не задан.
# Имя резолвится в UUID через GET /api/external-squads только при CREATE.
#
# HWID device limit (us.service.settings.remnawave.hwid_device_limit):
#   absent/null -> inherit Remnawave panel default (CREATE and UPDATE: omit field)
#   0           -> disable HWID limit for this user (CREATE and UPDATE send 0)
#   N > 0       -> explicit per-user limit N (CREATE and UPDATE send N)
# 0 is not a fallback for a missing setting. null != 0.
# Lifecycle UPDATE never sends JSON null: it cannot reset an existing explicit
# Remnawave value back to panel default. That reset is reconciliation-only.
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
# Literal heredoc: ', ", $, backticks and \ from toJson() are not expanded.
# __SHM_JSON__ shares the directive line so SHM PRE_CHOMP does not glue the
# JSON onto the heredoc delimiter. Absent/null stays empty or the literal null.
SERVICE_INTERNAL_SQUAD_POOL_JSON="$(cat <<'__SHM_INTERNAL_SQUAD_POOL_JSON__'
__SHM_JSON__{{ toJson(us.service.settings.remnawave.internal_squad_pool_names) }}
__SHM_INTERNAL_SQUAD_POOL_JSON__
)"
SERVICE_INTERNAL_SQUAD_POOL_JSON="${SERVICE_INTERNAL_SQUAD_POOL_JSON#__SHM_JSON__}"
# Untrusted profile fields. SHM substitutes these before bash starts, so a raw
# value in this file would be shell source. toJson() + a quoted heredoc keeps
# backticks, dollars, backslashes, quotes and newlines as one JSON string.
# jq decodes that data when the CREATE payload is built.
USER_LOGIN_JSON="$(cat <<'__SHM_USER_LOGIN_JSON__'
__SHM_JSON__{{ toJson(user.login) }}
__SHM_USER_LOGIN_JSON__
)"
USER_LOGIN_JSON="${USER_LOGIN_JSON#__SHM_JSON__}"
USER_FULL_NAME_JSON="$(cat <<'__SHM_USER_FULL_NAME_JSON__'
__SHM_JSON__{{ toJson(user.full_name) }}
__SHM_USER_FULL_NAME_JSON__
)"
USER_FULL_NAME_JSON="${USER_FULL_NAME_JSON#__SHM_JSON__}"
USER_TELEGRAM_LOGIN_JSON="$(cat <<'__SHM_USER_TELEGRAM_LOGIN_JSON__'
__SHM_JSON__{{ toJson(user.settings.telegram.login) }}
__SHM_USER_TELEGRAM_LOGIN_JSON__
)"
USER_TELEGRAM_LOGIN_JSON="${USER_TELEGRAM_LOGIN_JSON#__SHM_JSON__}"
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

# Filled by _select_internal_squad_for_create before _build_create_payload.
SELECTED_INTERNAL_SQUAD_MODE=""
SELECTED_INTERNAL_SQUAD_NAME=""
SELECTED_INTERNAL_SQUAD_UUID=""
SELECTED_INTERNAL_SQUAD_MEMBERS=""
SELECTED_INTERNAL_SQUAD_LOAD=""

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
    fail "No internal squad configured: neither us.service.settings.remnawave.internal_squad_name nor us.service.settings.remnawave.internal_squad_pool_names nor server.settings.remnawave.default_internal_squad_name is set"
  fi
}

# Prints trimmed pool JSON and returns 0 when a pool value is present.
# Empty and JSON null mean the dynamic pool is not configured (not an error).
_internal_squad_pool_raw() {
  local raw="${SERVICE_INTERNAL_SQUAD_POOL_JSON:-}"
  raw="${raw#"${raw%%[![:space:]]*}"}"
  raw="${raw%"${raw##*[![:space:]]}"}"
  if [[ -z "${raw}" || "${raw}" == "null" ]]; then
    return 1
  fi
  printf '%s' "${raw}"
}

_log_selected_internal_squad() {
  log "Internal squad selection mode: ${SELECTED_INTERNAL_SQUAD_MODE}"
  if [[ "${SELECTED_INTERNAL_SQUAD_MODE}" == "least_members" ]]; then
    log "Internal squad pool load: ${SELECTED_INTERNAL_SQUAD_LOAD}"
  fi
  if [[ -n "${SELECTED_INTERNAL_SQUAD_MEMBERS}" ]]; then
    log "Selected internal squad: ${SELECTED_INTERNAL_SQUAD_NAME} (members=${SELECTED_INTERNAL_SQUAD_MEMBERS})"
  else
    log "Selected internal squad: ${SELECTED_INTERNAL_SQUAD_NAME}"
  fi
}

_require_internal_squads_snapshot() {
  local squads_json="$1"
  [[ -n "${squads_json}" ]] || fail "Empty response from /api/internal-squads"
  jq -e '.response.internalSquads | type == "array"' >/dev/null 2>&1 <<<"${squads_json}" \
    || fail "Unexpected /api/internal-squads response"
}

_assign_named_internal_squad() {
  local squads_json="$1"
  local name="$2"
  local result ok
  result="$(jq -c --arg NAME "${name}" '
    [ .response.internalSquads[] | select(.name == $NAME) ] as $hits
    | if ($hits | length) == 0 then
        {ok: false, missing: true}
      elif ($hits[0].uuid | type) != "string" or $hits[0].uuid == "" then
        {ok: false, missing: false, error: ("Internal Squad has no uuid: " + $NAME)}
      else
        {
          ok: true,
          name: $NAME,
          uuid: $hits[0].uuid,
          members: (
            if ($hits[0].info.membersCount | type) == "number" then
              $hits[0].info.membersCount
            else
              null
            end
          )
        }
      end
  ' <<<"${squads_json}")" || fail "Failed to parse /api/internal-squads response"
  ok="$(jq -r '.ok' <<<"${result}")"
  if [[ "${ok}" != "true" ]]; then
    if [[ "$(jq -r '.missing // false' <<<"${result}")" == "true" ]]; then
      fail "Internal Squad '${name}' not found on panel ${PANEL_URL}"
    fi
    fail "$(jq -r '.error' <<<"${result}")"
  fi
  SELECTED_INTERNAL_SQUAD_NAME="$(jq -r '.name' <<<"${result}")"
  SELECTED_INTERNAL_SQUAD_UUID="$(jq -r '.uuid' <<<"${result}")"
  SELECTED_INTERNAL_SQUAD_MEMBERS="$(jq -r 'if .members == null then "" else (.members | tostring) end' <<<"${result}")"
  SELECTED_INTERNAL_SQUAD_LOAD=""
}

_assign_least_members_internal_squad() {
  local squads_json="$1"
  local pool_json="$2"
  local result ok
  jq -e 'type' >/dev/null 2>&1 <<<"${pool_json}" \
    || fail "Invalid internal_squad_pool_names: not valid JSON"
  result="$(jq -c --argjson POOL "${pool_json}" '
    def bad($msg): {ok: false, error: $msg};
    if ($POOL | type) != "array" then
      bad("Invalid internal_squad_pool_names: must be a JSON array of squad names")
    elif ($POOL | length) == 0 then
      bad("Invalid internal_squad_pool_names: empty array")
    elif any($POOL[]; type != "string") then
      bad("Invalid internal_squad_pool_names: every item must be a non-empty string")
    elif any($POOL[]; length == 0) then
      bad("Invalid internal_squad_pool_names: contains an empty string")
    elif any($POOL[]; . != gsub("^[[:space:]]+|[[:space:]]+$"; "")) then
      bad("Invalid internal_squad_pool_names: name has leading or trailing whitespace")
    elif ($POOL | length) != ($POOL | unique | length) then
      bad("Invalid internal_squad_pool_names: duplicate names")
    else
      .response.internalSquads as $squads
      | [ $POOL[] as $name
          | [ $squads[] | select(.name == $name) ] as $hits
          | if ($hits | length) == 0 then
              {missing: $name}
            elif ($hits[0].uuid | type) != "string" or $hits[0].uuid == "" then
              {bad_uuid: $name}
            elif ($hits[0].info.membersCount | type) != "number" then
              {bad_members: $name}
            else
              {name: $name, uuid: $hits[0].uuid, members: $hits[0].info.membersCount}
            end
        ] as $rows
      | if any($rows[]; has("missing")) then
          bad("Configured internal squad not found on panel: " + ([$rows[] | select(has("missing")) | .missing] | join(", ")))
        elif any($rows[]; has("bad_uuid")) then
          bad("Internal squad has no uuid: " + ([$rows[] | select(has("bad_uuid")) | .bad_uuid] | join(", ")))
        elif any($rows[]; has("bad_members")) then
          bad("Internal squad membersCount is not a number: " + ([$rows[] | select(has("bad_members")) | .bad_members] | join(", ")))
        else
          ($rows | sort_by(.members, .name) | .[0]) as $best
          | {
              ok: true,
              name: $best.name,
              uuid: $best.uuid,
              members: $best.members,
              load: ($rows | map("\(.name)=\(.members)") | join(", "))
            }
        end
    end
  ' <<<"${squads_json}")" || fail "Failed to evaluate internal squad pool"
  ok="$(jq -r '.ok' <<<"${result}")"
  if [[ "${ok}" != "true" ]]; then
    local err
    err="$(jq -r '.error // "internal squad pool selection failed"' <<<"${result}")"
    case "${err}" in
      "Configured internal squad not found on panel:"*)
        fail "${err} (${PANEL_URL})"
        ;;
    esac
    fail "${err}"
  fi
  SELECTED_INTERNAL_SQUAD_NAME="$(jq -r '.name' <<<"${result}")"
  SELECTED_INTERNAL_SQUAD_UUID="$(jq -r '.uuid' <<<"${result}")"
  SELECTED_INTERNAL_SQUAD_MEMBERS="$(jq -r '.members | tostring' <<<"${result}")"
  SELECTED_INTERNAL_SQUAD_LOAD="$(jq -r '.load' <<<"${result}")"
}

# One snapshot per CREATE. Sets SELECTED_INTERNAL_SQUAD_* for _build_create_payload.
_select_internal_squad_for_create() {
  local squads_json pool_json fallback_name
  squads_json="$(_http_get "/api/internal-squads")"
  _require_internal_squads_snapshot "${squads_json}"

  if [[ -n "${SERVICE_INTERNAL_SQUAD_NAME:-}" && "${SERVICE_INTERNAL_SQUAD_NAME}" != "null" ]]; then
    SELECTED_INTERNAL_SQUAD_MODE="fixed"
    _assign_named_internal_squad "${squads_json}" "${SERVICE_INTERNAL_SQUAD_NAME}"
  elif pool_json="$(_internal_squad_pool_raw)"; then
    SELECTED_INTERNAL_SQUAD_MODE="least_members"
    _assign_least_members_internal_squad "${squads_json}" "${pool_json}"
  else
    fallback_name="$(_effective_internal_squad_name)"
    SELECTED_INTERNAL_SQUAD_MODE="fallback"
    _assign_named_internal_squad "${squads_json}" "${fallback_name}"
  fi
  _log_selected_internal_squad
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

# Validate in the current shell (not in $(...)): `local x="$(fail)"` would swallow exit.
_validate_hwid_device_limit() {
  local v="${SERVICE_HWID_DEVICE_LIMIT:-}"
  if [[ -z "${v}" || "${v}" == "null" ]]; then
    return 0
  fi
  [[ "${v}" =~ ^[0-9]+$ ]] || fail "Invalid hwid_device_limit: '${v}' (must be non-negative integer or null)"
}

# Prints empty string for absent/null (panel default), otherwise a non-negative integer.
# Does not use 0 as a fallback for a missing setting.
_effective_hwid_device_limit() {
  _validate_hwid_device_limit
  local v="${SERVICE_HWID_DEVICE_LIMIT:-}"
  if [[ -z "${v}" || "${v}" == "null" ]]; then
    echo ""
    return
  fi
  echo "${v}"
}

_hwid_device_limit_log_label() {
  _validate_hwid_device_limit
  local v="${SERVICE_HWID_DEVICE_LIMIT:-}"
  if [[ -z "${v}" || "${v}" == "null" ]]; then
    echo "panel default"
  elif [[ "${v}" == "0" ]]; then
    echo "disabled (0)"
  else
    echo "${v}"
  fi
}

_log_effective_limits() {
  log "Effective traffic limit bytes: $(_effective_traffic_limit_bytes)"
  log "Effective traffic reset strategy: $(_effective_traffic_limit_strategy)"
  log "Effective HWID device limit: $(_hwid_device_limit_log_label)"
}

# Резолв UUID по имени отдельным GET. CREATE его не вызывает:
# UUID уже выбран из snapshot в _select_internal_squad_for_create.
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

# Manual reset on PROLONGATE only when Remnawave will not schedule one.
# DAY/WEEK/MONTH are reset by Remnawave; NO_RESET is not.
_should_reset_traffic_on_prolongate() {
  local strategy
  strategy="$(_effective_traffic_limit_strategy)"
  [[ "${strategy}" == "NO_RESET" ]]
}

_reset_traffic_on_prolongate_if_needed() {
  local user_id="$1"
  local strategy
  strategy="$(_effective_traffic_limit_strategy)"
  if _should_reset_traffic_on_prolongate; then
    log "Traffic reset on PROLONGATE: enabled (strategy=${strategy})"
    _reset_user_traffic "${user_id}"
  else
    log "Traffic reset on PROLONGATE: skipped (strategy=${strategy}; managed by Remnawave)"
  fi
}

# Empty text and JSON null become "". A JSON string is returned unchanged.
# Call as: value="$(_json_string_or_empty "$raw")" || fail "..."
# so a rejected value exits the current shell (fail inside $(...) would not).
_json_string_or_empty() {
  local raw="$1"
  local trimmed
  trimmed="${raw#"${raw%%[![:space:]]*}"}"
  trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
  if [[ -z "${trimmed}" || "${trimmed}" == "null" ]]; then
    printf '%s' '""'
    return 0
  fi
  jq -e -n --argjson value "${trimmed}" '$value | type == "string"' >/dev/null || return 1
  printf '%s' "${trimmed}"
}

# Payloads
_build_create_payload() {
  _validate_hwid_device_limit
  local expire_iso="$(_expire_iso)"
  local squad_uuid="${SELECTED_INTERNAL_SQUAD_UUID:-}"
  [[ -n "${squad_uuid}" ]] || fail "Internal squad UUID is not selected"
  local traffic_limit_bytes="$(_effective_traffic_limit_bytes)"
  local traffic_limit_strategy="$(_effective_traffic_limit_strategy)"
  local hwid_device_limit
  hwid_device_limit="$(_effective_hwid_device_limit)"
  local external_squad_name="$(_effective_external_squad_name)"
  local external_squad_uuid=""
  local login_json name_json telegram_json

  if [[ -n "${external_squad_name}" ]]; then
    external_squad_uuid="$(_resolve_external_squad_uuid_by_name "${external_squad_name}")"
  fi

  login_json="$(_json_string_or_empty "${USER_LOGIN_JSON}")" || fail "user.login must be a JSON string"
  name_json="$(_json_string_or_empty "${USER_FULL_NAME_JSON}")" || fail "user.full_name must be a JSON string"
  telegram_json="$(_json_string_or_empty "${USER_TELEGRAM_LOGIN_JSON}")" || fail "user.settings.telegram.login must be a JSON string"

  jq -n \
    --arg username "${USERNAME}" \
    --arg status "${STATUS_ACTIVE}" \
    --argjson trafficLimitBytes "${traffic_limit_bytes}" \
    --arg trafficLimitStrategy "${traffic_limit_strategy}" \
    --arg expireAt "${expire_iso}" \
    --argjson login "${login_json}" \
    --argjson fullName "${name_json}" \
    --argjson telegramLogin "${telegram_json}" \
    --arg squadUuid "${squad_uuid}" \
    --arg externalSquadUuid "${external_squad_uuid}" \
    --arg hwidDeviceLimit "${hwid_device_limit}" \
    '{
      username: $username,
      status: $status,
      trafficLimitBytes: $trafficLimitBytes,
      trafficLimitStrategy: $trafficLimitStrategy,
      expireAt: $expireAt,
      description: ("SHM: login=" + $login + ", name=" + $fullName + ", url=https://t.me/" + $telegramLogin),
      tag: null,
      telegramId: null,
      email: null
    }
    + (if $hwidDeviceLimit == "" then {} else {hwidDeviceLimit: ($hwidDeviceLimit | tonumber)} end)
    + {
        activeInternalSquads: [$squadUuid],
        externalSquadUuid: (if $externalSquadUuid == "" then null else $externalSquadUuid end)
      }'
}

_build_update_payload() {
  local user_id="$1"
  _require_user_id "${user_id}"
  _validate_hwid_device_limit
  local expire_iso="$(_expire_iso)"
  local traffic_limit_bytes="$(_effective_traffic_limit_bytes)"
  local traffic_limit_strategy="$(_effective_traffic_limit_strategy)"
  local hwid_device_limit
  hwid_device_limit="$(_effective_hwid_device_limit)"
  local hwid_update_comma=""
  local hwid_update_line=""

  if [[ -n "${hwid_device_limit}" ]]; then
    hwid_update_comma=","
    hwid_update_line="  \"hwidDeviceLimit\": ${hwid_device_limit}"
  fi

  cat <<JSON
{
  "id": ${user_id},
  "status": "${STATUS_ACTIVE}",
  "trafficLimitBytes": ${traffic_limit_bytes},
  "trafficLimitStrategy": "${traffic_limit_strategy}",
  "expireAt": "${expire_iso}"${hwid_update_comma}
${hwid_update_line}
}
JSON
}

log "Remnawave Template v1.6"
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
    _select_internal_squad_for_create
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
    log "Prolongate ${USERNAME_SANITIZED}"
    user_id="$(_user_id_by_username "${USERNAME_SANITIZED}")"

    _reset_traffic_on_prolongate_if_needed "${user_id}"
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