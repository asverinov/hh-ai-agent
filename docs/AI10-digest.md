# ДАЙДЖЕСТ СЕССИИ AI10 (05.11.2026)

**Тема:** reconcile inline, единый browser_worker (apply+read), salary thresholds (RUR 200k / USD 3000), questionnaire → skipped, VPN-диагноз (MTU/Cloudflare), AI10-коммиты.

**Предыдущий:** `docs/AI9-digest.md`
**Техдолг:** `docs/TECH_DEBT.md`
**Правила:** `docs/INTERACTION.md`

---

## 1. ЧТО СДЕЛАНО В AI10 (в git, 2 коммита)

### 1.1. Reconcile inline в `search_worker` (главное)

- **`reconcile.py`** (новый): `apply_reconcile(db, seen, allowed_exp, dry_run)` — вся логика в одном месте.
- **`search_worker.py`**: после обхода 4 резюме вызывает `apply_reconcile`. `seen_acc` собирается из API (все ID, не только новые).
- **`database.py`**: `mark_irrelevant` / `reject_by_filter` — принимают `discovered`/`pending_approval`/`analysis_failed`/`expired`/`irrelevant`. `revive_vacancy` — для возврата по полям.
- **Логика reconcile** (NOT_TOUCH: `applied`, `applying`, `apply_failed`, `skipped`, `approved`, `rejected_by_llm`, `rejected_by_filter`):
  - нет в API → `irrelevant` (для discovered/pending/analysis_failed)
  - есть в API, exp not allowed → `rejected_by_filter`
  - есть в API, exp OK → backfill (`experience_id`)
  - expired/irrelevant + есть в API → revive по полям (нет desc → discovered, нет eval → discovered, нет letter → discovered, есть всё → pending_approval)

### 1.2. Единый `browser_worker.py` (новый)

- **Apply (приоритет)** + **read description** в одном цикле.
- `APPLY_COOLDOWN_SEC = 120` (2 мин, было 300).
- **Во время cooldown — читает description** (`_read_during_cooldown`, max 3).
- `reset_all_applying()` на старте — сброс зависших `applying` (single instance).
- Капча при apply → **STOP** (VNC). При read → `defer 60 мин` + пауза 5 мин; 2 подряд → STOP.

### 1.3. Salary thresholds: RUR 200k / USD 3000

- `prefilter_rules.yaml`: `salary_hard_min: 200000`, `salary_hard_min_usd: 3000`.
- `config.py`: `PrefilterRules.salary_hard_min_usd` (через `_int_val`).
- **Логика (везде: `search_worker`, `prefilter_audit_pending`, `enqueue_apply`)**:
  - есть `salary_to` («до Y» / «от X до Y») → режем, если `Y <= threshold`
  - только `salary_from` («от X») → режем, если `X < threshold`
  - прочие валюты (KZT, BYR) — не режем
- **Это исправляет баг:** «от 150 до 250» раньше резалось (по from), теперь не режется (по to=250 > 200).

### 1.4. Apply-очередь

- **`tg_bot.py`**: кнопка `apply:` → `enqueue_apply` (не синхронный `approve_and_apply`). Ответ «В очереди: N-я». TG → `browser_worker` забирает.
- **`_main_menu_keyboard`** — ReplyKeyboard (постоянное меню): «📋 Выдать 20 карточек», «📊 Статус», «🔍 Показать меню».
- **`_send_pending_cards(offset, limit=20)`** — 20 карточек + inline «📋 Следующие 20». `pending_page:N` callback.
- **`run_tg_bot.py`** (новый) — отдельный entrypoint `TelegramService` (без `main.py`). `_NoopSender` для `approval_service` (apply делает `browser_worker`).
- **`send_pending_cards.py`** — fallback (ручной запуск карточек).

### 1.5. questionnaire_required → skipped

- **`database.py`**: `mark_skipped_by_id` (для `pending`/`applying`/`approved` → `skipped`).
- **`hh_client.py`**: `except QuestionnaireRequiredError` → `mark_skipped_by_id(..., 'questionnaire_required')`.
- **`browser_worker._apply_one`**: `pre_response_recheck` → `skip` (не `error`, не увеличивает `apply_streak_errors`).

### 1.6. Prefilter audit (разово)

- **`prefilter_audit_pending.py`** — находит `pending_approval` с `salary < threshold` / плохим `experience_id` / junior в title → `irrelevant`.
- Первый прогон: 43 → irrelevant. Потом 11 USD/RUR (8+3), из них 5 восстановлены (ошибка старой логики).
- **Исправлено:** `reason_for` смотрит `salary_to` (а не только `salary_from`).

### 1.7. Письма для pending

- **`generate_letters_for_pending.py`** (новый): `SELECT pending WHERE cover_letter=''` → `generate_cover_letter` → `UPDATE cover_letter`. **Не меняет статус.** `--limit`, `--dry-run`. `try/except SystemExit` (не падает на `_safe_letter`).

### 1.8. hh_client: стабильность apply

- **`CaptchaDetectedError` / `VacancyExpiredError`** — re-raise перед `except Exception` (иначе `application_failed` съедает).
- **Escape перед `letter_toggle`** — закрыть `drop-base` модалку.
- **`success_marker` timeout 5000 → 15000** (страница медленно грузится).
- **URL с `?from=similar_vacancies`** в `read_vacancy`.

### 1.9. `refresh_hh_token.py` (новый)

Обновляет `.env` (`HH_ACCESS_TOKEN`, `HH_REFRESH_TOKEN`, `HH_TOKEN_EXPIRES_AT`) через `hh.ru/oauth/token`. `fetch_similar_page` теперь вызывает `_refresh_token_if_needed` перед запросом.

### 1.10. Контент из git

- `git rm --cached` для `assess*.md`, `ds_*.md`, `prompts.json`, `calibration_set.json`, `cover_letter_models_test_*.md`, `stage1_hints.yaml`.
- `.gitignore` — полный блок.
- Задача в `TECH_DEBT.md`: шаблоны (`*.example.md`/`*.example.yaml`) после финализации промптов.

### 1.11. VPN-диагноз

- **MTU с AmneziaVPN = 1228** (норма 1500). Большие HTTPS-пакеты к AITUNNEL/HH таймаутят.
- **Split tunneling по IP для Cloudflare не работает** (aitunnel за Cloudflare, IP меняются).
- **Решение:** режим «Только Telegram через VPN» (IP-whitelist: `91.108.4.0/22`, `149.154.160.0/20` и т.д.). Тогда AITUNNEL/hh.ru — напрямую.
- **Без VPN:** AITUNNEL 12KB system → 200 (4 сек), TG → 000 (заблокирован).
- **С VPN:** AITUNNEL 12KB → 000 (90s timeout), TG → 200.

---

## 2. ЧТО В РАБОТЕ НА МОМЕНТ ДАЙДЖЕСТА

- **`migrate_assess --statuses discovered`** — идёт, ~35/280 (Stage 1/2 без `transient_server`).
- **`browser_worker`** — перезапущен (read ~150 discovered без desc).
- **`run_tg_bot`** — не запущен (VPN не работает для больших; TG малые — если VPN включён, но split tunneling не решён).

**Статусы на 05.11 ~13:30:**

    rejected_by_llm     2019
    expired             1415
    irrelevant           452
    discovered           376 (migrate идёт)
    pending_approval     115 (с письмом)
    skipped               45
    applied               44
    rejected_by_filter    35
    apply_failed          17

---

## 3. СЛЕДУЮЩИЕ ШАГИ

1. **Дождаться `migrate_assess`** (280 → 0). Затем `generate_letters_for_pending` для новых pending без письма (~100).
2. **VPN: настроить «Только Telegram через VPN»** (Windows Amnezia, IP-whitelist). Проверить `curl getMe` из VM.
3. **`run_tg_bot`** — после VPN. Карточки в TG (115 pending + новые).
4. **`browser_worker`** — read до 0 discovered без desc, потом apply по кнопкам.
5. **Миграция `rejected_by_llm`** (1926) — не делали. Сначала сэмпл 100 на v1.7, если change rate >20% — все.

---

## 4. КОМАНДЫ ЗАПУСКА

    # TG-бот (нужен VPN)
    PYTHONPATH=. python3 -u scripts/run_tg_bot.py

    # Единый браузерный воркер (apply + read)
    DISPLAY=:99 PYTHONPATH=. python3 -u scripts/browser_worker.py

    # Оценка discovered (v1.7)
    PYTHONPATH=. python3 -u scripts/migrate_assess.py --statuses discovered

    # Письма для pending_approval без письма
    PYTHONPATH=. python3 -u scripts/generate_letters_for_pending.py --limit 5 --dry-run  # тест
    PYTHONPATH=. python3 -u scripts/generate_letters_for_pending.py                    # боевой

    # Reconcile inline (search_worker) — API, 8 мин с SLEEP (5,10)
    PYTHONPATH=. python3 -u scripts/search_worker.py --once

    # Prefilter audit (разово, если нужно)
    PYTHONPATH=. python3 -u scripts/prefilter_audit_pending.py --dry-run

    # Refresh HH-токена
    PYTHONPATH=. python3 scripts/refresh_hh_token.py

---

## 5. NON-OBVIOUS (AI10)

- **`LLM_PROVIDER=anthropic_compatible` в `.env`, но `migrate_assess` создаёт `OpenAICompatibleProvider`** напрямую (для AITUNNEL). Это **правильно**, работает. `create_llm_provider` не используется в `migrate_assess`.
- **`salary_hard_min: 200000`, `salary_hard_min_usd: 3000`** — RUR/USD, прочие валюты не режем. Логика: `to <= / from <`.
- **`mark_expired`** теперь принимает `discovered` (раньше — только `pending_approval`/`applying`). Без этого `browser_worker` залипал на `read_expired` в бесконечном цикле.
- **`dequeue_apply`** — lease 10 мин (`queued_at = now + 10 min`). `reset_all_applying()` на старте `browser_worker` — сброс зависших.
- **`pre_response_recheck`** — `skip`, не `error`. Не увеличивает `apply_streak_errors`.
- **`questionnaire_required`** — `mark_skipped_by_id`, `error_text='questionnaire_required'`. В `browser_worker._apply_one` проверка `_v.status == skipped` → `skip`.
- **`_safe_letter` → `SystemExit(1)`** — `generate_letters_for_pending` ловит `SystemExit` и пропускает (не падает).
- **`Escape` перед `letter_toggle`** — `drop-base` модалка hh.ru перекрывает кнопку.
- **`success_marker` timeout 5→15 сек** — hh.ru медленно грузит успех.
- **`search_worker` SLEEP (5,10)** — 68 страниц × 7.5 сек ≈ 8 мин (было 28 мин с (20,30)).
- **`migrate_assess` с `transient_server`** — AITUNNEL 5xx, `max_retries=2` (2 попытки). Если AITUNNEL лежит — падает, ждать.
- **VPN MTU 1228** — большие запросы (>~1KB system) к AITUNNEL таймаутят. Малые проходят.
- **IP-whitelist Telegram** (не blacklist): только IP Telegram через VPN, всё остальное напрямую.
- **`config.py` `_int_val`** — для `salary_hard_min_usd` (не `int()` — валидация как у `salary`).

---

## 6. ТЕХДОЛГ (AI10, в `docs/TECH_DEBT.md`)

1. **`apply_worker`** — сделан как `browser_worker`. `main.py` — DEPRECATED, не запускать.
2. **`letter_attach_failed`** (138914331) — пометили `applied` (отклик ушёл, письмо нет). Задача: разобрать `_attach_cover_letter_after_response`.
3. **`138847467`** — `pending_approval`, `queue_priority=0`, `error_text='timeout_success_check_manually'`. Проверить вручную в браузере (отклик ушёл?).
4. **Шаблоны контентных файлов** (`*.example.md`/`*.example.yaml`) — после финализации промптов.
5. **`main.py`** — удалить после стабилизации.
6. **`profile.yaml`** — выпилить (в LLM не идёт).
7. **`excluded_keywords`** — не проверяется в `search_worker` (нужен description, есть в `assess_worker`).
8. **`Unclosed session`** — aiohttp в `ManagedLLMProvider`.
9. **`migrate_assess` без `--limit`** — добавить для тестов.
10. **VPN: Linux-разделение по приложениям не работает** — только по IP/хосту (Windows), для VM — «только Telegram через VPN».

---

## 7. ТЕСТЫ

- `tests/test_ai9_fixes.py` (20), `test_llm_database` (5) — зелёные.
- **Не чинили** старые (`test_main.py`, `test_hh_client.py` и т.д.) — отдельная итерация.

---

## 8. GIT

- Коммит `bc29d38` — browser_worker + apply-очередь + refresh + контент из git.
- Коммит `b0e028e` — reconcile inline + browser_worker (cooldown/reads) + salary thresholds + VPN-aware apply.
- Не в git: `assess*.md`, `ds_*.md`, `prompts.json`, `stage1_hints.yaml` (`.gitignore`).
- В git: `reconcile.py`, `browser_worker.py`, `run_tg_bot.py`, `generate_letters_for_pending.py`, `prefilter_audit_pending.py`, `refresh_hh_token.py`.

---

## 9. AI11: Web UI вместо TG (план)

**Проблема:** Telegram требует VPN, но VPN (Amnezia, MTU 1228) ломает большие HTTPS-запросы к AITUNNEL/hh.ru. **Взаимоисключающе:** либо TG (через VPN), либо LLM/hh.ru (без VPN). Workaround — 2 режима, но это неудобно.

**Решение:** Web UI на виртуалке. TG — fallback (запускается только в VPN-режиме, когда нужен).

### 9.1. Стек

- **FastAPI** (async, лёгкий) + Jinja2 для шаблонов + htmx или ванильный JS.
- **Auth:** один пароль в `.env`, cookie (SameSite=Lax, HttpOnly, 30 дней).
- **Порт:** 8080 (или 80 с reverse-proxy). Доступ из локальной сети VM.
- **Service manager:** subprocess + PID-файлы (start/stop воркеров). systemd — опционально позже.

### 9.2. Страницы

| URL | Что | Источник |
|---|---|---|
| `/` | Список вакансий. Табы: «Выбранные» / «Отвергнутые». Слева — список, справа — карточка выбранной. Сортировка `confidence` DESC, фильтр `is_required=true match<0.7`. | `pending_approval`, `rejected_by_llm` |
| `/vacancy/{id}` | Карточка: заголовок + ссылка на hh.ru (новая вкладка) + оценка + письмо + кнопки. | `vacancies` |
| `/monitoring` | Статусы воркеров (start/stop/restart) + последние строки логов + счётчики по статусам. | PID-файлы + `/tmp/*.log` |
| `/login` | Пароль. | `.env` |

**iframe с hh.ru — НЕВОЗМОЖЕН.** `X-Frame-Options: DENY` + `Content-Security-Policy: frame-ancestors` на уровне документа. Обход (вырезать заголовки, прокси, Chrome-extension) — не работает для SPA. **Только ссылка** «открыть в новой вкладке».

### 9.3. Endpoints (все — те же вызовы, что в `tg_bot`)

- `POST /api/apply/{id}` → `db.enqueue_apply(vid)` (уже есть). `browser_worker` подхватит.
- `POST /api/skip/{id}` → `db.skip(vid, ...)` (уже есть).
- `POST /api/edit/{id}` (body: текст) → `db.update_cover_letter(vid, text)` (уже есть).
- `POST /api/unreject/{id}` → **НОВЫЙ** `unreject_and_request_letter(vid)`: `UPDATE status='pending_approval', llm_decision=1` + `generate_cover_letter` + `UPDATE cover_letter`. ~15 строк.
- `GET /api/status` → `db.stats()` + `db.count_pending()` + PID-статусы воркеров.
- `POST /api/worker/{name}/{action}` → start/stop/restart.

### 9.4. Расширить `discover()` — 10 полей из API (2–3 часа)

API `similar_vacancies` уже отдаёт, но `search_worker` их **теряет**. Добавить в `database.discover()` и миграцию БД (`ALTER TABLE`):

| Поле API | Колонка | Зачем |
|---|---|---|
| `published_at` | `published_at` | **Сортировка «кто раньше»** — важно для apply (быстрее отклик = выше шанс). |
| `created_at` | `created_at` | — |
| `initial_created_at` | `initial_created_at` | — |
| `employment.id` | `employment` | фильтр (full/part/project) |
| `schedule.id` | `schedule` | фильтр (remote/fullDay/flexible) |
| `work_format` | `work_format` | фильтр (ON_SITE/REMOTE/HYBRID) |
| `area.name` | `area` | город |
| `address.metro` | `address_metro` | метро |
| `employer.id` | `employer_id` | дедуп по компании |
| `key_skills` | `key_skills_json` | фильтр по критичным навыкам |

`read_vacancy` (браузер) — **только для `description`**, как сейчас. 10 полей из шапки hh.ru — опционально (rating, responses_count), не в MVP.

### 9.5. Оценка AI11

| Этап | Время |
|---|---|
| FastAPI скелет + `/` + `/login` + `apply/skip/edit` | 1–2 дня |
| `/vacancy/{id}` + `unreject_and_request_letter` | 1 день |
| Расширить `discover()` (10 полей) + сортировка | 0.5 дня |
| `/monitoring` (start/stop воркеров) | 0.5 дня |
| Полировка, тесты на 182 pending | 0.5 дня |
| **MVP** | **3–4 дня** |

### 9.6. Что остаётся

- TG (`tg_bot.py`, `run_tg_bot.py`) — не удалять, fallback. Запускать только когда нужен TG (VPN-режим, push в телефон).
- `browser_worker`, `assess_worker`, `migrate_assess`, `search_worker` — **без изменений**. Веб — только UI + `enqueue_apply` + start/stop.
- VPN-проблема **исчезает** для основного сценария (LLM/hh.ru без VPN, веб в локальной сети).

### 9.7. Открытые вопросы (на AI11)

1. **Auth:** один пароль — достаточно? Или нужен 2FA / несколько пользователей?
2. **Доступ:** только локальная сеть VM или reverse-proxy с ноута (HTTPS)?
3. **Service manager:** subprocess/PID или systemd?
4. **Push-уведомления** (было в TG): web-push / email / ничего?
5. **«Сопровод без оценки»** для `rejected_by_llm` — подтверждено, нужно. Точный UX (кнопка на карточке отвергнутой).

---

*Создан 05.11.2026. При обновлении — старую версию в `docs/bak/AI10-digest.md.bak-<timestamp>`.*