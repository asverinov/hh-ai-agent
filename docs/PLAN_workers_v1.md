# ПЛАН: 5 модулей обработки вакансий (v1.1 от 29.10.2026)

**Статус на 29.10.2026: 3 из 5 модулей сделаны** (read_description, assess_worker, cover_letter_worker). Остались apply_worker и tg_bot (кнопка enqueue_apply).

**Состояние на 27.10.2026, 21:35.** Сессия AI6 подходит к концу, план — для следующей сессии.

## 1. КОНТЕКСТ

`search_worker` уже работает (PID 15169 на 21:35, крутится в фоне, `discovered=1708`). Это API-модуль, наполняет `discovered`. Остальные 4 модуля — в плане.

**Проблема с CloakBrowser:** 1 concurrent session. `read_description` и `apply` не могут работать параллельно с одним `.browser-profile`. Решение — **file lock + флаг приоритета** (один профиль, как требует заказчик: два профиля = риск бана аккаунта).

## 2. АРХИТЕКТУРА (5 модулей, 1 CloakBrowser)
search_worker (API) → vacancies (discovered, description='')
↓
read_description (CloakBrowser, flock) → vacancies (description != '')
↓
assess_worker (AITUNNEL) → vacancies (evaluation_json != '')
↓
cover_letter_worker (AITUNNEL) → vacancies (cover_letter != '', pending_approval)
↓
TG-клик "Откликнуться" → vacancies (queue_priority=1)
↓
apply_worker (CloakBrowser, flock) → vacancies (applied / apply_failed)

text

**Независимы (не трогают CloakBrowser):** `search_worker`, `assess_worker`, `cover_letter_worker`.
**Делят CloakBrowser:** `read_description`, `apply_worker` — через `flock` + `.browser-apply-pending`.

## 3. FILE LOCK (арбитр)

**Файлы:**
- `.browser-arbiter.lock` — `flock`, эксклюзивный.
- `.browser-apply-pending` — флаг: `apply` ждёт браузер.

**Логика `read_description`:**
while True:
if .browser-apply-pending exists: sleep(5); continue
fd = acquire_flock(.browser-arbiter.lock)
try:
if .browser-apply-pending exists: continue
result = await read_vacancy(...)
save_description(result)
finally:
release_flock(fd)
sleep(random 3-30)

text

**Логика `apply`:**
while True:
row = dequeue_apply()
if row is None: sleep(30); continue
touch(.browser-apply-pending)
fd = acquire_flock(.browser-arbiter.lock)
try:
result = await submit_application(...)
save_status(result)
finally:
release_flock(fd); rm(.browser-apply-pending)
sleep(300)

text

**`flock` в asyncio:** `fcntl.flock` блокирующий → `await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_EX)`.

## 4. МОДУЛИ (по одному)

### 4.1. `read_description.py` (СДЕЛАНО, коммиты 215982f, 6d44aea, b73fb55)

**Статус:** работает в фоне. Защита от цикла капчи: `defer_read_queue(vid, 60)` + пауза 300 сек; 2 капчи подряд → STOP. `cleanup_stale_chromium()`, `flock` + `.browser-apply-pending`.

### 4.1-исходно. `read_description.py` (первый, самый рискованный)

**Что делает:** берёт `discovered` с `description=''` и `queued_at IS NULL`, читает через CloakBrowser, сохраняет `description` + `company` + `company_url`. **Не делает assess.**

**Методы `database.py` (нужны):**
- `dequeue_read() -> row | None` — атомарно: `UPDATE vacancies SET queued_at=datetime('now') WHERE id = (SELECT id FROM vacancies WHERE status='discovered' AND (description IS NULL OR description='') AND queued_at IS NULL ORDER BY discovered_at LIMIT 1) RETURNING id, title, url`
- `update_description(vid, desc, company, company_url)` — `UPDATE vacancies SET description=?, company=?, company_url=?, queued_at=NULL WHERE id=?`

**Логика `read_description.py`:**
- `create_browser_backend(settings)` → `context`.
- `ensure_login()` — проверить сессию hh.ru (если слетела — VNC, ENTER).
- `flock` + `.browser-apply-pending`.
- `VacancySummary(id, title, url, search_query="")` → `read_vacancy(summary, None)`.
- `VACANCY_LOADED` → `update_description`.
- `VACANCY_REMOVED/HIDDEN/ACCESS_DENIED` → `status='expired'`.
- `CAPTCHA_DETECTED/PAGE_STRUCTURE_CHANGED/NETWORK_ERROR` → лог, `queued_at=NULL` (вернуть в очередь).
- Пауза `random(3, 30)`.

**Риски:**
- `ensure_login()` может потребовать VNC (как вчера).
- `kill -9` Chromium → `SingletonLock` → нужен cleanup.
- 1708 вакансий × 2 мин = 57 часов. **Долго.** Можно `--limit N` для теста.

### 4.2. `assess_worker.py` (СДЕЛАНО, коммит 4f4e976)

**Статус:** работает. `dequeue_assess` + `reject_by_assess` + `store_analysis` (queued_at=NULL). Остановлен (накопилось 140+ писем).

### 4.2-исходно. `assess_worker.py`

**Что делает:** берёт `discovered` с `description != ''` и `evaluation_json=''`, делает `assess` (AITUNNEL, `mimo-v2.6-flash`), сохраняет `evaluation_json` + `confidence` + `verdict` + `best_resume`.

**Методы `database.py`:**
- `dequeue_assess() -> row | None` — `discovered` с `description != ''` и `evaluation_json=''`.
- `update_evaluation(vid, eval_json, confidence, verdict, suitable)`.

**Логика:** `select_resume_stage0` → `select_resume_stage1` (если >1) → `assess` с `resume_text`. Без CloakBrowser.

**Риски:** LLM timeout 180 сек, `invalid_response` (mimo reasoning, но `max_output_tokens=16000` уже стоит).

### 4.3. `cover_letter_worker.py` (СДЕЛАНО, коммиты babd145, ec53695)

**Статус:** работает. `dequeue_cover_letter` + `request_approval` (queued_at=NULL). v4.4. LLM получает только {best_resume, resume_text} + vacancy, без profile. `_safe_letter` -> SystemExit(1) при greeting/footer.

### 4.3-исходно. `cover_letter_worker.py`

**Что делает:** берёт `discovered` с `evaluation_json != ''` и `cover_letter=''`, делает `generate_cover_letter` (AITUNNEL, `qwen3.7-flash`, v4.2), сохраняет, переводит в `pending_approval`.

**Методы `database.py`:**
- `dequeue_cover_letter() -> row | None` — `discovered` с `evaluation_json != ''` и `cover_letter=''`.
- `update_cover_letter(vid, letter, fit_summary, confidence)` — `UPDATE cover_letter, fit_summary, status='pending_approval'`.

**Логика:** `generate_cover_letter(best_resume, resume_text, v4.2)`. Без CloakBrowser.

**Риски:** `letter=''` (qwen reasoning, но `max_output_tokens=10000` для cover letter).

### 4.4. `apply_worker.py`

**Что делает:** берёт `pending_approval` с `queue_priority=1` и `queued_at IS NOT NULL`, `flock` + флаг, `submit_application`, переводит в `applied`/`apply_failed`/`expired`.

**Методы `database.py`:**
- `enqueue_apply(vid)` — `UPDATE queue_priority=1, queued_at=datetime('now') WHERE id=? AND status='pending_approval'`.
- `dequeue_apply() -> row | None` — `pending_approval` с `queue_priority=1` и `queued_at IS NOT NULL`, `UPDATE status='applying'`.
- `complete_apply(vid, status)` — `UPDATE status=?`.

**Логика:** `submit_application` через CloakBrowser, `flock` + флаг, cooldown 300 сек.

**Риски:** CloakBrowser 1 concurrent, `flock` с `asyncio.to_thread`, `ensure_login` (VNC).

### 4.5. `tg_bot.py` — кнопка «Откликнуться»

**Что меняется:** `_callback_handler` для `action='apply'` → `database.enqueue_apply(vid)` вместо `approve_and_apply`. Ответ: «В очереди: N-я».

**Метод `database.py`:** `count_apply_queue() -> int`.

## 5. ПОРЯДОК РАЗРАБОТКИ

1. **`read_description.py`** + методы `dequeue_read`, `update_description` → smoke на 3 вакансиях → полный запуск.
2. **`assess_worker.py`** + `dequeue_assess`, `update_evaluation` → smoke → запуск.
3. **`cover_letter_worker.py`** + `dequeue_cover_letter`, `update_cover_letter` → smoke → запуск.
4. **`apply_worker.py`** + `enqueue_apply`, `dequeue_apply`, `complete_apply` → smoke → запуск.
5. **`tg_bot.py`** — кнопка «Откликнуться» → `enqueue_apply`.

**После каждого модуля — коммит + smoke + запуск в фоне.**

## 6. ОТКРЫТЫЕ ВОПРОСЫ

1. **`read_description` — анонимный или с логином?** Решили: **с логином** (один профиль, риск бана меньше, чем два профиля).
2. **`read_description` — `max_concurrent=1`** (один профиль). Пауза между вакансиями 3–30 сек.
3. **`assess_worker` — параллельные вызовы?** Нет, последовательно (AITUNNEL может rate-limit). Пауза 2–5 сек.
4. **`cover_letter_worker` — параллельные?** Нет, последовательно.
5. **`apply_worker` — 300 сек между apply.** Уже в `approval.py`, но `apply_worker` — отдельный процесс, rate-limit in-memory не работает. Перенести `_last_apply_at` в БД или в `apply_worker` (в памяти процесса).
6. **Статусы:** `discovered` (read), `discovered` (assess), `discovered` (cover_letter) — три стадии с одним статусом. Различаются по полям `description`, `evaluation_json`, `cover_letter`. Это работает, но менее явно, чем отдельные статусы. **Решили:** поля, не статусы (SQLite `ALTER CHECK` — рискованно).

## 7. КОММИТЫ, КОТОРЫЕ УЖЕ ЕСТЬ

- `7d83b91` — `search_worker` + `search_page`
- `b8c83e7` — миграция `queued_at` + `queue_priority`
- `df441f8` — актуализация ARCHITECTURE + AI6-digest
- `8b54f37` — `classify_page` архивные
- `814987c` — `max_output_tokens=10000` + диагностика
- `ec59c59` — каскад Stage 0→1→2
- `6f7a7ed` — v4.2 промпт

## 8. ЧТО НЕ СДЕЛАНО (на 29.10.2026, 00:45)

- **`apply_worker.py`** — не сделан. Логика: `enqueue_apply`/`dequeue_apply`/`complete_apply`, `flock` + `.browser-apply-pending`, **стоп при капче сразу** (apply — тяжёлая операция для hh.ru, риск бана).
- **`tg_bot.py`** — кнопка «Откликнуться» -> `enqueue_apply` (сейчас `approve_and_apply`).
- **`watch_workers.sh`** — монитор воркеров (запланирован).
- **Уведомления в отдельный TG-канал** (`@asverinov` личка/канал) — запланировано.
- **Архитектурная чистка `profile.yaml`**: вынести `closing` в константу (`cover_letter.py`), убрать `cover_letter` из `profile.yaml` полностью, `candidate` (кроме `github_url` -> отдельное поле), убрать инструкции из `ai_analyzer.py` (`_COVER_LETTER_SYSTEM`, retry-промпт) — оставить один источник: дайджест.
- **Логирование `cover_letter` в `llm_requests`** — баг: письма не видны в учёте токенов.
- **`Unclosed client session`** — aiohttp-сессия не закрывается в `ManagedLLMProvider`.
- **Калибровка 19 вакансий на MIMO** — не сделана.
- **`main.py`** — Stage 1 в проде на AITUNNEL (сделан коммит f9d5ed0, но main.py-путь в проде не проверен).


