# ARCHITECTURE — карта проекта hh-ai-agent

**Назначение:** единый источник для «где что лежит и как работает». Читается
в начале каждой сессии вместо exploratory grep. Обновлять каждые 2-3 коммита.

**Что НЕ включать:** номера строк (дрифтуют), точные anchor'ы для патча
(они всегда находятся через `grep -n`), текущие значения `.env`.

**Обновлено AI9 (02.11.2026):** исправлены врущие секции — Stage 0 (не reject-фильтр),
DATA FLOW (воркеры, не pipeline), `main.py` (DEPRECATED), `stage0_rules.yaml` → `stage1_hints.yaml`.

---

## КОНТЕКСТ: pipeline → workers (AI7–AI9)

**До 27.10.2026:** `main.py` — единый pipeline (`process_vacancy` вёл одну вакансию от поиска до apply). Legacy, **не менять с 27.10**, **DEPRECATED**.

**С 28.10.2026 (AI7):** 4 **независимых воркера** на очередях в БД (`queued_at`). Каждый — отдельный процесс через `nohup`. `main.py` — не запускается.

**С 02.11.2026 (AI9):** `assess_worker` портирован на `assess_pipeline` (Stage 1 + Stage 2), `stage0_rules.yaml` переименован в `stage1_hints.yaml` (удалён мёртвый `reject:`).

**Не сделано:** `apply_worker.py`. Apply — вручную через TG-карточку.

---

## DATA FLOW (воркеры, обновлено AI9)

    hh.ru API (similar_vacancies для 4 резюме)
       │  фильтр experience на уровне API (params["experience"])
       ↓
    search_worker.py — pre-filter (до БД):
       ├─ experience: локальная проверка item_exp (defense-in-depth, AI9)
       ├─ junior: _is_junior_title (маркеры из config.py HHProfile.junior_title_markers)
       ├─ salary: _parse_salary (hard_min из profile.yaml)
       ├─ excluded_positions: title_rejection_reason
       ├─ excluded_companies: inline
       └─ seen_vacancies: пропуск уже виденных
       ↓
    database.discover -> vacancies (status=discovered, salary_*)
       ↓
    read_description.py (CloakBrowser, flock + батчи)
       ├─ update_description -> status=discovered, description!=''
       ├─ CAPTCHA_DETECTED -> defer_read_queue + пауза 300 сек
       └─ 2 капчи подряд -> STOP (VNC)
       ↓
    assess_worker.py (assess_pipeline, AI9):
       ├─ Stage 1: Qwen (assess1.md v1.1) -> ScreenResult
       │  └─ suitable=false -> rejected_by_llm (stage=1, evaluation_json={"stage":1,...})
       └─ Stage 2: MiMo (assess2.md v1.4) + post-processing
          ├─ suitable=false -> rejected_by_llm (evaluation_json полный)
          └─ suitable=true  -> store_analysis (llm_decision=1)
       ↓
    cover_letter_worker.py (Qwen, ds_cover_letter_digest.md v4.7)
       └─ discovered с llm_decision=1 -> письмо -> pending_approval
       ↓
    tg_bot.send_preview -> TG-карточка (кнопки apply/skip/edit)
       ↓
    (manual) approval.approve_and_apply -> hh_client.submit_application
       ├─ открывает модалку hh.ru
       ├─ _get_resume_id(best_resume) -> dropdown (label[data-magritte-select-option])
       ├─ textarea для сопровода
       └─ submit -> status=applied

**Очереди в БД (`vacancies.queued_at`):**
- `queued_at` — «в работе» (ставит `dequeue_*`), снимает `update_*`/`request_approval`/`mark_analysis_failed`.
- `dequeue_read` -> `update_description` / `reset_read_queue` / `defer_read_queue(vid, minutes)`.
- `dequeue_assess` -> `store_analysis` (ok) / `reject_by_assess` (reject) / `mark_analysis_failed`.
- `dequeue_cover_letter` -> `request_approval`.
- **Правило:** любой метод, завершающий работу, обязан ставить `queued_at=NULL`.

**Параллельные процессы (независимы):**
- `search_worker` — API, наполняет `discovered`.
- `read_description` — CloakBrowser, добавляет `description`.
- `assess_worker` — LLM, Stage 1 + Stage 2.
- `cover_letter_worker` — LLM, письма.
- `migrate_assess` — одноразовый, переоценка накопленного.
- `retry_due_analyses` — retry для `analysis_failed` (только в `main.py`, legacy).

**Back-pressure:** не реализован в воркерах. В `main.py` — если `pending >= MAX_PENDING_RECOMMENDATIONS`, fetch пропускается.

### Reconcile (AI9, `scripts/reconcile_vacancies.py`)

Раз в N дней (или по требованию) — сверка БД с `similar_vacancies`:

1. **Phase 1 (collect):** обход `similar_vacancies` для 4 резюме (все страницы, 2 сек между страницами). Собирает `dict[id → experience_id]` в `reports/reconcile_seen.json`.
2. **Phase 2 (apply):** для каждого `discovered` в БД:
   - **есть в API + experience в allowed** → `set_evaluation_json(experience_id, reconcile=backfill, reconciled_at)`.
   - **есть в API + experience не в allowed** → `reject_by_filter(reason="experience: X")`.
   - **нет ни в одной из 4 выдач** → `mark_irrelevant(reason="not_in_similar_<дата>")`, status=`irrelevant`.
   - `pending_approval` **не трогается** (user видит в TG).

**Статус `irrelevant`** (AI9): «была в БД, но больше не в similar_vacancies». Не путать с `expired`: `expired` — подтверждено браузером (removed/hidden), `irrelevant` — hh больше не рекомендует (может быть активна).

**Дата прогона** — в `llm_reason` (`not_in_similar_2026-11-03`), не в `status` (enum стабилен, история прогонов видна).

**Результат AI9:** 779 discovered → 779 backfill (с `experience_id`), 323 irrelevant. `reject_exp=0` (API-фильтр работает). `errors=0`.

**Backfill через `/vacancies/{id}` — НЕВОЗМОЖЕН** (см. NON-OBVIOUS).

---

## CASCADE: Stage 1 → Stage 2 (обновлено AI9)

**Ключевое:** `profile.yaml` больше не идёт в LLM. Оценка — два независимых этапа.

### Pre-filter (бывший «Stage 0»)

**Stage 0 в assess не существует.** Отсев до БД — в `search_worker` + API hh.ru:
- junior, salary, excluded_positions/companies — из `profile.yaml`
- experience — API + локальная страховка (AI9)
- excluded_keywords — **не проверяется** (нужен `description`, а `search_worker` работает до чтения) — техдолг

`select_resume_stage0` **удалён в AI9** (Q1, решение Эксперта). Выбор резюме — только Stage 1 (`assess1.md`). `profile.yaml.stage0_rules` удаляется.

### Stage 1 (Скрининг)

- **Модель:** `qwen3.7-flash` (reasoning OFF, cache_control ON)
- **Промпт:** `docs/datasets/assess1.md` (v1.1)
- **Вход:** FULL вакансия + 4 summary + `hints` (из `stage1_hints.yaml`)
- **Выход:** `ScreenResult` (suitable, best_resume, confidence, reason)
- **Логика:** если `suitable=false` → `rejected_by_llm` (stage=1)

### Stage 2 (Оценка)

- **Модель:** `mimo-v2.6-flash` (reasoning ON, implicit cache)
- **Промпт:** `docs/datasets/assess2.md` (v1.4)
- **Вход:** `system = assess2.md + resume_text (выбранного)`, `user = FULL вакансия + alternatives`
- **Выход:** `SuitabilityResult` (suitable, requirements, verdict, hard_filter)
- **Логика:** если `suitable=true` → `store_analysis` (llm_decision=1)

### Post-processing (код-страховка)

- **Где:** `assess_pipeline.py:validate_stage2_result`
- **Что:** если `level in (1,2)` и `match < 0.5` → принудительно `is_required=True`, `hard_filter=True`
- **AI9:** было `level=1 && match<0.3`, расширено. Причина: 28% reject шло через verdict (модель ставит `level=2`/`match=0.4` на ядро роли).
- **Также:** `verdict` со словами «не подаваться» → `suitable=false` (post-process).

---

## ВОРКЕРЫ (актуально на AI9)

Фоновые процессы, каждый — отдельный `scripts/*.py` (`nohup env PYTHONPATH=. python3 ... > /tmp/*.log 2>&1 &`):

| Воркер | Что | LLM? | Браузер? | Промпт/модель |
|---|---|---|---|---|
| `search_worker.py` | API `similar_vacancies` для 4 резюме -> `discovered` | нет | нет | — |
| `read_description.py` | `discovered` без description -> CloakBrowser -> description | нет | **да** (flock + батчи) | — |
| `assess_worker.py` | `discovered` с description -> Stage 1 + Stage 2 -> `evaluation_json` | **да** | нет | assess1 v1.1 + assess2 v1.4 (AI9) |
| `cover_letter_worker.py` | `discovered` с `llm_decision=1` -> письмо v4.7 -> `pending_approval` | **да** | нет | ds_cover_letter_digest v4.7 |
| `apply_worker.py` | **НЕ СДЕЛАН** (apply вручную через TG) | — | — | — |

**Защита от банов (`read_description`):**
- `SLEEP_BETWEEN = (10, 30)` — пауза между вакансиями.
- Батчи: после 150-190 вакансий отдых 10-20 минут.
- 1-я капча: `defer_read_queue(vid, 60)` + пауза 300 сек.
- 2-я подряд: `STOP` (break) — нужно вмешательство (VNC).
- `dequeue_read` берёт `queued_at IS NULL OR queued_at <= now` (отложенные по времени).

---

## MIGRATION + AUTO-STOP (AI9)

`migrate_assess.py` — одноразовая переоценка `discovered + pending_approval` на v1.4 (запускать изолированно, без search_worker и read_description).

**Авто-стоп (AI9):** проверка каждые `STOP_CHECK_EVERY=25` вакансий.
- `suitable=true < 10%` (после 50) → стоп
- `hard_filter > 85%` (после 50) → стоп
- `verdict-filter > 50%` (после 50) → стоп
- `suitable=true + 'не подаваться'` (после 50) → стоп
- `invalid JSON > 5%` (после 20) → стоп
- При стопе — `🛑 АВТО-СТОП на N/M`, выход.

Пороги — константы в начале `migrate_assess.py`. Согласованы с Экспертом (в работе).

`check_migration_stats.py` — ручной чек той же логики (для мониторинга в фоне).

---

## LLM CONFIG (актуально на 02.11.2026)

| Параметр | Значение | Зачем |
|---|---|---|
| Stage 1 (в `assess_stage1`) | `qwen3.7-flash` (дефолт) | Скрининг (reasoning OFF, быстрый) |
| Stage 2 (в `assess_stage2`) | `mimo-v2.6-flash` (дефолт) | Глубокая оценка (reasoning ON) |
| `ASSESS_MODEL` из `.env` | **игнорируется** (AI9) | Было: Stage 2 в старом `analyzer.assess`. После порта — дефолты |
| `COVER_LETTER_MODEL` | `qwen3.7-flash` | Письма |
| `cache_control` | ON для Qwen, OFF для MiMo | Экономия токенов (50-90%) |
| `LLM_MAX_OUTPUT_TOKENS` | `16000` | mimo уходит в reasoning, 6000 не хватало |
| `LLM_TIMEOUT_SECONDS` | `120` | Оценка + письмо |
| `max_output_tokens` (cover) | `10000` | qwen обрезался на 6000 → letter=0 |
| `session_id` | `assess1-screen-v1`, `assess2-eval-v1`, `cover-letter-main` | Кэш AITUNNEL |
| `ds_cover_letter_digest.md` | **v4.7** (30.10.2026) | Промпт писем |
| `cover_letter.style` | **удалён** (не передаётся в LLM) | Дубль v4.7 |
| `cover_letter.closing` | двойные кавычки + `\n` | YAML не схлопывает |

**Кеш работает по-разному:**
- Qwen: `cache_control: {"type": "ephemeral"}` в `system`.
- MiMo: implicit (автоматически). Если передать `cache_control` для MiMo — ломается (`cached_tokens=3`).

**mimo-v2.6-flash — reasoning-модель.** При `max_tokens=6000` reasoning съедал всё, `content=null` → `invalid_response`. При 16000 — хватает.

**Диагностика `managed.py`:** при `empty/truncated` логируется `finish_reason` + `text_len`, `raw_response` сохраняется при ошибке.

---

## SCRIPTS (актуально на AI9)

| Скрипт | Что делает |
|---|---|
| `scripts/migrate_assess.py` | Массовая переоценка (v1.4) + авто-стоп (AI9). |
| `scripts/check_migration_stats.py` | Ручной мониторинг миграции (стоп-сигналы). |
| `scripts/audit_migration.py` | Сбор сэмплов для Эксперта. |
| `scripts/reconcile_vacancies.py` | **AI9:** сверка БД с `similar_vacancies` (backfill experience + irrelevant). Двухфазный: collect → apply. `--dry-run`, `--skip-collect`, `--resume RP`. |
| `scripts/backfill_experience.py` | **УДАЛЁН** (API `/vacancies/{id}` закрыт для физлиц). Заменён на `reconcile_vacancies.py`. |
| `scripts/fetch_descr_and_regen.py` | Дотягивает description для pending без него + каскад. |
| `scripts/regen_letter_only.py` | Догенерация cover_letter для pending. |
| `scripts/regenerate_pending.py` | Полный прогон каскада для pending. |
| `scripts/test_cascade.py`, `test_stage12_v2.py`, `test_stage3_cover.py` | Тесты каскада. |

---

## RATE-LIMIT

- `approval.py`: `asyncio.Lock` + `_last_apply_at` в памяти процесса, cooldown 300 сек.
- **Ограничение:** не переживает рестарт. **TODO:** заменить на `apply_worker.py` — очередь в БД + отдельный процесс.

---

## CLASSIFY_PAGE

- `checks` использует `count() > 0` вместо `is_visible()` (последний бросал на невидимых).
- Fallback через `page.locator("body").inner_text()` + `re.search` (игнорирует `\xa0`, разбивку по узлам).
- Маркеры архива: `Вакансия в архиве`, `в архиве с N`, `не принимает отклики`, `Работодатель, вероятно, уже нашел`.

---

## FILES (high-level, обновлено AI9)

| Файл | Что там |
|---|---|
| **main.py** | **DEPRECATED с 27.10.2026.** AI6 pipeline. Не запускать. Fallback до стабилизации воркеров (2-3 итерации без инцидентов). |
| hh_client.py | hh.ru: `search_vacancies_by_resume` (legacy, не используется), `fetch_similar_page` (API), `read_vacancy` (CloakBrowser), `submit_application` (UI), `_get_resume_id`, `_is_junior_title`, `_normalize_experience`, `_parse_salary` |
| ai_analyzer.py | LLM: `SuitabilityResult`, `_request`, `generate_cover_letter`, `_safe_letter`. `select_resume_stage0` удалён AI9. |
| assess_pipeline.py | **Оркестратор Stage 1 + Stage 2 + post-processing** |
| database.py | SQLite: миграции, CRUD, `queued_at`/`queue_priority`, `seen_vacancies`, `applied_jobs`, `evaluations`, `llm_requests` |
| cover_letter.py | Утилиты писем: `format_portfolio_footer`, `cover_letter_validation_error`, `normalize_cover_letter_body` |
| browser_backend.py | Wrapper для CloakBrowser/Playwright |
| config.py | `Settings`, `load_settings`, `CandidateProfile`, `HHProfile` (junior_title_markers дефолт — с `trainee`/`intern`, AI9), `ResumeProfile`, `Stage0Rule` |
| approval.py | `ApprovalService`, `ApprovalGuard`, `ApplicationPermission`, `PhysicalApplicationSender` |
| tg_bot.py | Telegram UX: `send_preview`, `notify_cycle_done`, `show_pending`, `_callback_handler` |
| logging_setup.py | RotatingFileHandler -> agent.log (2 МБ × 3) |
| instance_lock.py | `single_instance` |
| mascots/tracker.py | «Песец» — шуточный маскот (к LLM отношения не имеет) |
| llm/types.py | `LLMRequest` (с `cache_control`), `LLMResponse` (с `cached_tokens`) |
| llm/managed.py | `ManagedLLMProvider._generate`: retry, dirtyjson, blacklist, `prompt_version` |
| llm/factory.py | `create_llm_provider` — выбор провайдера по `LLM_PROVIDER` |
| llm/providers/anthropic_compatible.py | Адаптер gpt2giga (Anthropic API) |
| llm/providers/openai_compatible.py | Адаптер AITUNNEL (OpenAI API) |
| scripts/search_worker.py | API similar_vacancies + pre-filter (junior, salary, excluded_*, experience — AI9) |
| scripts/read_description.py | CloakBrowser -> description (flock, анти-бан) |
| scripts/assess_worker.py | Stage 1 + Stage 2 (assess_pipeline, AI9) |
| scripts/cover_letter_worker.py | Письмо v4.7 (читает `ds_cover_letter_digest.md` в runtime) |
| scripts/migrate_assess.py | Переоценка + авто-стоп (AI9) |
| **stage1_hints.yaml** | **Hints для Stage 1 (было stage0_rules.yaml).** `reject:` удалён AI9. Только `hint:`. |
| docs/datasets/assess1.md | Промпт скрининга (Stage 1, v1.1) |
| docs/datasets/assess2.md | Промпт оценки (Stage 2, v1.4) |
| docs/datasets/ds_cover_letter_digest.md | Дайджест для сопроводов (v4.7, ~27 КБ) |
| profile.yaml | Старая архитектура. Используется: `hh.*` (search_worker, read_description), `candidate.excluded_*`, `cover_letter.closing/max_length/required_portfolio_url`, `resumes.*` (summary + full_text). **В LLM не идёт.** Выпиливание — после стабилизации воркеров. |

---

## KEY FUNCTIONS (актуально на AI9)

### assess_pipeline.py

- `assess_stage1(analyzer, title, description, summaries, hints, ...) -> ScreenResult` — скрининг (Qwen).
- `assess_stage2(analyzer, title, description, best_resume, resume_text, alternatives, ...) -> SuitabilityResult` — оценка (MiMo).
- `validate_stage2_result(result) -> SuitabilityResult` — пост-процессинг: `level in (1,2) && match < 0.5` → `is_required=True`, `hard_filter=True` (AI9: было `level=1 && match<0.3`).
- `extract_hints(title, desc)` — из `stage1_hints.yaml`.

### scripts/assess_worker.py

- `assess_one(analyzer, settings, db, row, dry_run) -> str` — Stage 1 → (reject | Stage 2) → store/reject. **AI9:** на `assess_pipeline`, не на `analyzer.assess`.
- `build_evaluation_json(suitability, best_resume, reason) -> str` — `requirements` через `model_dump()` (с `is_required`), `hard_filter_triggered`/`hard_filter_reason` на верхнем уровне.

### scripts/search_worker.py

- `search_one_resume(hh, db, code, max_pages) -> (new, pages)` — `fetch_similar_page` + pre-filter (experience — AI9).
- `allowed_exp` — вычисляется один раз до цикла.

### scripts/migrate_assess.py

- `main()` — цикл с `processed`, `error_count`, `_quick_stop_check` каждые 25.
- `_quick_stop_check(db) -> str | None` — причина стопа или None (AI9).

### hh_client.py

- `fetch_similar_page(resume_id, page, per_page) -> dict` — API с фильтром `experience` (из `settings.profile.hh.experience_filters`).
- `read_vacancy(summary, captcha_handler) -> VacancyDetails` — CloakBrowser.
- `submit_application(permission) -> bool` — UI apply.
- `_is_junior_title(title, markers) -> str | None` — substring match, casefolded.
- `_normalize_experience(values) -> list[str]` — `от 3 до 6 лет` -> `between3And6`.
- `_parse_salary(item) -> (from, to, currency)`.

### database.py

- `discover(...)`, `transition(...)`, `request_approval(...)`, `approve(...)`.
- `record_evaluation(...)`, `latest_llm_request_meta(...)`.
- `reserve_llm_request(...)`, `complete_llm_request(...)`.
- `dequeue_read`/`dequeue_assess`/`dequeue_cover_letter`, `update_description`, `store_analysis`, `reject_by_assess`, `mark_analysis_failed`, `defer_read_queue`, `reset_read_queue`.
- **Правило:** любой метод, завершающий работу, обязан ставить `queued_at=NULL`.

### approval.py

- `ApprovalService.approve_and_apply(job_id, telegram_user_id) -> ApprovalResult` — approve + submit.
- `ApprovalGuard.claim(permission)` — проверка permit.

### cover_letter.py

- `format_portfolio_footer(letter, required_url, closing, max_length) -> str`.
- `cover_letter_validation_error(text, required_url, closing, max_length) -> str`.

---

## WHERE TO LOOK (быстрый индекс)

| Что ищем | Где |
|---|---|
| Как фильтруется вакансия до БД | `search_worker.py:search_one_resume` + `hh_client.fetch_similar_page` |
| Как оценивается вакансия | `assess_pipeline.py`: `assess_stage1` + `assess_stage2` |
| Как выбирается резюме | Только Stage 1 (`assess_pipeline.assess_stage1` + `assess1.md`) |
| Как генерируется сопровод | `ai_analyzer.py:generate_cover_letter` + `cover_letter.py:format_portfolio_footer` |
| Где правила скрининга | `docs/datasets/assess1.md` |
| Где правила оценки | `docs/datasets/assess2.md` |
| Где код-страховка `is_required` | `assess_pipeline.py:validate_stage2_result` |
| Retry / кэш / dirtyjson | `llm/managed.py:_generate` |
| Миграции БД | `database.py:init()` |
| TG-карточки | `tg_bot.py:send_preview`, `notify_bootstrap` |
| Pre-filter (junior/salary/excluded/experience) | `search_worker.py` + `profile.yaml` |
| Junior-маркеры (дефолт) | `config.py:HHProfile.junior_title_markers` |
| Список резюме (RP/RSP/RPO/ROP) | `config/resumes.yaml` + `hh_client._get_resume_id` |
| Тип `SuitabilityResult` | `ai_analyzer.py:class SuitabilityResult` |
| Hints для Stage 1 | `stage1_hints.yaml` + `assess_pipeline.extract_hints` |
| Авто-стоп миграции | `scripts/migrate_assess.py:_quick_stop_check` |

---

## profile.yaml: исходная идея vs текущее состояние

**Исходная идея (AI5–AI6):** `profile.yaml` — единый конфиг кандидата + правила предфильтрации резюме (2–3 из 4, чтобы не грузить LLM всеми четырьмя). `select_resume_stage0` читал `stage0_rules` и возвращал 1–3 кода. Stage 1 видел только их.

**Текущее состояние (AI9):**
- **`select_resume_stage0` удалён** (Q1, решение Эксперта). `stage0_rules.*` из `profile.yaml` удаляется.
- **Жёсткая предфильтрация резюме (2–3 из 4) не реализована.** Stage 1 видит все 4 summary + hints. Эксперт: «пока не возвращать, 52.4% true — в мягкости suitable, не в выборе резюме».
- **Pre-filter по опыту/зарплате/junior/positions/companies** — в `search_worker` + `prefilter_rules.yaml`. **`excluded_keywords`/`employment_type_flags`** — в `assess_worker` до Stage 1.
- **Оценка (Stage 1/2) `profile.yaml` не читает.** Только `resumes.*` (summary, full_text, title) — и они переезжают в `resumes.yaml` + `data/resumes/*.txt` (шаг 6.0).
- **Что осталось в `profile.yaml`:** `candidate.*` (кроме `excluded_*`), `hh.*` (кроме перенесённых), `cover_letter.*`, `resumes.*`. Полное выпиливание — после стабилизации воркеров.

**Если понадобится жёсткая предфильтрация (2–3 из 4):** делать в `assess_pipeline.assess_stage1` через `extract_hints` (уже есть likely_profile) или отдельным keyword-фильтром. Не через `select_resume_stage0`.

---

## NON-OBVIOUS (что легко упустить)

- **Stage 0 в assess не существует.** Отсев — в `search_worker` (pre-filter) + API. `select_resume_stage0` — это выбор резюме, не отсев вакансий.
- **`stage1_hints.yaml` (было `stage0_rules.yaml`).** Содержит **только** `hint:` для Stage 1. Pre-filter (junior, salary, experience, excluded_*) — в `profile.yaml` + `search_worker`. Дубля нет (AI9).
- **`main.py` DEPRECATED с 27.10.** Не запускать. Воркеры — единственный путь. Удаление — после стабилизации (2-3 итерации).
- **`assess_worker` — на `assess_pipeline`** (AI9). Старый `analyzer.assess()` + `ds_context_digest_v3.md` больше не используется. Модели — дефолты в `assess_stage1`/`assess_stage2`, `ASSESS_MODEL` из `.env` игнорируется.
- **`_is_junior_title`** — substring match по маркерам из `config.py:HHProfile.junior_title_markers`. AI9 добавил `trainee`, `intern` (были только в мёртвом `stage0_rules.yaml.reject.junior_title_keywords`).
- **Опыт: API + локальная страховка.** `fetch_similar_page` передаёт `experience` в API. AI9 добавил локальную проверку `item_exp` в `search_worker` до `discover()` (defense-in-depth).
- **`excluded_keywords` не проверяется нигде.** Нужен `description`, а `search_worker` работает до чтения. Техдолг. Планируется в `backfill_experience.py` (одноразово) + `read_description` (постоянно).
- **Кеш работает по-разному.** Qwen — `cache_control: {"type": "ephemeral"}`. MiMo — implicit. Если передать `cache_control` для MiMo — ломается (`cached_tokens=3`).
- **`is_required` — это боль.** Модель часто ставит `is_required=False` на ядро роли (AV, Shopify). Код-страховка в `assess_pipeline.py` принудительно ставит `True`, если `level in (1,2)` и `match < 0.5` (AI9: расширено).
- **`profile.yaml` больше не идёт в LLM.** Все промпты самодостаточны. `profile.yaml` — только `hh.*`, `candidate.excluded_*`, `cover_letter.closing`, `resumes.*`.
- **`_safe_letter` -> `SystemExit(1)`** при greeting/footer. LLM **не должна** писать greeting/footer — это делает скрипт. Если написала — баг кода/промпта, воркер **падает**, **не** retry.
- **`candidate` в `generate_cover_letter`** — **всегда** `{best_resume, resume_text}`, без `_candidate()`. В LLM уходит **только** резюме + вакансия + дайджест.
- **`closing`** — `profile.yaml`, **двойные кавычки** + `\n`. **Планируется** вынести в константу (`cover_letter.py`).
- **`style`** — удалён из `profile.yaml`, `config.py` дефолт `""`.
- **`os.environ` перезаписывает `.env`** — `config.py:load_settings`: `values.update(os.environ)`. Агент запускать в чистом shell (без `source .env`).
- **`cover_letter_analyzer` — отдельный провайдер.** AITUNNEL создаётся в `run()` (legacy main.py), прокидывается через 5 функций. В воркерах — создаётся отдельно в каждом.
- **Retry в `generate_cover_letter` теряет дайджест.** На первом провале `_safe_letter` заменяет `system_instructions` на короткую инструкцию без дайджеста. TODO.
- **`record_evaluation` в `process_vacancy`** (main.py) — до `request_approval`. В воркерах — `store_analysis`/`reject_by_assess` пишут сразу.
- **`best_resume` в `evaluation_json`** — сохраняется, используется в `submit_application`.
- **AITUNNEL provider создаётся отдельно** (не через `llm/factory.py`). Причина: `create_llm_provider` выбирает по `LLM_PROVIDER`.
- **`session_id` для кэша** — одинаковый для серии вызовов.
- **`system_override`** — заменяет `system_instructions` целиком.
- **`_safe_letter`** — отбрасывает письмо, если находит `_FORBIDDEN_LETTER_PHRASES`.
- **`seen_vacancies`** — архив всех ID. Без него page=0 всегда даёт 100 дублей.
- **`page_callback` в `search_vacancies_by_resume`** — legacy, для TG-отчётов.
- **Правило «Песец»** — `mascots/tracker.py`. К LLM не имеет отношения.
- **`back-pressure`** — только в `main.py` (legacy). В воркерах не реализован.
- **Авто-стоп в `migrate_assess`** — проверка каждые 20 вакансий. Только жёсткие факапы: `invalid_response > 3%`, `infra errors > 5%`, `timeslow > 2× baseline`, `mismatch > 3%`. Статистика (`suitable%`, `hard_filter%`) — в `check_migration_stats.py`, не стоп.
- **`GET /vacancies/{id}` ЗАКРЫТ для физлиц с апреля 2026** (подтверждено Пользователем). API отвечает `403 captcha_required` или `403 forbidden` на любой точечный запрос. **Не пытаться** — только через браузер (CloakBrowser, `read_vacancy`). `backfill_experience.py` был удалён из-за этого. Альтернатива — `similar_vacancies` (работает).
- **`similar_vacancies` — без rate limit на разумных объёмах** (проверено: 68 страниц × 100 items за ~2.5 мин, 0×429, 0×captcha). Пауза 2 сек между страницами — безопасна. `search_worker` использует 20–30 сек (из осторожности, можно уменьшить, но не нужно).
- **`irrelevant` ≠ `expired`.** `irrelevant` (AI9) — hh больше не рекомендует (может быть активна). `expired` — подтверждено браузером (removed/hidden). Не путать в отчётах.
- **`migrate_assess` НЕ трогает `pending_approval`** (AI9-fix): user их видит в TG, миграция перезапишет его решение. Только `discovered` с `description`. И **сохраняет `experience_id`** от reconcile (не сбрасывает `evaluation_json=''`).
- **Правила общения с Пользователем** — `docs/INTERACTION.md` (формат команд, бекапы, контентные файлы, «стоп всё»). Читать в начале сессии.

---

## CHANGELOG (по коммитам)

- **AI9 (03.11)**: **reconcile_vacancies.py** (сверка с `similar_vacancies`, 779 backfill + 323 irrelevant); **`irrelevant` статус**; **удалён `backfill_experience.py`** (API `/vacancies/{id}` закрыт); фикс `verdict-contains` в `validate_stage2_result` (startswith, не in); фикс `check_prefilter` — короткие аббревиатуры (РКО, PKI) через word boundary; фикс `_migrate_status_check` (реальный CREATE TABLE из sqlite_master, не хардкод); `migrate_assess` без pending + сохраняет `experience_id`; `docs/INTERACTION.md`.
- **AI9 (02.11)**: `assess_worker` → `assess_pipeline`; `stage0_rules.yaml` → `stage1_hints.yaml` (удалён `reject:`); `_is_junior_title` + `trainee`/`intern`; локальная проверка `item_exp` в `search_worker`; код-страховка `level<=2 && match<0.5`; авто-стоп в `migrate_assess`; ARCHITECTURE исправлен (Stage 0, pipeline, main.py legacy).
- **AI8 (02.11)**: Новая архитектура оценки (Stage 1 + Stage 2), выпиливание `profile.yaml` из LLM, код-страховка `is_required`, миграция. Батчи в `read_description`.
- **AI7 (28-29.10)**: Воркеры (`read_description`, `assess_worker`, `cover_letter_worker`), защита от капчи, чистка промптов писем.
- **AI6 (26.10)**: Stage 0 -> 1 -> 2 архитектура + v4.1 промпт.
- `f9d5ed0` (29.10): Stage 1 в проде на AITUNNEL (mimo).
- `4f4e976` (28.10): assess_worker.
- `215982f` (28.10): read_description.
- `ec59c59` (26.10): каскад Stage 0→1→2.
- `a528e57` (17.10): `model_override` + `evaluations`.
- `05fc556` (17.10): пагинация + `seen_vacancies` + `evaluations`.
- `abaf273` (17.10): `backup_db.sh`.
- `32cb620` (17.10): `max_pending_recommendations`.
- `e62dad4` (16.10): observability шаг 1.

---

*Создан 18.10.2026. Обновлён AI9 02.11.2026 (исправлены Stage 0, DATA FLOW, воркеры, `main.py` legacy, `stage1_hints.yaml`). При следующем обновлении — старую версию в `docs/bak/ARCHITECTURE.md.bak-<timestamp>`.*
