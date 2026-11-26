#!/bin/bash
#
# remnawave_traffic_addon
#
# CREATE для услуги «Дополнительный трафик 300 ГБ» (service_id 17,
# категория traffic-add-vff). Повышает лимит уже существующего пользователя
# Remnawave us_<parent_user_service_id>. Нового пользователя не создаёт,
# срок, расход, стратегию и параметры подключения не меняет.
#
# Оплата читается из списания SHM этого экземпляра, а не из состояния storage.
# RESERVED и ORDER_UNKNOWN при оплаченном списании переводятся в PAID.
# Повтор ставит абсолютный applied_limit_bytes, а не сумму с текущим лимитом.
#
# Успех (код 0) только когда пакет предоставлен и запись storage в APPLIED.
# Отказ — код 1 и строка RESULT level=temporary|permanent.
# SHM повторяет FAIL без лимита. Постоянные отказы останавливает будущая сверка.
#
# Период открыт, если lastTrafficResetAt попадает в текущее окно, либо
# сброса ещё не было и createdAt лежит в этом же окне и не в будущем.
# Штатный сброс Remnawave шаблон не вызывает и не меняет.
#
# Ключ tl-<parent_user_service_id> — общий порядок для будущих обработчиков
# этой подписки. TTL и version не являются CAS и гонку не закрывают.
#
# Основной шаблон remnawave этот файл не подменяет и не изменяет.
#
set -euo pipefail

EVENT="{{ event_name }}"
SESSION_ID="{{ user.gen_session.id }}"
API_URL="{{ config.api.url }}"
PANEL_URL="{{ server.settings.remnawave.api }}"
REMNAWAVE_API_TOKEN="{{ server.settings.remnawave.token }}"

ADDON_US_ID="{{ us.id }}"
ADDON_USER_ID="{{ us.user_id }}"
ADDON_SERVICE_ID="{{ us.service_id }}"
ADDON_CATEGORY="{{ us.service.category }}"
CATALOG_COST="{{ us.service.cost }}"

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

PURCHASE_ID=""
PARENT_US_ID=""
PERIOD=""
PERIOD_KEY=""
STORAGE_KEY=""
LOCK_KEY=""
LOCK_ID=""
LOCK_HELD="0"
STAGE="start"

log() {
  echo "[$(date -u +'%F %T')] purchase_id=${PURCHASE_ID:-} parent_user_service_id=${PARENT_US_ID:-} addon_user_service_id=${ADDON_US_ID:-} period=${PERIOD:-} stage=${STAGE} $*" >&2
}

# stderr, not stdout: helpers are often called inside $(...), and a failure
# there would otherwise discard the RESULT line before SHM records the task.
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

# Prints the HTTP code, or "transport". Body is written to the destination.
_curl_code() {
  local dest="$1"
  shift
  local code curl_rc
  code="$(curl -skS --max-time "$(_max_time)" -o "${dest}" -w "%{http_code}" "$@")" && curl_rc=0 || curl_rc=$?
  code="${code//$'\r'/}"
  if [[ "${curl_rc}" -ne 0 || -z "${code}" || "${code}" == "000" ]]; then
    echo "transport"
    return 0
  fi
  echo "${code}"
}

_lock_fields() {
  local dest code
  dest="$(mktemp)" || return 1
  code="$(_storage_get "${LOCK_KEY}" "${dest}")"
  if [[ "${code}" != "200" ]]; then
    rm -f "${dest}"
    printf 'missing\t0\n'
    return 0
  fi
  jq -r '[.lock_id // "missing", (.until // 0 | tostring)] | @tsv' "${dest}"
  rm -f "${dest}"
}

# True only for our unexpired lock. A matching purchase_id with another
# lock_id is a different run and must not enter.
_lock_is_current() {
  local id until now_epoch
  IFS=$'\t' read -r id until < <(_lock_fields) || return 1
  now_epoch="$(_now_epoch)"
  [[ "${id}" == "${LOCK_ID}" && "${until}" =~ ^[0-9]+$ && "${until}" -gt "${now_epoch}" ]]
}

# Invoked by the EXIT trap. ShellCheck does not see that call.
# shellcheck disable=SC2317
_release_lock() {
  local id _until dest code
  [[ "${LOCK_HELD}" == "1" ]] || return 0
  IFS=$'\t' read -r id _until < <(_lock_fields) || id="missing"
  if [[ "${id}" != "${LOCK_ID}" ]]; then
    log "lock_release_skipped reason=owner_mismatch"
    LOCK_HELD="0"
    return 0
  fi
  dest="$(mktemp)" || { LOCK_HELD="0"; return 0; }
  code="$(_curl_code "${dest}" -X DELETE -H "session-id: ${SESSION_ID}" \
    "${API_URL}/shm/v1/storage/manage/${LOCK_KEY}")" || true
  rm -f "${dest}"
  if [[ "${code}" != "200" ]]; then
    log "lock_release_skipped reason=delete_${code}"
  fi
  LOCK_HELD="0"
}
trap _release_lock EXIT

_require_tools() {
  command -v curl >/dev/null 2>&1 || stop_permanent "curl_missing"
  command -v jq >/dev/null 2>&1 || stop_permanent "jq_missing"
  command -v date >/dev/null 2>&1 || stop_permanent "date_missing"
}

_json_or_null() {
  local raw="$1"
  if [[ -z "${raw}" || "${raw}" == "null" ]]; then
    echo "null"
    return 0
  fi
  jq -e -c . >/dev/null <<<"${raw}" || return 1
  jq -c . <<<"${raw}"
}

_epoch() {
  date -u -d "$1" +%s 2>/dev/null || return 1
}

_now_epoch() { date -u +%s; }

_storage_get() {
  local name="$1" dest="$2" code
  code="$(_curl_code "${dest}" -H "session-id: ${SESSION_ID}" \
    "${API_URL}/shm/v1/storage/manage/${name}")"
  echo "${code}"
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
  dest="$(mktemp)" || stop_temporary "storage_io"
  code="$(_storage_get "${STORAGE_KEY}" "${dest}")"
  if [[ "${code}" == "transport" || "${code}" =~ ^5 ]]; then
    rm -f "${dest}"
    stop_temporary "storage_unavailable"
  fi
  if [[ "${code}" == "404" ]]; then
    rm -f "${dest}"
    stop_permanent "storage_missing"
  fi
  if [[ "${code}" != "200" ]]; then
    rm -f "${dest}"
    stop_temporary "storage_read_${code}"
  fi
  jq -e -c . <"${dest}" >"${dest}.json" || {
    rm -f "${dest}" "${dest}.json"
    stop_temporary "storage_malformed"
  }
  mv "${dest}.json" "${dest}"
  echo "${dest}"
}

# $1 is the record file. Increments version and verifies the follow-up read.
_save_purchase() {
  local src="$1" body dest code got
  body="$(mktemp)" || stop_temporary "storage_io"
  jq -c '.version = ((.version // 0) + 1) | .state_changed_at = (now | strftime("%Y-%m-%dT%H:%M:%SZ"))' \
    "${src}" >"${body}" || { rm -f "${body}"; stop_temporary "storage_malformed"; }
  dest="$(mktemp)" || { rm -f "${body}"; stop_temporary "storage_io"; }
  code="$(_storage_write POST "${STORAGE_KEY}" "${body}" "${dest}")"
  rm -f "${dest}"
  if [[ "${code}" == "transport" || "${code}" =~ ^5 ]]; then
    rm -f "${body}"
    stop_temporary "storage_write"
  fi
  if [[ "${code}" != "200" ]]; then
    rm -f "${body}"
    stop_temporary "storage_write_${code}"
  fi
  dest="$(_load_purchase)"
  got="$(jq -r '.version | tostring' "${dest}")"
  local want
  want="$(jq -r '.version | tostring' "${body}")"
  local same
  same="$(jq -e -n --slurpfile a "${body}" --slurpfile b "${dest}" '$a[0].purchase_id == $b[0].purchase_id and ($a[0].version | tostring) == ($b[0].version | tostring)' )" || same="false"
  if [[ "${same}" != "true" || "${got}" != "${want}" ]]; then
    rm -f "${body}" "${dest}"
    stop_temporary "storage_conflict"
  fi
  cp "${dest}" "${src}"
  rm -f "${body}" "${dest}"
}

_acquire_lock() {
  local payload dest code id until now_epoch
  payload="$(mktemp)" || stop_temporary "storage_io"
  now_epoch="$(_now_epoch)"
  jq -n --arg lock_id "${LOCK_ID}" --arg purchase_id "${PURCHASE_ID}" \
    --arg handler "remnawave_traffic_addon" --argjson parent "${PARENT_US_ID}" \
    --argjson until "$((now_epoch + LOCK_TTL_SECONDS))" \
    '{lock_id:$lock_id, purchase_id:$purchase_id, handler:$handler, parent_user_service_id:$parent, until:$until}' \
    >"${payload}"
  dest="$(mktemp)" || { rm -f "${payload}"; stop_temporary "storage_io"; }
  code="$(_storage_write PUT "${LOCK_KEY}" "${payload}" "${dest}")"
  if [[ "${code}" == "200" ]]; then
    LOCK_HELD="1"
    rm -f "${payload}" "${dest}"
    return 0
  fi
  if [[ "${code}" == "transport" || "${code}" =~ ^5 ]]; then
    rm -f "${payload}" "${dest}"
    stop_temporary "lock_unavailable"
  fi
  rm -f "${dest}"
  IFS=$'\t' read -r id until < <(_lock_fields) || { rm -f "${payload}"; stop_temporary "lock_unavailable"; }
  if [[ "${id}" == "${LOCK_ID}" ]]; then
    LOCK_HELD="1"
    rm -f "${payload}"
    return 0
  fi
  if [[ "${until}" =~ ^[0-9]+$ && "${until}" -gt "${now_epoch}" ]]; then
    rm -f "${payload}"
    stop_temporary "lock_held"
  fi
  dest="$(mktemp)" || { rm -f "${payload}"; stop_temporary "storage_io"; }
  code="$(_storage_write POST "${LOCK_KEY}" "${payload}" "${dest}")"
  rm -f "${payload}" "${dest}"
  [[ "${code}" == "200" ]] || stop_temporary "lock_unavailable"
  IFS=$'\t' read -r id until < <(_lock_fields) || stop_temporary "lock_unavailable"
  [[ "${id}" == "${LOCK_ID}" ]] || stop_temporary "lock_held"
  LOCK_HELD="1"
}

_parent_file() {
  local dest code
  dest="$(mktemp)" || stop_temporary "shm_io"
  code="$(_curl_code "${dest}" -H "session-id: ${SESSION_ID}" \
    "${API_URL}/shm/v1/user/service?user_service_id=${PARENT_US_ID}")"
  if [[ "${code}" == "transport" || "${code}" =~ ^5 ]]; then
    rm -f "${dest}"
    stop_temporary "shm_unavailable"
  fi
  if [[ "${code}" != "200" ]]; then
    rm -f "${dest}"
    stop_temporary "shm_parent_${code}"
  fi
  echo "${dest}"
}

_panel_file() {
  local dest code
  dest="$(mktemp)" || stop_temporary "panel_io"
  code="$(_curl_code "${dest}" -H "$(_auth_header)" \
    "${PANEL_URL}/api/users/by-username/us_${PARENT_US_ID}")"
  if [[ "${code}" == "transport" || "${code}" =~ ^5 ]]; then
    rm -f "${dest}"
    stop_temporary "panel_unavailable"
  fi
  if [[ "${code}" == "404" ]]; then
    rm -f "${dest}"
    stop_permanent "panel_user_missing"
  fi
  if [[ "${code}" != "200" ]]; then
    rm -f "${dest}"
    stop_temporary "panel_read_${code}"
  fi
  jq -e -c '.response | type == "object"' "${dest}" >/dev/null || {
    rm -f "${dest}"
    stop_temporary "panel_malformed"
  }
  echo "${dest}"
}

_finish_applied() {
  local record="$1" panel="$2" before="$3"
  local expire strategy username uuid limit
  expire="$(jq -r '.response.expireAt // empty' "${panel}")"
  strategy="$(jq -r '.response.trafficLimitStrategy // empty' "${panel}")"
  username="$(jq -r '.response.username // empty' "${panel}")"
  uuid="$(jq -r '.response.uuid // empty' "${panel}")"
  limit="$(jq -r '.response.trafficLimitBytes | tonumber' "${panel}")"
  local want_expire want_strategy want_uuid want_limit
  want_expire="$(jq -r '.apply_proof.expire_at' "${record}")"
  want_strategy="$(jq -r '.apply_proof.strategy' "${record}")"
  want_uuid="$(jq -r '.apply_proof.remnawave_uuid' "${record}")"
  want_limit="$(jq -r '.applied_limit_bytes' "${record}")"
  [[ "${username}" == "us_${PARENT_US_ID}" ]] || stop_permanent "panel_username_mismatch"
  [[ "${uuid}" == "${want_uuid}" ]] || stop_permanent "panel_uuid_mismatch"
  [[ "${strategy}" == "${want_strategy}" && "${strategy}" == "MONTH" ]] || stop_permanent "strategy_changed"
  [[ "${expire}" == "${want_expire}" ]] || stop_permanent "expire_changed"
  [[ "${limit}" == "${want_limit}" ]] || stop_permanent "limit_not_applied"
  jq --arg before "${before}" \
    '.state = "APPLIED" | .last_error = null | .apply_proof.phase = "confirmed" | .apply_proof.observed_limit_after = ($before | tonumber)' \
    "${record}" >"${record}.next"
  # observed_limit_after must be the applied limit, not the previous one.
  jq --argjson after "${limit}" \
    '.apply_proof.observed_limit_after = $after' "${record}.next" >"${record}.done"
  mv "${record}.done" "${record}"
  rm -f "${record}.next"
  STAGE="applied"
  _save_purchase "${record}"
  log "base_limit_bytes=$(jq -r '.base_limit_bytes' "${record}") applied_limit_bytes=${limit} reason=applied"
  echo "RESULT level=ok reason=applied" >&2
  exit 0
}

_require_tools
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
if [[ -r /proc/sys/kernel/random/uuid ]]; then
  LOCK_ID="$(tr -d '-' < /proc/sys/kernel/random/uuid)"
else
  LOCK_ID="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
fi
[[ "${LOCK_ID}" =~ ^[0-9a-fA-F]{32}$ ]] || stop_permanent "lock_id_unavailable"

STAGE="load_storage"
record="$(_load_purchase)"
jq -e --arg purchase_id "${PURCHASE_ID}" --arg parent "${PARENT_US_ID}" --arg period "${PERIOD}" --arg user "${ADDON_USER_ID}" '
  .purchase_id == $purchase_id
  and (.parent_user_service_id | tostring) == $parent
  and .period == $period
  and (.user_id | tostring) == $user
  and (.base_limit_bytes | type == "number")
  and (.addon_bytes | type == "number")
  and (.applied_limit_bytes | type == "number")
  and .applied_limit_bytes == (.base_limit_bytes + .addon_bytes)
  and .base_limit_bytes > 0
  and .addon_bytes > 0
' "${record}" >/dev/null || { rm -f "${record}"; stop_permanent "storage_contract"; }

state="$(jq -r '.state // empty' "${record}")"
case "${state}" in
  EXPIRED) rm -f "${record}"; stop_permanent "state_expired" ;;
  REFUNDED) rm -f "${record}"; stop_permanent "state_refunded" ;;
  FAILED) rm -f "${record}"; stop_permanent "state_failed" ;;
  RESERVED|ORDER_UNKNOWN|PAID|APPLIED) ;;
  *) rm -f "${record}"; stop_permanent "state_unknown" ;;
esac

STAGE="period_window"
start_at="$(jq -r '.period_starts_at // empty' "${record}")"
end_at="$(jq -r '.period_ends_at // empty' "${record}")"
expect_start="$(date -u -d "${PERIOD}-01 00:20:00 UTC" +'%Y-%m-%dT%H:%M:%SZ')"
expect_end="$(date -u -d "${PERIOD}-01 00:20:00 UTC + 1 month" +'%Y-%m-%dT%H:%M:%SZ')"
[[ "${start_at}" == "${expect_start}" && "${end_at}" == "${expect_end}" ]] || {
  rm -f "${record}"
  stop_permanent "period_bounds"
}
now_epoch="$(_now_epoch)"
start_epoch="$(_epoch "${start_at}")" || stop_permanent "period_bounds"
end_epoch="$(_epoch "${end_at}")" || stop_permanent "period_bounds"
if (( now_epoch >= end_epoch )); then
  rm -f "${record}"
  stop_permanent "period_closed"
fi
if (( now_epoch < start_epoch )); then
  rm -f "${record}"
  stop_temporary "period_not_open"
fi

STAGE="payment"
log "catalog_cost=${CATALOG_COST}"
paid="$(jq -e --arg us "${ADDON_US_ID}" --arg user "${ADDON_USER_ID}" --arg service "${EXPECTED_SERVICE_ID}" '
  (.withdraw_date | type == "string" and length > 0)
  and (.user_service_id | tostring) == $us
  and (.user_id | tostring) == $user
  and (.service_id | tostring) == $service
  and (.total | tonumber) > 0
' >/dev/null <<<"${WITHDRAW_JSON}" && echo yes || echo no)"
if [[ "${paid}" != "yes" ]]; then
  rm -f "${record}"
  stop_temporary "payment_not_confirmed"
fi
amount_ok="$(jq -e -n --argjson wd "${WITHDRAW_JSON}" --slurpfile st "${record}" '
  ($st[0].amount == null or $st[0].amount == "")
  or (($st[0].amount | tonumber) == ($wd.total | tonumber))
' >/dev/null && echo yes || echo no)"
[[ "${amount_ok}" == "yes" ]] || { rm -f "${record}"; stop_permanent "amount_mismatch"; }

jq -n --argjson wd "${WITHDRAW_JSON}" --slurpfile st "${record}" '
  $st[0]
  | .withdraw_id = $wd.withdraw_id
  | .user_service_id = ($wd.user_service_id | tonumber)
  | .amount = (($wd.total | tonumber) * 100 | round | . as $c | (($c / 100 | floor | tostring) + "." + (if ($c % 100) < 10 then "0" else "" end) + ($c % 100 | tostring)))
  | if .state == "RESERVED" or .state == "ORDER_UNKNOWN" then .state = "PAID" else . end
' >"${record}.paid"
mv "${record}.paid" "${record}"

STAGE="parent"
parent="$(_parent_file)"
parent_reason="$(jq -r --arg parent "${PARENT_US_ID}" --arg user "${ADDON_USER_ID}" --slurpfile cat <(printf '%s' "${CATALOG_JSON}") '
  if (.data | type != "array") or (.data | length == 0) then "parent_not_owned"
  elif (.data | length != 1) or ((.data[0].user_service_id | tostring) != $parent) then "parent_not_owned"
  elif (.data[0].user_id | tostring) != $user then "parent_not_owned"
  elif .data[0].status != "ACTIVE" then "parent_not_active"
  elif .data[0].service.category != $cat[0].traffic_addon.parent_category then "parent_category_mismatch"
  elif ((.data[0].service_id | tonumber) as $id | $cat[0].traffic_addon.parent_service_ids | index($id) == null) then "tariff_not_allowed"
  else "ok" end
' "${parent}")"
if [[ "${parent_reason}" != "ok" ]]; then
  rm -f "${record}" "${parent}"
  stop_permanent "${parent_reason}"
fi
rm -f "${parent}"

STAGE="lock"
_acquire_lock
fresh="$(_load_purchase)"
mv "${fresh}" "${record}"
state="$(jq -r '.state // empty' "${record}")"
case "${state}" in
  EXPIRED) rm -f "${record}"; stop_permanent "state_expired" ;;
  REFUNDED) rm -f "${record}"; stop_permanent "state_refunded" ;;
  FAILED) rm -f "${record}"; stop_permanent "state_failed" ;;
  RESERVED|ORDER_UNKNOWN|PAID|APPLIED) ;;
  *) rm -f "${record}"; stop_permanent "state_unknown" ;;
esac
jq -e --arg purchase_id "${PURCHASE_ID}" --arg parent "${PARENT_US_ID}" --arg period "${PERIOD}" '
  .purchase_id == $purchase_id
  and (.parent_user_service_id | tostring) == $parent
  and .period == $period
' "${record}" >/dev/null || { rm -f "${record}"; stop_permanent "storage_contract"; }
amount_ok="$(jq -e -n --argjson wd "${WITHDRAW_JSON}" --slurpfile st "${record}" '
  ($st[0].amount == null or $st[0].amount == "")
  or (($st[0].amount | tonumber) == ($wd.total | tonumber))
' >/dev/null && echo yes || echo no)"
[[ "${amount_ok}" == "yes" ]] || { rm -f "${record}"; stop_permanent "amount_mismatch"; }
jq -n --argjson wd "${WITHDRAW_JSON}" --slurpfile st "${record}" '
  $st[0]
  | .withdraw_id = $wd.withdraw_id
  | .user_service_id = ($wd.user_service_id | tonumber)
  | .amount = (($wd.total | tonumber) * 100 | round | . as $c | (($c / 100 | floor | tostring) + "." + (if ($c % 100) < 10 then "0" else "" end) + ($c % 100 | tostring)))
  | if .state == "RESERVED" or .state == "ORDER_UNKNOWN" then .state = "PAID" else . end
' >"${record}.paid"
mv "${record}.paid" "${record}"

STAGE="panel"
panel="$(_panel_file)"
username="$(jq -r '.response.username // empty' "${panel}")"
status="$(jq -r '.response.status // empty' "${panel}")"
strategy="$(jq -r '.response.trafficLimitStrategy // empty' "${panel}")"
uuid="$(jq -r '.response.uuid // empty' "${panel}")"
expire="$(jq -r '.response.expireAt // empty' "${panel}")"
reset_at="$(jq -r '.response.lastTrafficResetAt // empty' "${panel}")"
created_at="$(jq -r '.response.createdAt // empty' "${panel}")"
limit="$(jq -r '.response.trafficLimitBytes | tonumber? // empty' "${panel}")"
used="$(jq -r '.response.usedTrafficBytes // .response.userTraffic.usedTrafficBytes // empty' "${panel}")"
[[ "${username}" == "us_${PARENT_US_ID}" ]] || { rm -f "${record}" "${panel}"; stop_permanent "panel_username_mismatch"; }
[[ "${strategy}" == "MONTH" ]] || { rm -f "${record}" "${panel}"; stop_permanent "strategy_mismatch"; }
[[ "${uuid}" =~ ^[0-9a-fA-F-]{36}$ ]] || { rm -f "${record}" "${panel}"; stop_temporary "panel_malformed"; }
[[ "${limit}" =~ ^[0-9]+$ ]] || { rm -f "${record}" "${panel}"; stop_temporary "panel_malformed"; }
case "${status}" in
  ACTIVE|LIMITED) ;;
  DISABLED|EXPIRED) rm -f "${record}" "${panel}"; stop_permanent "panel_status" ;;
  *) rm -f "${record}" "${panel}"; stop_permanent "panel_status" ;;
esac
log "used_traffic_bytes=${used:-unknown} panel_status=${status} base_limit_bytes=$(jq -r '.base_limit_bytes' "${record}") applied_limit_bytes=$(jq -r '.applied_limit_bytes' "${record}")"

# Remnawave 3.4.8 leaves lastTrafficResetAt null until a reset runs.
# A user created inside this window still has a zero counter, so createdAt
# opens the period. A reset timestamp from the previous window does not.
created_epoch=""
reset_epoch=""
if [[ -n "${created_at}" ]]; then
  created_epoch="$(_epoch "${created_at}")" || created_epoch=""
fi
if [[ -n "${created_epoch}" && "${created_epoch}" -gt "${now_epoch}" ]]; then
  rm -f "${record}" "${panel}"
  stop_temporary "created_at_in_future"
fi
if [[ -n "${reset_at}" ]]; then
  reset_epoch="$(_epoch "${reset_at}")" || reset_epoch=""
fi
opened="no"
if [[ -n "${reset_epoch}" && "${reset_epoch}" -ge "${start_epoch}" && "${reset_epoch}" -le "${now_epoch}" ]]; then
  opened="yes"
fi
if [[ "${opened}" != "yes" && -z "${reset_at}" && -n "${created_epoch}" \
  && "${created_epoch}" -ge "${start_epoch}" && "${created_epoch}" -le "${now_epoch}" ]]; then
  opened="yes"
fi
if [[ "${opened}" != "yes" ]]; then
  rm -f "${record}" "${panel}"
  stop_temporary "period_not_confirmed"
fi

base="$(jq -r '.base_limit_bytes' "${record}")"
applied="$(jq -r '.applied_limit_bytes' "${record}")"
proof_uuid="$(jq -r '.apply_proof.remnawave_uuid // empty' "${record}")"
proof_phase="$(jq -r '.apply_proof.phase // empty' "${record}")"

if [[ "${limit}" == "${applied}" && "${proof_uuid}" == "${uuid}" && ( "${proof_phase}" == "patching" || "${proof_phase}" == "confirmed" ) ]]; then
  STAGE="confirm"
  _finish_applied "${record}" "${panel}" "${limit}"
fi

if [[ "${limit}" != "${base}" ]]; then
  rm -f "${record}" "${panel}"
  stop_permanent "limit_conflict"
fi

STAGE="mark_patching"
_lock_is_current || { rm -f "${record}" "${panel}"; stop_temporary "lock_lost"; }
jq --arg uuid "${uuid}" --arg expire "${expire}" --arg strategy "${strategy}" --argjson before "${limit}" '
  .apply_proof = {
    remnawave_uuid: $uuid,
    expire_at: $expire,
    strategy: $strategy,
    observed_limit_before: $before,
    phase: "patching"
  }
' "${record}" >"${record}.proof"
mv "${record}.proof" "${record}"
_save_purchase "${record}"

STAGE="patch"
_lock_is_current || { rm -f "${record}" "${panel}"; stop_temporary "lock_lost"; }
patch_body="$(mktemp)" || stop_temporary "panel_io"
jq -n --arg uuid "${uuid}" --argjson trafficLimitBytes "${applied}" \
  '{uuid:$uuid, trafficLimitBytes:$trafficLimitBytes}' >"${patch_body}"
patch_dest="$(mktemp)" || stop_temporary "panel_io"
patch_code="$(_curl_code "${patch_dest}" -X PATCH -H "$(_auth_header)" \
  -H "Content-Type: application/json" --data-binary @"${patch_body}" \
  "${PANEL_URL}/api/users")"
rm -f "${patch_body}" "${patch_dest}"
rm -f "${panel}"
panel="$(_panel_file)"
limit="$(jq -r '.response.trafficLimitBytes | tonumber? // empty' "${panel}")"
if [[ "${patch_code}" == "transport" || "${patch_code}" =~ ^5 || "${patch_code}" == "408" || "${patch_code}" == "429" ]]; then
  if [[ "${limit}" == "${applied}" ]]; then
    _finish_applied "${record}" "${panel}" "${applied}"
  fi
  rm -f "${record}" "${panel}"
  stop_temporary "panel_unavailable"
fi
if [[ "${patch_code}" != "200" ]]; then
  rm -f "${record}" "${panel}"
  stop_permanent "panel_rejected"
fi
STAGE="confirm"
_finish_applied "${record}" "${panel}" "${applied}"
