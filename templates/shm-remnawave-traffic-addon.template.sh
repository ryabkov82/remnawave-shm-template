#!/bin/bash
#
# remnawave_traffic_addon
#
# CREATE для услуги «Дополнительный трафик 300 ГБ» повышает лимит уже
# существующего пользователя Remnawave us_<parent_user_service_id> до
# абсолютного итога из user_services.settings этой покупки.
# REMOVE возвращает сохранённую базу после конца периода и подтверждённого
# сброса. Срок, статус и параметры подключения не меняются.
#
# Оплата читается из списания текущего экземпляра. Собственного состояния,
# storage, подписи и блокировки у шаблона нет: результат — код выхода spool.
#
# Успех — только код 0 после повторного чтения панели. Если во время PATCH
# период уже закрылся, код 0 не возвращается: в stderr остаётся диагностика.
#
# TRAFFIC_ADDON_CLOCK_FILE предназначен для тестов. В Production его нет.
# Основной шаблон remnawave этот файл не изменяет.
#
set -euo pipefail
umask 077

_RAW_EVENT="$(cat <<'__SHM_EVENT__'
__SHM_JSON__{{ toJson(event_name) }}
__SHM_EVENT__
)"
_RAW_SESSION="$(cat <<'__SHM_SESSION__'
__SHM_JSON__{{ toJson(user.gen_session.id) }}
__SHM_SESSION__
)"
_RAW_API="$(cat <<'__SHM_API__'
__SHM_JSON__{{ toJson(config.api.url) }}
__SHM_API__
)"
_RAW_PANEL="$(cat <<'__SHM_PANEL__'
__SHM_JSON__{{ toJson(server.settings.remnawave.api) }}
__SHM_PANEL__
)"
_RAW_TOKEN="$(cat <<'__SHM_TOKEN__'
__SHM_JSON__{{ toJson(server.settings.remnawave.token) }}
__SHM_TOKEN__
)"
_RAW_US="$(cat <<'__SHM_US__'
__SHM_JSON__{{ toJson(us.id) }}
__SHM_US__
)"
_RAW_USER="$(cat <<'__SHM_USER__'
__SHM_JSON__{{ toJson(us.user_id) }}
__SHM_USER__
)"
_RAW_SERVICE="$(cat <<'__SHM_SERVICE__'
__SHM_JSON__{{ toJson(us.service_id) }}
__SHM_SERVICE__
)"
_RAW_CATEGORY="$(cat <<'__SHM_CATEGORY__'
__SHM_JSON__{{ toJson(us.service.category) }}
__SHM_CATEGORY__
)"
_RAW_COST="$(cat <<'__SHM_COST__'
__SHM_JSON__{{ toJson(us.service.cost) }}
__SHM_COST__
)"

SETTINGS_JSON="$(cat <<'__SHM_SETTINGS_JSON__'
__SHM_JSON__{{ toJson(us.settings) }}
__SHM_SETTINGS_JSON__
)"
SETTINGS_JSON="${SETTINGS_JSON#__SHM_JSON__}"
CATALOG_JSON="$(cat <<'__SHM_CATALOG_JSON__'
__SHM_JSON__{{ toJson(us.service.settings) }}
__SHM_CATALOG_JSON__
)"
CATALOG_JSON="${CATALOG_JSON#__SHM_JSON__}"
WITHDRAW_JSON="$(cat <<'__SHM_WITHDRAW_JSON__'
__SHM_JSON__{{ toJson(us.withdraw.get) }}
__SHM_WITHDRAW_JSON__
)"
WITHDRAW_JSON="${WITHDRAW_JSON#__SHM_JSON__}"
ADDON_SERVICES_JSON="null"
ADDON_WITHDRAWS_JSON="null"
# {{ IF event_name == 'REMOVE' }}
ADDON_SERVICES_JSON="$(cat <<'__SHM_ADDON_SERVICES__'
__SHM_JSON__{{ toJson(user.services.list(where = { service_id = 17, status = 'ANY' })) }}
__SHM_ADDON_SERVICES__
)"
ADDON_WITHDRAWS_JSON="$(cat <<'__SHM_ADDON_WITHDRAWS__'
__SHM_JSON__{{ toJson(user.wd.list(where = { service_id = 17 })) }}
__SHM_ADDON_WITHDRAWS__
)"
# {{ END }}

EXPECTED_SERVICE_ID="17"
EXPECTED_CATEGORY="traffic-add-vff"
MAX_BYTES="9007199254740991"

PURCHASE_ID=""
PARENT_US_ID=""
PERIOD=""
START_AT=""
END_AT=""
STAGE="start"
WORK_DIR=""
PANEL_FILE=""
BASE_LIMIT=""
ADDON_LIMIT=""
APPLIED_LIMIT=""
PATCH_UUID=""

log() {
  echo "[$(date -u +'%F %T')] purchase_id=${PURCHASE_ID:-} parent_user_service_id=${PARENT_US_ID:-} addon_user_service_id=${ADDON_US_ID:-} period=${PERIOD:-} stage=${STAGE} $*" >&2
}

_result() {
  local level="$1" reason="$2"
  log "RESULT level=${level} reason=${reason}"
  echo "RESULT level=${level} reason=${reason}" >&2
  echo "ERROR: ${reason}" >&2
}
stop_temporary() { _result temporary "$1"; exit 1; }
stop_permanent() { _result permanent "$1"; exit 1; }

_scalar() {
  local raw="$1"
  raw="${raw#__SHM_JSON__}"
  jq -er . <<<"${raw}"
}

_auth_header() {
  [[ -n "${REMNAWAVE_API_TOKEN:-}" ]] || stop_permanent "panel_token_missing"
  echo "Authorization: Bearer ${REMNAWAVE_API_TOKEN}"
}

_max_time() {
  local limit="${TRAFFIC_ADDON_HTTP_MAX_TIME:-20}"
  [[ "${limit}" =~ ^[1-9][0-9]*$ ]] || stop_permanent "bad_http_timeout"
  echo "${limit}"
}

_tmp() {
  [[ -n "${WORK_DIR}" && -d "${WORK_DIR}" ]] || stop_temporary "workdir_missing"
  mktemp "${WORK_DIR}/item.XXXXXX"
}

# TLS проверяется системным хранилищем CA. Оба используемых API
# отдают цепочку, которая с хоста шаблона проходит без -k.
_curl_code() {
  local dest="$1"
  shift
  local code curl_rc
  code="$(curl -sS --max-time "$(_max_time)" -o "${dest}" -w "%{http_code}" "$@")" && curl_rc=0 || curl_rc=$?
  code="${code//$'\r'/}"
  if [[ "${curl_rc}" -ne 0 || -z "${code}" || "${code}" == "000" ]]; then
    echo "transport"
    return 0
  fi
  echo "${code}"
}

_now_epoch() {
  local value
  if [[ -n "${TRAFFIC_ADDON_CLOCK_FILE:-}" ]]; then
    value="$(tr -d '[:space:]' < "${TRAFFIC_ADDON_CLOCK_FILE}")" || stop_temporary "clock_unreadable"
    [[ "${value}" =~ ^[0-9]+$ ]] || stop_temporary "clock_unreadable"
    echo "${value}"
    return 0
  fi
  date -u +%s
}

_epoch() { date -u -d "$1" +%s 2>/dev/null || return 1; }

_json_or_null() {
  local raw="$1"
  if [[ -z "${raw}" || "${raw}" == "null" ]]; then
    echo "null"
    return 0
  fi
  jq -e -c . >/dev/null <<<"${raw}" || return 1
  jq -c . <<<"${raw}"
}

# shellcheck disable=SC2317
_on_exit() {
  if [[ -n "${WORK_DIR}" && -d "${WORK_DIR}" ]]; then
    rm -rf "${WORK_DIR}"
  fi
}
trap _on_exit EXIT
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/traffic-addon.XXXXXX")"

_require_parent() {
  local dest code reason
  dest="$(_tmp)" || stop_temporary "shm_io"
  code="$(_curl_code "${dest}" -H "session-id: ${SESSION_ID}" \
    "${API_URL}/shm/v1/user/service?user_service_id=${PARENT_US_ID}")"
  if [[ "${code}" == "transport" || "${code}" =~ ^5 ]]; then
    stop_temporary "shm_unavailable"
  fi
  if [[ "${code}" != "200" ]]; then
    stop_temporary "shm_parent_${code}"
  fi
  reason="$(jq -r --arg parent_id "${PARENT_US_ID}" --arg user "${ADDON_USER_ID}" --slurpfile cat <(printf '%s' "${CATALOG_JSON}") '
    if (.data | type != "array") or (.data | length != 1) then "parent_not_owned"
    elif (.data[0].user_service_id | tostring) != $parent_id then "parent_not_owned"
    elif (.data[0].user_id | tostring) != $user then "parent_not_owned"
    elif .data[0].status == "PROGRESS" then "parent_in_progress"
    elif .data[0].status != "ACTIVE" then "parent_not_active"
    elif .data[0].service.category != $cat[0].traffic_addon.parent_category then "parent_category_mismatch"
    elif ((.data[0].service_id | tonumber) as $id | $cat[0].traffic_addon.parent_service_ids | index($id) == null) then "tariff_not_allowed"
    else "ok" end
  ' "${dest}")" || stop_temporary "parent_unreadable"
  case "${reason}" in
    ok) ;;
    parent_in_progress) stop_temporary "parent_in_progress" ;;
    *) stop_permanent "${reason}" ;;
  esac
}

_panel_file() {
  local dest code
  dest="$(_tmp)" || stop_temporary "panel_io"
  code="$(_curl_code "${dest}" -H "$(_auth_header)" \
    "${PANEL_URL}/api/users/by-username/us_${PARENT_US_ID}")"
  if [[ "${code}" == "transport" || "${code}" =~ ^5 ]]; then
    stop_temporary "panel_unavailable"
  fi
  if [[ "${code}" == "404" ]]; then
    stop_permanent "panel_user_missing"
  fi
  if [[ "${code}" != "200" ]]; then
    stop_temporary "panel_read_${code}"
  fi
  jq -e -c '.response | type == "object"' "${dest}" >/dev/null || stop_temporary "panel_malformed"
  echo "${dest}"
}

# open, period_closed, period_not_open, period_not_confirmed, created_at_in_future, period_bounds
_period_status() {
  local panel="$1" now_epoch start_epoch end_epoch created_at created_epoch reset_at reset_epoch
  now_epoch="$(_now_epoch)"
  start_epoch="$(_epoch "${START_AT}")" || { echo "period_bounds"; return 0; }
  end_epoch="$(_epoch "${END_AT}")" || { echo "period_bounds"; return 0; }
  if (( now_epoch >= end_epoch )); then
    echo "period_closed"
    return 0
  fi
  if (( now_epoch < start_epoch )); then
    echo "period_not_open"
    return 0
  fi
  if [[ -z "${panel}" ]]; then
    echo "open"
    return 0
  fi
  created_at="$(jq -r '.response.createdAt // empty' "${panel}")"
  reset_at="$(jq -r '.response.lastTrafficResetAt // empty' "${panel}")"
  created_epoch=""
  reset_epoch=""
  if [[ -n "${created_at}" ]]; then
    created_epoch="$(_epoch "${created_at}")" || created_epoch=""
  fi
  if [[ -n "${created_epoch}" && "${created_epoch}" -gt "${now_epoch}" ]]; then
    echo "created_at_in_future"
    return 0
  fi
  if [[ -n "${reset_at}" ]]; then
    reset_epoch="$(_epoch "${reset_at}")" || reset_epoch=""
  fi
  if [[ -n "${reset_epoch}" && "${reset_epoch}" -ge "${start_epoch}" && "${reset_epoch}" -le "${now_epoch}" ]]; then
    echo "open"
    return 0
  fi
  if [[ -z "${reset_at}" && -n "${created_epoch}" && "${created_epoch}" -ge "${start_epoch}" && "${created_epoch}" -le "${now_epoch}" ]]; then
    echo "open"
    return 0
  fi
  echo "period_not_confirmed"
}

_require_period() {
  local status
  status="$(_period_status "$1")"
  case "${status}" in
    open) return 0 ;;
    period_closed|period_bounds) stop_permanent "${status}" ;;
    *) stop_temporary "${status}" ;;
  esac
}

_expire_state() {
  local raw="$1" epoch now_epoch
  [[ -n "${raw}" ]] || { echo "invalid"; return 0; }
  epoch="$(_epoch "${raw}")" || { echo "invalid"; return 0; }
  now_epoch="$(_now_epoch)"
  if (( epoch <= now_epoch )); then
    echo "past"
  else
    echo "future"
  fi
}

_require_expire_future() {
  local state
  state="$(_expire_state "$1")"
  case "${state}" in
    future) return 0 ;;
    past) stop_permanent "subscription_expired" ;;
    *) stop_permanent "expire_invalid" ;;
  esac
}

_load_panel_fields() {
  local panel="$1"
  PANEL_USERNAME="$(jq -r '.response.username // empty' "${panel}")"
  PANEL_STATUS="$(jq -r '.response.status // empty' "${panel}")"
  PANEL_STRATEGY="$(jq -r '.response.trafficLimitStrategy // empty' "${panel}")"
  PANEL_UUID="$(jq -r '.response.uuid // empty' "${panel}")"
  PANEL_EXPIRE="$(jq -r '.response.expireAt // empty' "${panel}")"
  PANEL_LIMIT="$(jq -r '.response.trafficLimitBytes | tonumber? // empty' "${panel}")"
  [[ "${PANEL_USERNAME}" == "us_${PARENT_US_ID}" ]] || stop_permanent "panel_username_mismatch"
  [[ "${PANEL_STRATEGY}" == "MONTH" ]] || stop_permanent "strategy_mismatch"
  [[ "${PANEL_UUID}" =~ ^[0-9a-fA-F-]{36}$ ]] || stop_temporary "panel_malformed"
  [[ "${PANEL_LIMIT}" =~ ^[0-9]+$ ]] || stop_temporary "panel_malformed"
  case "${PANEL_STATUS}" in
    ACTIVE|LIMITED) ;;
    *) stop_permanent "panel_status" ;;
  esac
}

_note_period_crossed() {
  log "period_crossed observed_limit=${PANEL_LIMIT} applied_limit_bytes=${APPLIED_LIMIT} period_ends_at=${END_AT}"
  stop_permanent "period_crossed"
}

# UUID фиксируется в теле PATCH. Подтверждающее чтение должно вернуть того же пользователя.
_require_patched_uuid() {
  [[ -z "${PATCH_UUID}" || "${PANEL_UUID}" == "${PATCH_UUID}" ]] || stop_permanent "panel_uuid_mismatch"
}

_confirm_and_finish() {
  local panel="$1" patched="$2"
  _load_panel_fields "${panel}"
  if [[ "${patched}" == "yes" ]]; then
    _require_patched_uuid
  fi
  if [[ "$(_period_status "${panel}")" == "period_closed" ]]; then
    if [[ "${patched}" == "yes" && "${PANEL_LIMIT}" == "${APPLIED_LIMIT}" ]]; then
      _note_period_crossed
    fi
    stop_permanent "period_closed"
  fi
  _require_period "${panel}"
  _require_expire_future "${PANEL_EXPIRE}"
  [[ "${PANEL_LIMIT}" == "${APPLIED_LIMIT}" ]] || stop_permanent "limit_not_applied"
  STAGE="applied"
  log "base_limit_bytes=${BASE_LIMIT} applied_limit_bytes=${PANEL_LIMIT} reason=applied"
  echo "RESULT level=ok reason=applied" >&2
  exit 0
}

_panel_optional() {
  local dest code
  dest="$(_tmp)" || return 1
  code="$(_curl_code "${dest}" -H "$(_auth_header)" \
    "${PANEL_URL}/api/users/by-username/us_${PARENT_US_ID}")"
  if [[ "${code}" == "transport" || "${code}" =~ ^5 ]]; then
    stop_temporary "panel_unavailable"
  fi
  if [[ "${code}" == "404" ]]; then
    echo ""
    return 0
  fi
  if [[ "${code}" != "200" ]]; then
    stop_temporary "panel_read_${code}"
  fi
  jq -e -c '.response | type == "object"' "${dest}" >/dev/null || stop_temporary "panel_malformed"
  echo "${dest}"
}

_other_live_pack() {
  local now_iso="$1" rows withdraws
  rows="${ADDON_SERVICES_JSON#__SHM_JSON__}"
  withdraws="${ADDON_WITHDRAWS_JSON#__SHM_JSON__}"
  if [[ -z "${rows//[[:space:]]/}" ]]; then
    rows="[]"
  fi
  if [[ -z "${withdraws//[[:space:]]/}" ]]; then
    withdraws="[]"
  fi
  jq -n -r --arg self "${ADDON_US_ID}" --arg parent "${PARENT_US_ID}" --arg user "${ADDON_USER_ID}" --arg now "${now_iso}" --argjson rows "${rows}" --argjson withdraws "${withdraws}" '
    def as_rows:
      if . == null then error("unreadable")
      elif type == "array" then .
      elif type == "object" and (.user_service_id != null or .withdraw_id != null) then [.]
      elif type == "object" then [.[] | select(type == "object")]
      else error("unreadable") end;
    def cents:
      (if type == "number" then tostring elif type == "string" then . else "" end) as $t
      | if ($t | test("^[0-9]+(\\.[0-9]{1,2})?$")) then
          $t | capture("^(?<w>[0-9]+)(?:\\.(?<f>[0-9]{1,2}))?$") as $p
          | ($p.w | tonumber) * 100 + ((($p.f // "") + "00")[0:2] | tonumber)
        else -1 end;
    ($rows | as_rows) as $list
    | ($withdraws | as_rows) as $paid
    | reduce $list[] as $row ("clear";
        if . == "block" then .
        elif ($row.user_service_id | tostring) == $self then .
        elif ($row.settings.parent_user_service_id | tostring) != $parent then .
        elif ($row.settings | type) != "object"
          or ($row.settings.period_ends_at | type) != "string"
          or (($row.settings.expected_amount | cents) <= 0) then error("unreadable")
        elif ([($row.status // "") | ascii_upcase] | .[0]) as $status
          | ($status != "ACTIVE" and $status != "REMOVED" and $status != "PROGRESS") then .
        else
          [ $paid[] | select((.user_service_id | tostring) == ($row.user_service_id | tostring)) ] as $mine
          | if any($mine[]; (.withdraw_date | type) == "string" and (.withdraw_date | length) > 0 and ((.total | cents) <= 0)) then error("unreadable")
            elif any($mine[];
              (.withdraw_date | type) == "string" and (.withdraw_date | length) > 0
              and (.withdraw_id | type) == "number" and .withdraw_id > 0
              and (.user_id | tostring) == $user
              and (.service_id | tostring) == "17"
              and (.total | cents) == ($row.settings.expected_amount | cents)
            ) and ($row.settings.period_ends_at > $now) then "block"
            else . end
        end
      )
  '
}

_restore_base() {
  local body dest code panel limit uuid
  PATCH_UUID="${PANEL_UUID}"
  body="$(_tmp)"
  jq -n --arg uuid "${PATCH_UUID}" --argjson trafficLimitBytes "${BASE_LIMIT}" \
    '{uuid:$uuid, trafficLimitBytes:$trafficLimitBytes}' >"${body}"
  dest="$(_tmp)"
  code="$(_curl_code "${dest}" -X PATCH -H "$(_auth_header)" \
    -H "Content-Type: application/json" --data-binary @"${body}" \
    "${PANEL_URL}/api/users")"
  panel="$(_panel_optional)" || stop_temporary "panel_io"
  if [[ -z "${panel}" ]]; then
    stop_temporary "panel_unavailable"
  fi
  limit="$(jq -r '.response.trafficLimitBytes | tonumber? // empty' "${panel}")"
  uuid="$(jq -r '.response.uuid // empty' "${panel}")"
  [[ "${uuid}" == "${PATCH_UUID}" ]] || stop_permanent "panel_uuid_mismatch"
  if [[ "${limit}" == "${BASE_LIMIT}" ]]; then
    log "base_limit_bytes=${BASE_LIMIT} restored"
    echo "RESULT level=ok reason=restored" >&2
    exit 0
  fi
  if [[ "${code}" == "transport" || "${code}" =~ ^5 || "${code}" == "408" || "${code}" == "429" || "${code}" != "200" ]]; then
    stop_temporary "panel_unavailable"
  fi
  stop_temporary "limit_not_restored"
}

_finish_remove() {
  local now_epoch end_epoch parent_file parent_code reason panel reset_at reset_epoch decision now_iso
  now_epoch="$(_now_epoch)"
  end_epoch="$(_epoch "${END_AT}")" || stop_temporary "period_bounds"
  if (( now_epoch < end_epoch )); then
    stop_temporary "period_open"
  fi
  parent_file="$(_tmp)" || stop_temporary "shm_io"
  parent_code="$(_curl_code "${parent_file}" -H "session-id: ${SESSION_ID}" \
    "${API_URL}/shm/v1/user/service?user_service_id=${PARENT_US_ID}")"
  if [[ "${parent_code}" == "transport" || "${parent_code}" =~ ^5 ]]; then
    stop_temporary "shm_unavailable"
  fi
  if [[ "${parent_code}" == "200" ]]; then
    reason="$(jq -r --arg parent_id "${PARENT_US_ID}" --arg user "${ADDON_USER_ID}" '
      if (.data | type != "array") or (.data | length != 1) then "parent_not_owned"
      elif (.data[0].user_service_id | tostring) != $parent_id then "parent_not_owned"
      elif (.data[0].user_id | tostring) != $user then "parent_not_owned"
      else "ok" end
    ' "${parent_file}")" || stop_temporary "parent_unreadable"
    [[ "${reason}" == "ok" ]] || stop_permanent "${reason}"
  elif [[ "${parent_code}" != "404" ]]; then
    stop_temporary "shm_parent_${parent_code}"
  fi
  panel="$(_panel_optional)" || stop_temporary "panel_io"
  if [[ -z "${panel}" ]]; then
    if [[ "${parent_code}" == "404" ]]; then
      echo "RESULT level=ok reason=parent_absent" >&2
      exit 0
    fi
    stop_temporary "panel_user_missing"
  fi
  reset_at="$(jq -r 'if (.response.lastTrafficResetAt | type) == "string" then .response.lastTrafficResetAt else "" end' "${panel}")"
  [[ -n "${reset_at}" ]] || stop_temporary "reset_not_confirmed"
  reset_epoch="$(_epoch "${reset_at}")" || stop_temporary "reset_not_confirmed"
  if (( reset_epoch < end_epoch || reset_epoch > now_epoch )); then
    stop_temporary "reset_not_confirmed"
  fi
  now_iso="$(date -u -d "@${now_epoch}" +%Y-%m-%dT%H:%M:%SZ)"
  decision="$(_other_live_pack "${now_iso}")" || stop_temporary "history_unreadable"
  if [[ "${decision}" == "block" ]]; then
    echo "RESULT level=ok reason=newer_pack" >&2
    exit 0
  fi
  [[ "${decision}" == "clear" ]] || stop_temporary "history_unreadable"
  PANEL_USERNAME="$(jq -r '.response.username // empty' "${panel}")"
  PANEL_UUID="$(jq -r '.response.uuid // empty' "${panel}")"
  PANEL_LIMIT="$(jq -r '.response.trafficLimitBytes | tonumber? // empty' "${panel}")"
  [[ "${PANEL_USERNAME}" == "us_${PARENT_US_ID}" ]] || stop_permanent "panel_username_mismatch"
  [[ "${PANEL_UUID}" =~ ^[0-9a-fA-F-]{36}$ ]] || stop_temporary "panel_malformed"
  [[ "${PANEL_LIMIT}" =~ ^[0-9]+$ ]] || stop_temporary "panel_malformed"
  if [[ "${PANEL_LIMIT}" == "${BASE_LIMIT}" ]]; then
    echo "RESULT level=ok reason=already_base" >&2
    exit 0
  fi
  if [[ "${PANEL_LIMIT}" != "${APPLIED_LIMIT}" ]]; then
    echo "RESULT level=ok reason=limit_kept" >&2
    exit 0
  fi
  _restore_base
}

command -v curl >/dev/null 2>&1 || stop_permanent "curl_missing"
command -v jq >/dev/null 2>&1 || stop_permanent "jq_missing"
command -v date >/dev/null 2>&1 || stop_permanent "date_missing"
EVENT="$(_scalar "${_RAW_EVENT}")" || stop_permanent "bad_event"
SESSION_ID="$(_scalar "${_RAW_SESSION}")" || stop_permanent "bad_session"
API_URL="$(_scalar "${_RAW_API}")" || stop_permanent "bad_api_url"
PANEL_URL="$(_scalar "${_RAW_PANEL}")" || stop_permanent "bad_panel_url"
REMNAWAVE_API_TOKEN="$(_scalar "${_RAW_TOKEN}")" || stop_permanent "panel_token_missing"
ADDON_US_ID="$(_scalar "${_RAW_US}")" || stop_permanent "bad_addon_id"
ADDON_USER_ID="$(_scalar "${_RAW_USER}")" || stop_permanent "bad_user_id"
ADDON_SERVICE_ID="$(_scalar "${_RAW_SERVICE}")" || stop_permanent "unexpected_service"
ADDON_CATEGORY="$(_scalar "${_RAW_CATEGORY}")" || stop_permanent "unexpected_category"
CATALOG_COST="$(_scalar "${_RAW_COST}")" || stop_permanent "bad_catalog_cost"
[[ "${EVENT}" == "CREATE" || "${EVENT}" == "REMOVE" ]] || stop_permanent "unexpected_event"
[[ "${ADDON_US_ID}" =~ ^[1-9][0-9]*$ ]] || stop_permanent "bad_addon_id"
[[ "${ADDON_USER_ID}" =~ ^[1-9][0-9]*$ ]] || stop_permanent "bad_user_id"
[[ "${ADDON_SERVICE_ID}" == "${EXPECTED_SERVICE_ID}" ]] || stop_permanent "unexpected_service"
[[ "${ADDON_CATEGORY}" == "${EXPECTED_CATEGORY}" ]] || stop_permanent "unexpected_category"

SETTINGS_JSON="$(_json_or_null "${SETTINGS_JSON}")" || stop_permanent "bad_settings"
CATALOG_JSON="$(_json_or_null "${CATALOG_JSON}")" || stop_permanent "bad_catalog"
WITHDRAW_JSON="$(_json_or_null "${WITHDRAW_JSON}")" || stop_temporary "withdraw_unreadable"
jq -e '
  .traffic_addon.brand == "vff"
  and .traffic_addon.parent_category == "vpn-mz-test"
  and (.traffic_addon.parent_service_ids | type == "array" and length > 0)
  and (.traffic_addon.parent_service_ids | all(type == "number" and . > 0))
  and (.traffic_addon.bytes | type == "number" and . > 0)
  and .traffic_addon.strategy == "MONTH"
  and .traffic_addon.period == "remnawave_monthly_reset"
  and .traffic_addon.reset_cron == "20 0 1 * *"
  and .traffic_addon.reset_timezone == "UTC"
  and .traffic_addon.max_purchases_per_period == 1
' >/dev/null <<<"${CATALOG_JSON}" || stop_permanent "unsupported_catalog"

PURCHASE_ID="$(jq -r '.purchase_id // empty' <<<"${SETTINGS_JSON}")"
PARENT_US_ID="$(jq -r '.parent_user_service_id // empty' <<<"${SETTINGS_JSON}")"
PERIOD="$(jq -r '.period // empty' <<<"${SETTINGS_JSON}")"
[[ "${PURCHASE_ID}" =~ ^[0-9a-f]{32}$ ]] || stop_permanent "bad_purchase_id"
[[ "${PARENT_US_ID}" =~ ^[1-9][0-9]*$ ]] || stop_permanent "bad_parent_id"
[[ "${PERIOD}" =~ ^[0-9]{4}-[0-9]{2}$ ]] || stop_permanent "bad_period"
START_AT="$(date -u -d "${PERIOD}-01 00:20:00 UTC" +'%Y-%m-%dT%H:%M:%SZ')"
END_AT="$(date -u -d "${PERIOD}-01 00:20:00 UTC + 1 month" +'%Y-%m-%dT%H:%M:%SZ')"
jq -e -n --argjson row "${SETTINGS_JSON}" --arg start "${START_AT}" --arg end "${END_AT}" --argjson max "${MAX_BYTES}" '
  def safeint: type == "number" and (floor == .) and . > 0 and . <= $max;
  def money: if type == "number" then tostring elif type == "string" then . else "" end;
  $row | (.base_limit_bytes | safeint)
    and (.addon_bytes | safeint)
    and (.applied_limit_bytes | safeint)
    and (.applied_limit_bytes == (.base_limit_bytes + .addon_bytes))
    and .period_starts_at == $start
    and .period_ends_at == $end
    and ((.expected_amount | money) | test("^[0-9]+(\\.[0-9]{1,2})?$") and (tonumber > 0))
' >/dev/null || stop_permanent "bad_settings"
BASE_LIMIT="$(jq -r '.base_limit_bytes' <<<"${SETTINGS_JSON}")"
ADDON_LIMIT="$(jq -r '.addon_bytes' <<<"${SETTINGS_JSON}")"
APPLIED_LIMIT="$(jq -r '.applied_limit_bytes' <<<"${SETTINGS_JSON}")"

STAGE="payment"
log "catalog_cost=${CATALOG_COST} addon_bytes=${ADDON_LIMIT}"
paid="$(jq -r --argjson settings "${SETTINGS_JSON}" --arg us "${ADDON_US_ID}" --arg user "${ADDON_USER_ID}" --arg service "${EXPECTED_SERVICE_ID}" '
  def cents:
    (if type == "number" then tostring elif type == "string" then . else "" end) as $t
    | if ($t | test("^[0-9]+(\\.[0-9]{1,2})?$")) then
        $t | capture("^(?<w>[0-9]+)(?:\\.(?<f>[0-9]{1,2}))?$") as $p
        | ($p.w | tonumber) * 100 + ((($p.f // "") + "00")[0:2] | tonumber)
      else -1 end;
  if (.withdraw_date | type != "string" or length == 0) then "unpaid"
  elif (.user_service_id | tostring) != $us
    or (.user_id | tostring) != $user
    or (.service_id | tostring) != $service
    or (.withdraw_id | type != "number" or . <= 0)
    or ((.total | cents) <= 0)
    or ((.total | cents) != ($settings.expected_amount | cents)) then "mismatch"
  else "ok" end
' <<<"${WITHDRAW_JSON}")" || stop_temporary "withdraw_unreadable"
case "${paid}" in
  ok) ;;
  unpaid) stop_temporary "payment_not_confirmed" ;;
  *) stop_permanent "payment_mismatch" ;;
esac

if [[ "${EVENT}" == "REMOVE" ]]; then
  _finish_remove
fi

STAGE="period_window"
_require_period ""
STAGE="parent"
_require_parent
STAGE="panel"
PANEL_FILE="$(_panel_file)"
_load_panel_fields "${PANEL_FILE}"
_require_expire_future "${PANEL_EXPIRE}"
_require_period "${PANEL_FILE}"
log "used_traffic_bytes=$(jq -r '.response.usedTrafficBytes // .response.userTraffic.usedTrafficBytes // "unknown"' "${PANEL_FILE}") panel_status=${PANEL_STATUS} base_limit_bytes=${BASE_LIMIT} applied_limit_bytes=${APPLIED_LIMIT}"

STAGE="before_patch"
_require_parent
PANEL_FILE="$(_panel_file)"
_load_panel_fields "${PANEL_FILE}"
_require_expire_future "${PANEL_EXPIRE}"
_require_period "${PANEL_FILE}"
if [[ "${PANEL_LIMIT}" == "${APPLIED_LIMIT}" ]]; then
  _confirm_and_finish "${PANEL_FILE}" "no"
fi
if [[ "${PANEL_LIMIT}" != "${BASE_LIMIT}" ]]; then
  stop_permanent "limit_conflict"
fi

STAGE="patch"
PATCH_UUID="${PANEL_UUID}"
patch_body="$(_tmp)"
jq -n --arg uuid "${PATCH_UUID}" --argjson trafficLimitBytes "${APPLIED_LIMIT}" \
  '{uuid:$uuid, trafficLimitBytes:$trafficLimitBytes}' >"${patch_body}"
patch_dest="$(_tmp)"
patch_code="$(_curl_code "${patch_dest}" -X PATCH -H "$(_auth_header)" \
  -H "Content-Type: application/json" --data-binary @"${patch_body}" \
  "${PANEL_URL}/api/users")"
PANEL_FILE="$(_panel_file)"
_load_panel_fields "${PANEL_FILE}"
_require_patched_uuid
if [[ "$(_period_status "${PANEL_FILE}")" == "period_closed" && "${PANEL_LIMIT}" == "${APPLIED_LIMIT}" ]]; then
  _note_period_crossed
fi
if [[ "${patch_code}" == "transport" || "${patch_code}" =~ ^5 || "${patch_code}" == "408" || "${patch_code}" == "429" ]]; then
  if [[ "${PANEL_LIMIT}" == "${APPLIED_LIMIT}" ]]; then
    _confirm_and_finish "${PANEL_FILE}" "yes"
  fi
  stop_temporary "panel_unavailable"
fi
if [[ "${patch_code}" != "200" ]]; then
  stop_permanent "panel_rejected"
fi
STAGE="confirm"
_confirm_and_finish "${PANEL_FILE}" "yes"
