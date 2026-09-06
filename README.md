# 🧩 Remnawave SHM Template

Шаблон для интеграции **Remnawave Panel** с биллингом **SHM (Server Hosting Manager)**.  
Позволяет автоматически создавать, блокировать, продлевать и удалять пользователей Remnawave из интерфейса SHM.  
Полностью совместим с экосистемой **Friends Connect / VPN for Friends**.

---

## 🚀 Возможности

- Автоматическое управление пользователями Remnawave:
  - создание, активация, блокировка, продление, удаление;
- Привязка к **Internal Squad** (через имя, без кэша UUID);
- **🆕 Поддержка выбора Internal Squad на уровне услуги SHM (с fallback на серверный Squad)**;
- **🆕 Поддержка лимита трафика на уровне услуги SHM**;
- **🆕 Поддержка стратегии сброса трафика на уровне услуги SHM**;
- **🆕 Поддержка лимита устройств (HWID Device Limit) на уровне услуги SHM**;
- Корректная обработка срока действия `{{ us.expire }}` с учётом таймзоны SHM и перехода на летнее/зимнее время;
- Загрузка JSON-профиля пользователя в SHM (`storage/manage/vpn_rmw_<id>`);
- Минимальные зависимости (`curl`, `jq`);
- **🆕 Опциональная поддержка sanitize-username (совместимость с Marzban-legacy ссылками)**.

---

## ⚙️ Настройки сервера `remnawave`

В SHM в разделе **Settings → Servers → [ваш сервер] → Settings JSON**  
нужно добавить параметры:

```yaml
remnawave:
  api: https://panel.example.com           # Базовый URL панели Remnawave (https://...)
  token: eyJh...                           # API-токен (Bearer)
  default_internal_squad_name: Default-Squad  # Fallback Internal Squad (если не задан в услуге)

  # Необязательные параметры:
  shm_tz: Europe/Moscow          # Таймзона SHM, если отличается от системной
  expire_safety_minutes: 0       # Дополнительный буфер в минутах (например, 21)
  sanitize_username: false       # 🆕 Включает алгоритм приведения username (Marzban-style)
```

### 🔍 Описание параметров

| Параметр | Обязательный | Описание |
|-----------|--------------|-----------|
| `api` | ✅ | Базовый URL API панели (например https://panel.example.com) |
| `token` | ✅ | Bearer-токен администратора Remnawave |
| `default_internal_squad_name` | ✅ | Internal Squad по умолчанию (если не задан в услуге) |
| `shm_tz` | ⛔ | Таймзона SHM для корректной конвертации времени (пример: Europe/Moscow) |
| `expire_safety_minutes` | ⛔ | Дополнительный сдвиг срока действия в минутах |
| `sanitize_username` | ⛔ | Если true,username приводится алгоритмом из remnawave/subscription-page |

---

## 🧩 Настройки услуги (опционально)

Для конкретной услуги в SHM можно задать собственный Internal Squad и опционально External Squad:

```yaml
remnawave:
  internal_squad_name: AntiBlock-Squad
  external_squad_name: VPN-for-Friends
  traffic_limit_bytes: 53687091200
  traffic_limit_strategy: MONTH
  hwid_device_limit: 2  
```

### 🔍 Описание параметров услуги
| Параметр | Обязательный | Описание |
|-----------|--------------|-----------|
| `internal_squad_name`	| ⛔	| Internal Squad для пользователей этой услуги |
| `external_squad_name` | ⛔ | Точное имя External Squad в Remnawave. UUID в SHM не хранится. Отсутствие параметра сохраняет прежнее поведение |
| `traffic_limit_bytes` |	⛔ |	Лимит трафика в байтах. Если не задан — используется 0 (без ограничения) |
| `traffic_limit_strategy` |	⛔ |	Стратегия сброса лимита трафика. Допустимые значения: NO_RESET, DAY, WEEK, MONTH |
| `hwid_device_limit` |	⛔ |	Лимит устройств (HWID Device Limit). Три состояния: отсутствует/`null` — глобальный HWID limit Remnawave; `0` — лимит отключён индивидуально; `N > 0` — индивидуальный лимит `N` |

`external_squad_name` отвечает только за бренд / Subpage Config.
`internal_squad_name` по-прежнему определяет доступные ноды и inbound.

Примеры настройки только External Squad (остальные параметры услуги без изменений):

**VFF:**
```yaml
remnawave:
  external_squad_name: VPN-for-Friends
```

**Friends Connect:**
```yaml
remnawave:
  external_squad_name: Friends-Connect
```

### Приоритет выбора Internal Squad

Шаблон определяет Internal Squad в следующем порядке:

1. `us.service.settings.remnawave.internal_squad_name`
2. `server.settings.remnawave.default_internal_squad_name`

Это позволяет использовать один и тот же шаблон для нескольких тарифов и направлять пользователей в разные Internal Squad Remnawave.

Для External Squad серверного fallback нет: параметр задаётся только на уровне услуги. Отсутствие или `null` означает, что External Squad не задан.

### Поведение External Squad

- Новые пользователи получают External Squad при событии `CREATE` (имя резолвится в UUID через `GET /api/external-squads`).
- Существующие пользователи требуют отдельной reconciliation-процедуры — шаблон сам их не обновляет.
- `ACTIVATE` и `PROLONGATE` пока не синхронизируют External Squad.

### One-time External Squad reconciliation

Одноразовая утилита `scripts/reconcile_external_squads.py` назначает External Squad
уже существующим пользователям Remnawave по категории пользовательской услуги в SHM:

| SHM category | External Squad |
|--------------|----------------|
| `vpn-mz-test` | `VPN-for-Friends` |
| `vpn-mz-fc` | `Friends-Connect` |

Username в Remnawave: `us_<user_service_id>` (не `user_id`).

По умолчанию выполняется **dry-run** (без изменений в Remnawave).

**1. Dry-run**

```bash
export SHM_PASSWORD='...'
export REMNAWAVE_TOKEN='...'

python3 scripts/reconcile_external_squads.py \
  --shm-base-url https://shm.example.com \
  --shm-login admin \
  --shm-password-env SHM_PASSWORD \
  --remnawave-panel-url https://panel.example.com \
  --remnawave-token-env REMNAWAVE_TOKEN \
  --output ./reconcile-dry-run
```

В `--output` появятся `summary.json`, `plan.json`, `plan.csv`, `conflicts.csv`, `missing.csv`.

**2. Просмотр отчёта**

- `summary.json` — `plan_counts` (`already_correct` / `needs_assignment` / `conflict` / `missing_in_remnawave` / `error`)
- `conflicts.csv` — пользователи с другим ненулевым External Squad (по умолчанию **не** изменяются)
- `missing.csv` — `us_<user_service_id>` не найден в Remnawave

**3. Apply** (только `needs_assignment`)

Apply выполняется **последовательными синхронными** `PATCH /api/users`
(по одному пользователю; между запросами — `--request-delay-ms`).
Bulk-update не используется.

```bash
python3 scripts/reconcile_external_squads.py \
  --shm-base-url https://shm.example.com \
  --shm-login admin \
  --shm-password-env SHM_PASSWORD \
  --remnawave-panel-url https://panel.example.com \
  --remnawave-token-env REMNAWAVE_TOKEN \
  --output ./reconcile-apply \
  --apply \
  --confirm ASSIGN_EXTERNAL_SQUADS
```

Без пары `--apply` + `--confirm ASSIGN_EXTERNAL_SQUADS` изменения запрещены.

Повторный запуск **идемпотентен**: уже назначенные пользователи попадают в
`already_correct` и не изменяются; остаются только новые `needs_assignment`.

После apply в `summary.json` добавляется блок `apply`
(`requested` / `applied` / `failed` / `complete`).

**4. Повторный dry-run после apply**

Запустите dry-run ещё раз в **новый** пустой `--output`: записи из `needs_assignment`
должны перейти в `already_correct`.

**Важно:** не переключайте `SUBPAGE_CONFIG_UUID` на `00000000-0000-0000-0000-000000000000`
до завершения reconciliation — иначе брендинг/Subpage Config может «отвалиться»
у пользователей, которым External Squad ещё не назначен.

### Поведение лимитов по умолчанию

Если параметры услуги не заданы:

- `traffic_limit_bytes` = `0` (без ограничения трафика)
- `traffic_limit_strategy` = `NO_RESET`

`hwid_device_limit` — отдельная семантика, `null` и `0` здесь не одно и то же:

| SHM `hwid_device_limit` | Remnawave `hwidDeviceLimit` | Смысл |
|-------------------------|-----------------------------|--------|
| отсутствует или `null` | поле не отправляется (CREATE и UPDATE) | используется **глобальный** HWID device limit панели Remnawave |
| `0` | `0` | HWID-лимит **отключён индивидуально** для этого пользователя |
| `N > 0` | `N` | индивидуальный лимит `N` устройств |

Значение глобального fallback задаётся в панели Remnawave и **не** захардкожено в шаблоне.

На CREATE Remnawave не принимает `hwidDeviceLimit: null`, поэтому при отсутствии/`null` настройки ключ в payload отсутствует. На `ACTIVATE` / `PROLONGATE` (PATCH) шаблон тоже **не** отправляет `hwidDeviceLimit`: явный `0` или `N > 0` пишутся, а absent/`null` ключ опускает. Lifecycle-событие не сбрасывает уже записанный в панели explicit value (`0` или `N`) обратно к panel default.

Переход существующего explicit `hwidDeviceLimit` к наследованию глобального fallback выполняется только **reconciliation** (`scripts/reconcile_hwid_limits.py`), не шаблоном.

То есть старые услуги без `hwid_device_limit` больше не получают индивидуальный `0` на CREATE; уже существующий `0` в панели сам по себе не мигрируется через `ACTIVATE`/`PROLONGATE`.

### One-time HWID device limit reconciliation

Одноразовая утилита `scripts/reconcile_hwid_limits.py` сверяет
`hwidDeviceLimit` существующих пользователей Remnawave с настройкой
конкретной услуги SHM (`us.service.settings.remnawave.hwid_device_limit`).

Username в Remnawave: `us_<user_service_id>` (не `user_id`).

Целевое значение считается **по услуге**, а не «все нули в панели → null»:
явное `hwid_device_limit: 0` в SHM сохраняется как `0`.

Пользователи, для которых нельзя уверенно сопоставить user-service SHM
и каталог услуги, не изменяются.

По умолчанию выполняется **dry-run** (без изменений в Remnawave).

**1. Dry-run**

```bash
export SHM_PASSWORD='...'
export REMNAWAVE_TOKEN='...'

python3 scripts/reconcile_hwid_limits.py \
  --shm-base-url https://shm.example.com \
  --shm-login admin \
  --shm-password-env SHM_PASSWORD \
  --remnawave-panel-url https://panel.example.com \
  --remnawave-token-env REMNAWAVE_TOKEN \
  --output ./reconcile-hwid-dry-run
```

Опционально ограничить категории: `--category vpn-mz-test --category vpn-mz-fc`.

В `--output` появятся `summary.json`, `plan.json`, `plan.csv`, `errors.csv`, `missing.csv`.

**2. Просмотр отчёта**

- `summary.json` — `plan_counts` и `hwid_snapshot`:
  сколько сейчас имеют `0`, сколько из них должны стать `null`,
  сколько должны сохранить `0`, сколько имеют явный индивидуальный limit,
  сколько уже соответствуют SHM, сколько нельзя безопасно классифицировать
- классификации: `already_correct` / `needs_reset_to_panel_default` /
  `needs_set_explicit_limit` / `needs_disable_limit` /
  `missing_in_remnawave` / `invalid_shm_setting` / `error`
- `target_resolved=true` + `target_hwid_device_limit: null` — валидная цель
  (panel default); у `invalid_shm_setting` ключ цели отсутствует и
  `target_resolved=false` (это не reset)
- `errors.csv` — невалидная настройка SHM или ошибка резолва
- `missing.csv` — `us_<user_service_id>` не найден в Remnawave

**3. Apply** (только allow-list ∩ mutation-class, без drift)

Apply выполняется **последовательными синхронными** `PATCH /api/users`
(по одному пользователю; между запросами — `--request-delay-ms`).
Bulk-update не используется. После каждого PATCH пользователь заново
читается и сверяется `hwidDeviceLimit` (включая JSON `null`).

`--apply` требует явный allow-list `--apply-username` (можно повторять).
Allow-list — **дополнительное** ограничение, не обход плана:

- пользователь должен быть в mutation-class
  (`needs_reset_to_panel_default` / `needs_set_explicit_limit` /
  `needs_disable_limit`);
- `already_correct`, `missing_in_remnawave`, `invalid_shm_setting`, `error`
  не PATCHатся даже если username в списке;
- перед PATCH повторно читается Remnawave: `id`, current и classification
  должны совпасть с планом; drift блокирует PATCH и останавливает apply.

```bash
python3 scripts/reconcile_hwid_limits.py \
  --shm-base-url https://shm.example.com \
  --shm-login admin \
  --shm-password-env SHM_PASSWORD \
  --remnawave-panel-url https://panel.example.com \
  --remnawave-token-env REMNAWAVE_TOKEN \
  --output ./reconcile-hwid-apply \
  --apply \
  --confirm RECONCILE_HWID_LIMITS \
  --apply-username us_123 \
  --apply-username us_456
```

Без `--apply` + `--confirm RECONCILE_HWID_LIMITS` + хотя бы одного
`--apply-username` изменения запрещены.

Повторный запуск **идемпотентен**: уже приведённые пользователи попадают в
`already_correct` и не изменяются.

**4. Повторный dry-run после apply**

Запустите dry-run ещё раз в **новый** пустой `--output`: изменённые записи
должны перейти в `already_correct`.

### One-time Traffic Limit reconciliation

Одноразовая утилита `scripts/reconcile_traffic_limits.py` сверяет
`trafficLimitBytes` / `trafficLimitStrategy` существующих пользователей
Remnawave с настройками конкретной услуги SHM:

- `us.service.settings.remnawave.traffic_limit_bytes`
- `us.service.settings.remnawave.traffic_limit_strategy`

Source of truth — **настройки услуги SHM**, не Internal Squad и не имя
тарифа. Standard / другие планы определяются только тем, что явно записано
в `service.settings.remnawave`.

В отличие от lifecycle-шаблона и от HWID reconciliation:

- отсутствие блока `remnawave` или ключа `traffic_limit_bytes` —
  `unmanaged_service`, **не** `target=0`;
- массовый reconciliation не должен случайно сбросить уже существующий
  лимит на услуге, которая ещё не переведена на новую policy;
- если `traffic_limit_bytes` задан, `traffic_limit_strategy` тоже должна
  быть явной и валидной (`NO_RESET` / `DAY` / `WEEK` / `MONTH`).
  Fallback не угадывается. `MONTH_ROLLING` backend 3.2.3 принимает,
  текущий SHM template — нет; такое значение в SHM = `invalid_shm_setting`.

Username в Remnawave: `us_<user_service_id>` (не `user_id`).

По умолчанию выполняется **dry-run** (без изменений в Remnawave).
Трафик **не** сбрасывается: нет вызова `/actions/reset-traffic` и нет
записи `usedTrafficBytes` / `lastTrafficResetAt` / `status`.
`NO_RESET` для Standard корректен: календарный reset в Remnawave не нужен,
сброс трафика выполняет SHM `PROLONGATE`.

**1. Dry-run**

```bash
export SHM_PASSWORD='...'
export REMNAWAVE_TOKEN='...'

python3 scripts/reconcile_traffic_limits.py \
  --shm-base-url https://shm.example.com \
  --shm-login admin \
  --shm-password-env SHM_PASSWORD \
  --remnawave-panel-url https://panel.example.com \
  --remnawave-token-env REMNAWAVE_TOKEN \
  --output ./reconcile-traffic-dry-run
```

Опционально ограничить выборку: `--category vpn-mz-test` (можно повторять)
и/или `--service-id 6` (можно повторять).

В `--output` появятся `summary.json`, `plan.json`, `plan.csv`,
`errors.csv`, `missing.csv`, `over_limit.csv`.

**2. Просмотр отчёта**

- `summary.json` — `total_user_services_inspected`, `managed_users`,
  `plan_counts`, `risk.would_be_over_limit_now_count`,
  `target_combinations` (bytes + strategy + users_count)
- классификации: `already_correct` / `needs_set_limit` /
  `needs_set_strategy` / `needs_set_limit_and_strategy` /
  `unmanaged_service` / `missing_in_remnawave` /
  `invalid_shm_setting` / `error`
- `would_be_over_limit_now` — risk-поле, не отдельная mutation-class:
  `target_limit_bytes > 0` и `usedTrafficBytes >= target`
- `over_limit.csv` — только такие пользователи
- `errors.csv` — невалидная настройка SHM или ошибка резолва
- `missing.csv` — `us_<user_service_id>` не найден в Remnawave

**3. Apply** (только scoped mutation-class, без unscoped прогона)

Apply выполняется **последовательными синхронными** `PATCH /api/users`
с телом только:

```json
{"id": <numeric user id>, "trafficLimitBytes": <target>, "trafficLimitStrategy": "<target>"}
```

Bulk-update и reset-traffic не используются. Перед каждым PATCH live-user
и target перепроверяются. `usedTrafficBytes` — живой счётчик: рост
между plan и PATCH допустим, уменьшение (вероятный reset) останавливает
apply. После PATCH сверяются `trafficLimitBytes`, `trafficLimitStrategy`,
`status`, `expireAt`, HWID и squads; `usedTrafficBytes` должен быть
`>= pre-PATCH`. Unexpected decrease/status drift останавливает apply.

`--apply` требует одновременно:

- `--confirm RECONCILE_TRAFFIC_LIMITS`;
- хотя бы один `--category` или `--service-id` (unscoped apply запрещён).

Опциональный `--apply-username` (можно повторять) — **дополнительная**
allow-list поверх scope. Она сужает mutation set, но **не** заменяет
обязательный `--category` / `--service-id` и **не** обходит
`would_be_over_limit_now` / non-mutation classes.

Итоговый mutation set:

`category/service-id scope` ∩ `needs_set_*` ∩ `--apply-username`
(если задан) ∩ over-limit safety.

`--apply --apply-username us_1001` без `--category`/`--service-id`
запрещён.

Пользователи с `would_be_over_limit_now=true` **не** PATCH-аются, пока
не передан `--include-over-limit` — даже если username в allow-list.
После установки лимита Remnawave может почти сразу перевести такого
пользователя в `LIMITED`.

**Canary**

```bash
python3 scripts/reconcile_traffic_limits.py \
  --shm-base-url https://shm.example.com \
  --shm-login admin \
  --shm-password-env SHM_PASSWORD \
  --remnawave-panel-url https://panel.example.com \
  --remnawave-token-env REMNAWAVE_TOKEN \
  --output ./reconcile-traffic-canary \
  --service-id 3 \
  --apply-username us_1001 \
  --apply-username us_1002 \
  --apply \
  --confirm RECONCILE_TRAFFIC_LIMITS
```

**Scoped apply** (все mutation-class в выбранных услугах)

```bash
python3 scripts/reconcile_traffic_limits.py \
  --shm-base-url https://shm.example.com \
  --shm-login admin \
  --shm-password-env SHM_PASSWORD \
  --remnawave-panel-url https://panel.example.com \
  --remnawave-token-env REMNAWAVE_TOKEN \
  --output ./reconcile-traffic-apply \
  --service-id 3 \
  --service-id 4 \
  --apply \
  --confirm RECONCILE_TRAFFIC_LIMITS
```

Без `--apply` + `--confirm RECONCILE_TRAFFIC_LIMITS` + scoped
`--category`/`--service-id` изменения запрещены.

Повторный запуск **идемпотентен**: уже приведённые пользователи попадают в
`already_correct`, `usedTrafficBytes` остаётся прежним.

**4. Повторный dry-run после apply**

Запустите dry-run ещё раз в **новый** пустой `--output`: записи
`needs_*` должны перейти в `already_correct`.

---

## 🧹 Опция sanitize_username

Когда параметр:

```yaml
sanitize_username: true
```

включён — все вызовы API Remnawave (кроме CREATE) используют **санитизированное имя пользователя**:

- допускаются только `[A-Za-z0-9_-]`;
- остальные символы заменяются на `_`;
- минимальная длина username — **6 символов** (недостающие заменяются `_`);
- алгоритм полностью совпадает с реализацией в:  
  https://github.com/remnawave/subscription-page/blob/main/backend/src/common/utils/sanitize-username.ts

### 👉 Важно:
- CREATE всегда использует оригинальный username (us_<id>);
- Все остальные события используют санитизированную версию.

---

## 🕒 Обработка времени

Функция `_expire_iso()` автоматически:
- читает `{{ us.expire }}` из SHM;
- учитывает переходы DST;
- переводит в ISO-8601 UTC;
- применяет `expire_safety_minutes`.

---

## 📜 Установка

1. В панели SHM откройте **Templates → Add new**.
2. Назовите шаблон, например:
   ```
   vpn_rmw
   ```
3. Скопируйте содержимое файла `shm-remnawave.template.sh`
4. Сохраните.

---

## 🔧 Требования

- `curl`
- `jq`
- GNU coreutils (`date` с поддержкой TZ)

---

## 🧰 События

| Событие | Действие в Remnawave |
|----------|----------------------|
| `CREATE` | Создание пользователя и загрузка JSON-конфига |
| `ACTIVATE` | Активация пользователя |
| `BLOCK` | Блокировка пользователя |
| `PROLONGATE` | Сброс трафика + продление срока |
| `REMOVE` | Удаление пользователя |
| `UPDATE` | Обновление JSON-конфига в SHM |

---

## 🔗 Связанные проекты

| Проект | Описание |
|--------|-----------|
| [Remnawave Panel](https://github.com/remnawave) | Панель управления VLESS/Xray |
| [SHM (Server Hosting Manager)](https://github.com/danuk/shm) | Биллинг VPN/хостинга |
| [Friends Connect](https://t.me/vpn_for_myfriends_bot) | Экосистема VPN for Friends |

---

## 📄 Лицензия

[MIT License](LICENSE)

---

## 🤝 Автор

**Sergey Ryabkov**  
GitHub: [@ryabkov82](https://github.com/ryabkov82)
Проект: [VPN for Friends](https://t.me/vpn_for_myfriends_bot)
