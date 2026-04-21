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

Для конкретной услуги в SHM можно задать собственный Internal Squad:

```yaml
remnawave:
  internal_squad_name: AntiBlock-Squad
  traffic_limit_bytes: 53687091200
  traffic_limit_strategy: MONTH
  hwid_device_limit: 2  
```

### 🔍 Описание параметров услуги
| Параметр | Обязательный | Описание |
|-----------|--------------|-----------|
| `internal_squad_name`	| ⛔	| Internal Squad для пользователей этой услуги |
| `traffic_limit_bytes` |	⛔ |	Лимит трафика в байтах. Если не задан — используется 0 (без ограничения) |
| `traffic_limit_strategy` |	⛔ |	Стратегия сброса лимита трафика. Допустимые значения: NO_RESET, DAY, WEEK, MONTH |
| `hwid_device_limit` |	⛔ |	Лимит устройств (HWID Device Limit). Если не задан — используется 0 (без ограничения) |

### Приоритет выбора Internal Squad

Шаблон определяет Internal Squad в следующем порядке:

1. `us.service.settings.remnawave.internal_squad_name`
2. `server.settings.remnawave.default_internal_squad_name`

Это позволяет использовать один и тот же шаблон для нескольких тарифов и направлять пользователей в разные Internal Squad Remnawave.

### Поведение лимитов по умолчанию

Если параметры услуги не заданы, используются значения по умолчанию:

traffic_limit_bytes = 0
traffic_limit_strategy = NO_RESET
hwid_device_limit = 0

То есть старые услуги без этих настроек продолжают работать как раньше.

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
