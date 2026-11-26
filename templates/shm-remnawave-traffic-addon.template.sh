#!/bin/bash
#
# remnawave_traffic_addon
#
# CREATE для услуги «Дополнительный трафик 300 ГБ». Повышает лимит уже
# существующего пользователя Remnawave us_<parent_user_service_id>.
# Нового пользователя не создаёт и срок подписки не меняет.
#
# Снимок покупки подписан snapshot_mac. Ключ берётся из настроек сервера
# и в storage не хранится. TTL, version и write_id не заменяют CAS.
#
# Успех — только код 0 после записи APPLIED и повторного чтения панели.
# TRAFFIC_ADDON_CLOCK_FILE предназначен для тестов. В Production его нет.
#
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
_RAW_SNAPSHOT="$(cat <<'__SHM_SNAP__'
__SHM_JSON__{{ toJson(server.settings.traffic_addon.snapshot_key) }}
__SHM_SNAP__
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

EXPECTED_SERVICE_ID="17"
EXPECTED_CATEGORY="traffic-add-vff"
LOCK_TTL_SECONDS="120"
MAX_BYTES="9007199254740991"

PURCHASE_ID=""
PARENT_US_ID=""
PERIOD=""
PERIOD_KEY=""
STORAGE_KEY=""
LOCK_KEY=""
LOCK_ID=""
LOCK_HELD="0"
STAGE="start"
WORK_DIR=""
CANONICAL=""
START_AT=""
END_AT=""
LOCK_READ_STATUS=""
LOCK_READ_ID=""
LOCK_READ_UNTIL="0"

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

_auth_header() {
  [[ -n "${REMNAWAVE_API_TOKEN:-}" ]] || stop_permanent "panel_token_missing"
  echo "Authorization: Bearer ${REMNAWAVE_API_TOKEN}"
}

_max_time() {
  local limit="${REMNAWAVE_STORAGE_MAX_TIME:-20}"
  [[ "${limit}" =~ ^[1-9][0-9]*$ ]] || stop_permanent "bad_http_timeout"
  echo "${limit}"
}

_tmp() {
  [[ -n "${WORK_DIR}" && -d "${WORK_DIR}" ]] || stop_temporary "workdir_missing"
  mktemp "${WORK_DIR}/item.XXXXXX"
}

# TLS is verified with the system trust store. Both bill.portalbase.link
# and remna.vpn-for-friends.com present a chain that verifies there.
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

_canonical() {
  jq -c -n --slurpfile s "$1" '{
    purchase_id:$s[0].purchase_id,
    user_id:$s[0].user_id,
    parent_user_service_id:$s[0].parent_user_service_id,
    period:$s[0].period,
    period_starts_at:$s[0].period_starts_at,
    period_ends_at:$s[0].period_ends_at,
    expected_amount:$s[0].expected_amount,
    base_limit_bytes:$s[0].base_limit_bytes,
    addon_bytes:$s[0].addon_bytes,
    applied_limit_bytes:$s[0].applied_limit_bytes
  }'
}

_snapshot_mac() {
  local canon="$1"
  printf '%s' "${canon}" | openssl dgst -sha256 -hmac "${SNAPSHOT_KEY}" -hex | awk '{print tolower($NF)}'
}

# Prints ok or a permanent reason. Does not trust a changed snapshot.
_contract_reason() {
  local src="$1" canon mac want reason
  canon="$(_canonical "${src}")"
  reason="$(jq -r -n --argjson row "$(jq -c . "${src}")" --arg purchase "${PURCHASE_ID}" --arg parent "${PARENT_US_ID}" --arg period "${PERIOD}" --arg user "${ADDON_USER_ID}" --arg start "${START_AT}" --arg end "${END_AT}" --argjson max "${MAX_BYTES}" '
    def safeint: type == "number" and (floor == .) and . > 0 and . <= $max;
    def money: type == "string" and test("^[0-9]+\\.[0-9]{2}$") and ((tonumber * 100 | round) > 0);
    $row | if .purchase_id != $purchase then "storage_contract"
    elif (.user_id | tostring) != $user then "storage_contract"
    elif (.parent_user_service_id | tostring) != $parent then "storage_contract"
    elif .period != $period then "storage_contract"
    elif .period_starts_at != $start or .period_ends_at != $end then "period_bounds"
    elif (.expected_amount == null or .expected_amount == "") then "expected_amount_missing"
    elif (.expected_amount | money | not) then "expected_amount_missing"
    elif (.base_limit_bytes | safeint | not) then "storage_contract"
    elif (.addon_bytes | safeint | not) then "storage_contract"
    elif (.applied_limit_bytes | safeint | not) then "storage_contract"
    elif .applied_limit_bytes != (.base_limit_bytes + .addon_bytes) then "storage_contract"
    else "ok" end
  ')"
  [[ "${reason}" == "ok" ]] || { echo "${reason}"; return 0; }
  mac="$(jq -r '.snapshot_mac // empty' "${src}")"
  want="$(_snapshot_mac "${canon}")"
  [[ "${mac}" == "${want}" ]] || { echo "snapshot_mismatch"; return 0; }
  if [[ -n "${CANONICAL}" && "${canon}" != "${CANONICAL}" ]]; then
    echo "storage_contract"
    return 0
  fi
  echo "ok"
}

_require_contract() {
  local reason
  reason="$(_contract_reason "$1")"
  [[ "${reason}" == "ok" ]] || stop_permanent "${reason}"
  if [[ -z "${CANONICAL}" ]]; then
    CANONICAL="$(_canonical "$1")"
  fi
}

_storage_get() {
  local name="$1" dest="$2"
  _curl_code "${dest}" -H "session-id: ${SESSION_ID}" \
    "${API_URL}/shm/v1/storage/manage/${name}"
}

_storage_write() {
  local method="$1" name="$2" payload="$3" dest="$4"
  _curl_code "${dest}" -X "${method}" \
    -H "session-id: ${SESSION_ID}" \
    -H "Content-Type: application/json; charset=utf-8" \
    --data-binary @"${payload}" \
    "${API_URL}/shm/v1/storage/manage/${name}"
}

_load_purchase() {
  local dest code
  dest="$(_tmp)" || stop_temporary "storage_io"
  code="$(_storage_get "${STORAGE_KEY}" "${dest}")"
  if [[ "${code}" == "transport" || "${code}" =~ ^5 || "${code}" == "401" || "${code}" == "403" ]]; then
    stop_temporary "storage_unavailable"
  fi
  if [[ "${code}" == "404" ]]; then
    stop_permanent "storage_missing"
  fi
  if [[ "${code}" != "200" ]]; then
    stop_temporary "storage_read_${code}"
  fi
  jq -e -c . <"${dest}" >"${dest}.json" || stop_temporary "storage_malformed"
  mv "${dest}.json" "${dest}"
  echo "${dest}"
}

_save_purchase() {
  local src="$1" body dest code write_id same
  body="$(_tmp)" || stop_temporary "storage_io"
  if [[ -r /proc/sys/kernel/random/uuid ]]; then
    write_id="$(tr -d '-' < /proc/sys/kernel/random/uuid)"
  else
    write_id="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
  fi
  jq -c --arg write_id "${write_id}" \
    '.write_id = $write_id | .version = ((.version // 0) + 1) | .state_changed_at = (now | strftime("%Y-%m-%dT%H:%M:%SZ"))' \
    "${src}" >"${body}" || stop_temporary "storage_malformed"
  dest="$(_tmp)" || stop_temporary "storage_io"
  code="$(_storage_write POST "${STORAGE_KEY}" "${body}" "${dest}")"
  if [[ "${code}" == "transport" || "${code}" =~ ^5 ]]; then
    stop_temporary "storage_write"
  fi
  if [[ "${code}" != "200" ]]; then
    stop_temporary "storage_write_${code}"
  fi
  dest="$(_load_purchase)"
  same="$(jq -n --slurpfile a "${body}" --slurpfile b "${dest}" '($a[0] | tojson) == ($b[0] | tojson)')"
  [[ "${same}" == "true" ]] || stop_temporary "storage_conflict"
  _require_contract "${dest}"
  cp "${dest}" "${src}"
}

# Only HTTP 404 is a missing lock. Every other read failure blocks takeover.
_lock_read() {
  local dest code
  LOCK_READ_STATUS="unavailable"
  LOCK_READ_ID=""
  LOCK_READ_UNTIL="0"
  dest="$(_tmp)" || { LOCK_READ_STATUS="unavailable"; return 0; }
  code="$(_storage_get "${LOCK_KEY}" "${dest}")"
  if [[ "${code}" == "404" ]]; then
    LOCK_READ_STATUS="missing"
    return 0
  fi
  if [[ "${code}" != "200" ]]; then
    LOCK_READ_STATUS="unavailable"
    return 0
  fi
  jq -e 'type == "object" and (.lock_id | type == "string") and (.until | type == "number")' \
    "${dest}" >/dev/null 2>&1 || { LOCK_READ_STATUS="unavailable"; return 0; }
  LOCK_READ_ID="$(jq -r '.lock_id' "${dest}")"
  LOCK_READ_UNTIL="$(jq -r '.until | floor' "${dest}")"
  [[ "${LOCK_READ_UNTIL}" =~ ^[0-9]+$ ]] || { LOCK_READ_STATUS="unavailable"; return 0; }
  LOCK_READ_STATUS="ok"
}

_require_lock() {
  _lock_read
  if [[ "${LOCK_READ_STATUS}" != "ok" ]]; then
    stop_temporary "lock_unavailable"
  fi
  local now_epoch
  now_epoch="$(_now_epoch)"
  if [[ "${LOCK_READ_ID}" != "${LOCK_ID}" || "${LOCK_READ_UNTIL}" -le "${now_epoch}" ]]; then
    stop_temporary "lock_lost"
  fi
}

# shellcheck disable=SC2317
_release_lock() {
  [[ "${LOCK_HELD}" == "1" ]] || return 0
  if [[ -z "${WORK_DIR}" || ! -d "${WORK_DIR}" ]]; then
    LOCK_HELD="0"
    return 0
  fi
  _lock_read || true
  if [[ "${LOCK_READ_STATUS}" != "ok" || "${LOCK_READ_ID}" != "${LOCK_ID}" ]]; then
    log "lock_release_skipped reason=${LOCK_READ_STATUS}"
    LOCK_HELD="0"
    return 0
  fi
  local dest code
  dest="$(_tmp)" || { LOCK_HELD="0"; return 0; }
  code="$(_curl_code "${dest}" -X DELETE -H "session-id: ${SESSION_ID}" \
    "${API_URL}/shm/v1/storage/manage/${LOCK_KEY}")" || true
  if [[ "${code}" != "200" ]]; then
    log "lock_release_skipped reason=delete_${code}"
  fi
  LOCK_HELD="0"
}

# shellcheck disable=SC2317
_on_exit() {
  _release_lock || true
  if [[ -n "${WORK_DIR}" && -d "${WORK_DIR}" ]]; then
    rm -rf "${WORK_DIR}"
  fi
}
trap _on_exit EXIT
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/traffic-addon.XXXXXX")"

_acquire_lock() {
  local payload dest code now_epoch
  payload="$(_tmp)" || stop_temporary "storage_io"
  now_epoch="$(_now_epoch)"
  jq -n --arg lock_id "${LOCK_ID}" --arg purchase_id "${PURCHASE_ID}" \
    --arg handler "remnawave_traffic_addon" --argjson parent "${PARENT_US_ID}" \
    --argjson until "$((now_epoch + LOCK_TTL_SECONDS))" \
    '{lock_id:$lock_id, purchase_id:$purchase_id, handler:$handler, parent_user_service_id:$parent, until:$until}' \
    >"${payload}"
  dest="$(_tmp)" || stop_temporary "storage_io"
  code="$(_storage_write PUT "${LOCK_KEY}" "${payload}" "${dest}")"
  if [[ "${code}" == "200" ]]; then
    _lock_read
    if [[ "${LOCK_READ_STATUS}" == "ok" && "${LOCK_READ_ID}" == "${LOCK_ID}" ]]; then
      LOCK_HELD="1"
      return 0
    fi
    stop_temporary "lock_unavailable"
  fi
  if [[ "${code}" == "transport" || "${code}" =~ ^5 || "${code}" == "401" || "${code}" == "403" ]]; then
    stop_temporary "lock_unavailable"
  fi
  # SHM Storage::add reports an existing key as HTTP 400. Nothing else is takeover.
  if [[ "${code}" != "400" && "${code}" != "409" ]]; then
    stop_temporary "lock_unavailable"
  fi
  _lock_read
  if [[ "${LOCK_READ_STATUS}" != "ok" ]]; then
    stop_temporary "lock_unavailable"
  fi
  if [[ "${LOCK_READ_ID}" == "${LOCK_ID}" ]]; then
    LOCK_HELD="1"
    return 0
  fi
  now_epoch="$(_now_epoch)"
  if [[ "${LOCK_READ_UNTIL}" -gt "${now_epoch}" ]]; then
    stop_temporary "lock_held"
  fi
  dest="$(_tmp)" || stop_temporary "storage_io"
  code="$(_storage_write POST "${LOCK_KEY}" "${payload}" "${dest}")"
  [[ "${code}" == "200" ]] || stop_temporary "lock_unavailable"
  _lock_read
  if [[ "${LOCK_READ_STATUS}" != "ok" || "${LOCK_READ_ID}" != "${LOCK_ID}" ]]; then
    stop_temporary "lock_unavailable"
  fi
  LOCK_HELD="1"
}

_parent_reason_file() {
  local parent="$1"
  jq -r --arg parent_id "${PARENT_US_ID}" --arg user "${ADDON_USER_ID}" --slurpfile cat <(printf '%s' "${CATALOG_JSON}") '
    if (.data | type != "array") or (.data | length == 0) then "parent_not_owned"
    elif (.data | length != 1) or ((.data[0].user_service_id | tostring) != $parent_id) then "parent_not_owned"
    elif (.data[0].user_id | tostring) != $user then "parent_not_owned"
    elif .data[0].status != "ACTIVE" then "parent_not_active"
    elif .data[0].service.category != $cat[0].traffic_addon.parent_category then "parent_category_mismatch"
    elif ((.data[0].service_id | tonumber) as $id | $cat[0].traffic_addon.parent_service_ids | index($id) == null) then "tariff_not_allowed"
    else "ok" end
  ' "${parent}"
}

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
  reason="$(_parent_reason_file "${dest}")"
  [[ "${reason}" == "ok" ]] || stop_permanent "${reason}"
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

# open, period_closed, period_not_open, period_not_confirmed, created_at_in_future
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
  local raw="$1" state
  state="$(_expire_state "${raw}")"
  case "${state}" in
    future) return 0 ;;
    past) stop_permanent "subscription_expired" ;;
    *) stop_permanent "expire_invalid" ;;
  esac
}

_money_cents() {
  jq -nr --arg v "$1" '($v | tonumber) * 100 | round'
}

_apply_payment() {
  local src="$1" paid expected total stored_wd stored_us stored_amount
  paid="$(jq -e --arg us "${ADDON_US_ID}" --arg user "${ADDON_USER_ID}" --arg service "${EXPECTED_SERVICE_ID}" '
    (.withdraw_date | type == "string" and length > 0)
    and (.user_service_id | tostring) == $us
    and (.user_id | tostring) == $user
    and (.service_id | tostring) == $service
    and (.withdraw_id | type == "number" and . > 0)
    and (.total | tonumber) > 0
  ' >/dev/null <<<"${WITHDRAW_JSON}" && echo yes || echo no)"
  [[ "${paid}" == "yes" ]] || stop_temporary "payment_not_confirmed"
  expected="$(jq -r '.expected_amount' "${src}")"
  total="$(jq -r '.total' <<<"${WITHDRAW_JSON}")"
  [[ "$(_money_cents "${expected}")" == "$(_money_cents "${total}")" ]] || stop_permanent "amount_mismatch"
  stored_wd="$(jq -r '.withdraw_id // empty' "${src}")"
  stored_us="$(jq -r '.user_service_id // empty' "${src}")"
  stored_amount="$(jq -r '.amount // empty' "${src}")"
  if [[ -n "${stored_wd}" && "${stored_wd}" != "$(jq -r '.withdraw_id' <<<"${WITHDRAW_JSON}")" ]]; then
    stop_permanent "withdraw_mismatch"
  fi
  if [[ -n "${stored_us}" && "${stored_us}" != "${ADDON_US_ID}" ]]; then
    stop_permanent "user_service_mismatch"
  fi
  if [[ -n "${stored_amount}" && "$(_money_cents "${stored_amount}")" != "$(_money_cents "${total}")" ]]; then
    stop_permanent "amount_mismatch"
  fi
  jq -n --argjson wd "${WITHDRAW_JSON}" --slurpfile st "${src}" --arg amount "$(
    jq -nr --argjson total "${total}" '($total | tonumber) * 100 | round | . as $c | (($c / 100 | floor | tostring) + "." + (if ($c % 100) < 10 then "0" else "" end) + ($c % 100 | tostring))'
  )" '
    $st[0]
    | if .withdraw_id == null or .withdraw_id == "" then .withdraw_id = $wd.withdraw_id else . end
    | if .user_service_id == null or .user_service_id == "" then .user_service_id = ($wd.user_service_id | tonumber) else . end
    | if .amount == null or .amount == "" then .amount = $amount else . end
    | if .state == "RESERVED" or .state == "ORDER_UNKNOWN" then .state = "PAID" else . end
  ' >"${src}.paid"
  mv "${src}.paid" "${src}"
}

_state_gate() {
  local state
  state="$(jq -r '.state // empty' "$1")"
  case "${state}" in
    EXPIRED) stop_permanent "state_expired" ;;
    REFUNDED) stop_permanent "state_refunded" ;;
    FAILED) stop_permanent "state_failed" ;;
    RESERVED|ORDER_UNKNOWN|PAID|APPLIED) ;;
    *) stop_permanent "state_unknown" ;;
  esac
}

_reload_purchase() {
  local fresh="$1"
  local loaded
  loaded="$(_load_purchase)"
  _state_gate "${loaded}"
  _require_contract "${loaded}"
  cp "${loaded}" "${fresh}"
}

_note_for_reconciler() {
  local src="$1" error="$2"
  local state
  state="$(jq -r '.state // empty' "${src}")"
  case "${state}" in
    EXPIRED|REFUNDED|FAILED) return 0 ;;
  esac
  jq --arg error "${error}" '.last_error = $error | if .apply_proof != null then .apply_proof.phase = "patching" else . end' \
    "${src}" >"${src}.note"
  mv "${src}.note" "${src}"
  _save_purchase "${src}" || true
}

_finish_applied() {
  local record="$1" panel="$2"
  local username strategy uuid expire limit want_limit expire_state observed_expire
  _require_lock
  if [[ "$(_period_status "${panel}")" == "period_closed" ]]; then
    _note_for_reconciler "${record}" "period_crossed_after_patch"
    stop_permanent "period_crossed"
  fi
  _require_period "${panel}"
  _reload_purchase "${record}"
  state="$(jq -r '.state // empty' "${record}")"
  case "${state}" in
    PAID|APPLIED) ;;
    *) stop_permanent "state_${state,,}" ;;
  esac
  _require_parent
  username="$(jq -r '.response.username // empty' "${panel}")"
  strategy="$(jq -r '.response.trafficLimitStrategy // empty' "${panel}")"
  uuid="$(jq -r '.response.uuid // empty' "${panel}")"
  expire="$(jq -r '.response.expireAt // empty' "${panel}")"
  limit="$(jq -r '.response.trafficLimitBytes | tonumber' "${panel}")"
  want_limit="$(jq -r '.applied_limit_bytes' "${record}")"
  [[ "${username}" == "us_${PARENT_US_ID}" ]] || stop_permanent "panel_username_mismatch"
  [[ "${uuid}" == "$(jq -r '.apply_proof.remnawave_uuid' "${record}")" ]] || stop_permanent "panel_uuid_mismatch"
  [[ "${strategy}" == "MONTH" && "${strategy}" == "$(jq -r '.apply_proof.strategy' "${record}")" ]] || stop_permanent "strategy_changed"
  [[ "${limit}" == "${want_limit}" ]] || stop_permanent "limit_not_applied"
  expire_state="$(_expire_state "${expire}")"
  if [[ "${expire_state}" != "future" ]]; then
    _note_for_reconciler "${record}" "parent_expire_changed"
    stop_temporary "parent_expire_changed"
  fi
  observed_expire="$(jq -r '.apply_proof.expire_at' "${record}")"
  if [[ "${expire}" != "${observed_expire}" ]]; then
    log "parent_expire_changed observed=${expire}"
  fi
  jq --argjson after "${limit}" --arg expire "${expire}" \
    '.state = "APPLIED" | .last_error = null | .apply_proof.phase = "confirmed" | .apply_proof.observed_limit_after = $after | .apply_proof.expire_at = $expire' \
    "${record}" >"${record}.applied"
  mv "${record}.applied" "${record}"
  STAGE="applied"
  _save_purchase "${record}"
  log "base_limit_bytes=$(jq -r '.base_limit_bytes' "${record}") applied_limit_bytes=${limit} reason=applied"
  echo "RESULT level=ok reason=applied" >&2
  exit 0
}

_scalar() {
  local raw="$1"
  raw="${raw#__SHM_JSON__}"
  jq -er . <<<"${raw}"
}

command -v curl >/dev/null 2>&1 || stop_permanent "curl_missing"
command -v jq >/dev/null 2>&1 || stop_permanent "jq_missing"
command -v date >/dev/null 2>&1 || stop_permanent "date_missing"
command -v openssl >/dev/null 2>&1 || stop_permanent "openssl_missing"
EVENT="$(_scalar "${_RAW_EVENT}")" || stop_permanent "bad_event"
SESSION_ID="$(_scalar "${_RAW_SESSION}")" || stop_permanent "bad_session"
API_URL="$(_scalar "${_RAW_API}")" || stop_permanent "bad_api_url"
PANEL_URL="$(_scalar "${_RAW_PANEL}")" || stop_permanent "bad_panel_url"
REMNAWAVE_API_TOKEN="$(_scalar "${_RAW_TOKEN}")" || stop_permanent "panel_token_missing"
SNAPSHOT_KEY="$(_scalar "${_RAW_SNAPSHOT}")" || stop_permanent "snapshot_key_missing"
ADDON_US_ID="$(_scalar "${_RAW_US}")" || stop_permanent "bad_addon_id"
ADDON_USER_ID="$(_scalar "${_RAW_USER}")" || stop_permanent "bad_user_id"
ADDON_SERVICE_ID="$(_scalar "${_RAW_SERVICE}")" || stop_permanent "unexpected_service"
ADDON_CATEGORY="$(_scalar "${_RAW_CATEGORY}")" || stop_permanent "unexpected_category"
CATALOG_COST="$(_scalar "${_RAW_COST}")" || stop_permanent "bad_catalog_cost"
[[ -n "${SNAPSHOT_KEY}" && "${SNAPSHOT_KEY}" != "null" ]] || stop_permanent "snapshot_key_missing"
[[ "${EVENT}" == "CREATE" ]] || stop_permanent "unexpected_event"
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
PERIOD_KEY="${PERIOD//-/}"
STORAGE_KEY="ta-${PARENT_US_ID}-${PERIOD_KEY}"
LOCK_KEY="tl-${PARENT_US_ID}"
[[ "${#STORAGE_KEY}" -le 32 && "${#LOCK_KEY}" -le 32 ]] || stop_permanent "storage_key_too_long"
START_AT="$(date -u -d "${PERIOD}-01 00:20:00 UTC" +'%Y-%m-%dT%H:%M:%SZ')"
END_AT="$(date -u -d "${PERIOD}-01 00:20:00 UTC + 1 month" +'%Y-%m-%dT%H:%M:%SZ')"
if [[ -r /proc/sys/kernel/random/uuid ]]; then
  LOCK_ID="$(tr -d '-' < /proc/sys/kernel/random/uuid)"
else
  LOCK_ID="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
fi
[[ "${LOCK_ID}" =~ ^[0-9a-fA-F]{32}$ ]] || stop_permanent "lock_id_unavailable"

STAGE="load_storage"
record="$(_load_purchase)"
_state_gate "${record}"
_require_contract "${record}"
STAGE="period_window"
_require_period ""
STAGE="payment"
log "catalog_cost=${CATALOG_COST}"
_apply_payment "${record}"
STAGE="parent"
_require_parent
STAGE="lock"
_acquire_lock
_reload_purchase "${record}"
_apply_payment "${record}"
_require_period ""

STAGE="panel"
panel="$(_panel_file)"
username="$(jq -r '.response.username // empty' "${panel}")"
status="$(jq -r '.response.status // empty' "${panel}")"
strategy="$(jq -r '.response.trafficLimitStrategy // empty' "${panel}")"
uuid="$(jq -r '.response.uuid // empty' "${panel}")"
expire="$(jq -r '.response.expireAt // empty' "${panel}")"
limit="$(jq -r '.response.trafficLimitBytes | tonumber? // empty' "${panel}")"
used="$(jq -r '.response.usedTrafficBytes // .response.userTraffic.usedTrafficBytes // empty' "${panel}")"
[[ "${username}" == "us_${PARENT_US_ID}" ]] || stop_permanent "panel_username_mismatch"
[[ "${strategy}" == "MONTH" ]] || stop_permanent "strategy_mismatch"
[[ "${uuid}" =~ ^[0-9a-fA-F-]{36}$ ]] || stop_temporary "panel_malformed"
[[ "${limit}" =~ ^[0-9]+$ ]] || stop_temporary "panel_malformed"
case "${status}" in
  ACTIVE|LIMITED) ;;
  *) stop_permanent "panel_status" ;;
esac
_require_expire_future "${expire}"
_require_period "${panel}"
log "used_traffic_bytes=${used:-unknown} panel_status=${status} base_limit_bytes=$(jq -r '.base_limit_bytes' "${record}") applied_limit_bytes=$(jq -r '.applied_limit_bytes' "${record}")"

base="$(jq -r '.base_limit_bytes' "${record}")"
applied="$(jq -r '.applied_limit_bytes' "${record}")"
proof_uuid="$(jq -r '.apply_proof.remnawave_uuid // empty' "${record}")"
proof_phase="$(jq -r '.apply_proof.phase // empty' "${record}")"
if [[ "${limit}" == "${applied}" && "${proof_uuid}" == "${uuid}" && ( "${proof_phase}" == "patching" || "${proof_phase}" == "confirmed" ) ]]; then
  STAGE="confirm"
  _finish_applied "${record}" "${panel}"
fi
if [[ "${limit}" != "${base}" ]]; then
  stop_permanent "limit_conflict"
fi

STAGE="before_patch"
_require_lock
_require_parent
panel="$(_panel_file)"
expire="$(jq -r '.response.expireAt // empty' "${panel}")"
limit="$(jq -r '.response.trafficLimitBytes | tonumber? // empty' "${panel}")"
uuid="$(jq -r '.response.uuid // empty' "${panel}")"
strategy="$(jq -r '.response.trafficLimitStrategy // empty' "${panel}")"
[[ "${limit}" == "${base}" ]] || stop_permanent "limit_conflict"
_require_expire_future "${expire}"
_require_period "${panel}"
jq --arg uuid "${uuid}" --arg expire "${expire}" --arg strategy "${strategy}" --argjson before "${limit}" '
  .apply_proof = {remnawave_uuid:$uuid, expire_at:$expire, strategy:$strategy, observed_limit_before:$before, phase:"patching"}
' "${record}" >"${record}.proof"
mv "${record}.proof" "${record}"
_save_purchase "${record}"
_require_period "${panel}"
_require_lock

STAGE="patch"
patch_body="$(_tmp)"
jq -n --arg uuid "${uuid}" --argjson trafficLimitBytes "${applied}" \
  '{uuid:$uuid, trafficLimitBytes:$trafficLimitBytes}' >"${patch_body}"
patch_dest="$(_tmp)"
patch_code="$(_curl_code "${patch_dest}" -X PATCH -H "$(_auth_header)" \
  -H "Content-Type: application/json" --data-binary @"${patch_body}" \
  "${PANEL_URL}/api/users")"
panel="$(_panel_file)"
limit="$(jq -r '.response.trafficLimitBytes | tonumber? // empty' "${panel}")"
if [[ "$(_period_status "${panel}")" == "period_closed" ]]; then
  if [[ "${limit}" == "${applied}" ]]; then
    _note_for_reconciler "${record}" "period_crossed_after_patch"
    stop_permanent "period_crossed"
  fi
  stop_permanent "period_closed"
fi
if [[ "${patch_code}" == "transport" || "${patch_code}" =~ ^5 || "${patch_code}" == "408" || "${patch_code}" == "429" ]]; then
  if [[ "${limit}" == "${applied}" ]]; then
    _finish_applied "${record}" "${panel}"
  fi
  stop_temporary "panel_unavailable"
fi
if [[ "${patch_code}" != "200" ]]; then
  stop_permanent "panel_rejected"
fi
STAGE="confirm"
_finish_applied "${record}" "${panel}"
