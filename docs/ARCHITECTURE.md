# ARCHITECTURE — карта проекта hh-ai-agent

**Назначение:** единый источник для «где что лежит и как работает». Читается
в начале каждой сессии вместо exploratory grep. Обновлять каждые 2-3 коммита.

**Что НЕ включать:** номера строк (дрифтуют), точные anchor'ы для патча
(они всегда находятся через `grep -n`), текущие значения `.env`.

---

## DATA FLOW

hh.ru API -> search_vacancies_by_resume (pagination, до pages из API)
   ├─ фильтр опыта (experience_filters)
   ├─ junior-title prefilter (_is_junior_title)
   ├─ salary hard-min (salary_hard_min из profile.yaml)
   ├─ is_applied (applied_jobs)
   ├─ previously_sent (pending_approval)
   └─ is_seen (seen_vacancies — архив всех ID из API)
   ↓
hh_client.read_vacancy (CloakBrowser) -> description
   ↓
database.discover -> vacancies (status=discovered, description, salary_*)
   ↓
vacancy_rejection_reason (pre-filter: excluded_positions/companies/keywords)
   ↓
   ├─ rejected -> status=rejected_by_filter
   ↓
analyzer.assess (LLM: GigaChat-2-Pro) -> SuitabilityResult
   (suitable, requirements, verdict, confidence, best_resume)
   ↓
   ├─ database.record_evaluation -> evaluations (мульти-LLM таблица)
   ├─ not suitable -> status=rejected_by_llm
   ↓
analyzer.generate_cover_letter (LLM: GigaChat-2-Pro, в pipeline отключено)
   ФАЗА B (в разработке): AITUNNEL DeepSeek с system_override=дайджест
   ↓
database.request_approval -> status=pending_approval, cover_letter
   ↓
tg_bot.send_preview -> TG-карточка (company, fit_summary, cover_letter, кнопки)
   ↓ (user: callback "apply")
approval.approve_and_apply -> hh_client.submit_application
   ├─ открывает модалку hh.ru
   ├─ _get_resume_id(best_resume) -> выбор в dropdown
   │  (label[data-magritte-select-option="<id>"])
   ├─ textarea для сопровода
   └─ submit -> status=applied

**Параллельные процессы:**
- negotiations_sync — при старте, синхронизирует applied_jobs с hh.ru (~2.5 мин)
- retry_due_analyses — retry для analysis_failed
- agent_loop — паузы между циклами (CHECK_INTERVAL_MINUTES=30)
- back-pressure — если pending_approval >= MAX_PENDING_RECOMMENDATIONS, fetch пропускается

---

## FILES (high-level)

| Файл | Что там |
|---|---|
| main.py | Pipeline orchestration: process_vacancy, run_search_cycle, retry_due_analyses, agent_loop, run() (entry point), check_llm |
| hh_client.py | hh.ru: search_vacancies_by_resume (API + pagination), read_vacancy (CloakBrowser), submit_application (apply через UI), _get_resume_id, negotiations_sync |
| ai_analyzer.py | LLM: промпт (правила 1–24 + 18a), _request (фабрика LLMRequest), assess (оценка), generate_cover_letter, _safe_letter, _COVER_LETTER_SYSTEM, _FORBIDDEN_LETTER_PHRASES |
| database.py | SQLite: миграции в init(), CRUD для vacancies, evaluations, seen_vacancies, applied_jobs, search_runs, llm_requests |
| cover_letter.py | Утилиты писем: format_portfolio_footer, cover_letter_validation_error (лимит max_length + 400), normalize_cover_letter_body |
| browser_backend.py | Wrapper для CloakBrowser/Playwright: create_browser_backend, BrowserLaunchError |
| config.py | Settings, load_settings, парсинг .env, CandidateProfile, HHProfile |
| approval.py | ApprovalService, ApprovalGuard, ApplicationPermission, PhysicalApplicationSender (Protocol) |
| tg_bot.py | Telegram UX: bootstrap, send_preview, notify_cycle_done, show_pending, reply-уведомления, отчёты «Стр. N/M» |
| logging_setup.py | RotatingFileHandler -> agent.log (2 МБ × 3 бэкапа) |
| instance_lock.py | single_instance — защита от второго запуска |
| mascots/tracker.py | «Песец» — шуточный маскот, шлёт картинки в TG (к LLM отношения не имеет) |
| llm/types.py | LLMRequest (с vacancy_id, session_id), LLMResponse |
| llm/managed.py | ManagedLLMProvider._generate: retry, dirtyjson, blacklist-фильтр, prompt_version |
| llm/factory.py | create_llm_provider — выбор провайдера по LLM_PROVIDER (GigaChat по умолчанию) |
| llm/providers/anthropic_compatible.py | Адаптер gpt2giga (Anthropic API) |
| llm/providers/openai_compatible.py | Адаптер AITUNNEL (OpenAI API), поддерживает session_id для кэша |
| scripts/backup_db.sh | Бэкап agent.db с ротацией (10 последних) |
| scripts/evaluate_models.py | Прогон моделей на калибровке (--provider gigachat/aitunnel) |
| scripts/compare_all.py | Матрица N×M, consensus, pairwise, CSV |
| scripts/compare_vs_manual.py | Precision/recall/F1 против ручной разметки |
| scripts/generate_cover_letters.py | Генерация писем через AITUNNEL + дайджест (вне pipeline) |
| docs/AI5-digest.md | Дайджест сессии (этот файл — дополнение к нему) |
| docs/datasets/ds_context_digest.md | Дайджест для оценки (~33 КБ) — как system prompt для AITUNNEL |
| docs/datasets/ds_cover_letter_digest.md | Дайджест для сопроводов (~27 КБ) |
| docs/datasets/calibration_set.json | 20 калибровочных вакансий |
| docs/datasets/manual_labels_20261017.txt | Ручная разметка (8 approve/reject + 3 borderline) |


---

## KEY FUNCTIONS

### main.py

**Entry point:**
- `run(settings)` — главный entry. Создаёт `Database`, `browser_backend`, `llm_provider`, `telegram`, `analyzer`. Запускает `asyncio.gather(telegram.start_polling(), agent_loop(...))`.

**Pipeline:**
- `process_vacancy(summary, settings, database, hh_client, analyzer, telegram, *, approval_service=None, **cover_letter_analyzer=None, cover_letter_digest="", cover_letter_session_id="", now_factory=None)` — полный цикл одной вакансии (оценка → evaluation → cover letter → TG).
- `run_search_cycle(settings, database, hh_client, analyzer, telegram, control, *, approval_service=None, auto_apply_batch_limit=None, now_factory=None, **cover_letter_analyzer=None, cover_letter_digest="", cover_letter_session_id="")` — один цикл поиска, вызывает `process_vacancy` для каждой вакансии.
- `retry_due_analyses(settings, database, hh_client, analyzer, telegram, control, *, now_factory=None, **cover_letter_analyzer=None, cover_letter_digest="", cover_letter_session_id="")` — retry для `analysis_failed`.
- `agent_loop(settings, database, hh_client, analyzer, telegram, control, approval_service=None, *, **cover_letter_analyzer=None, cover_letter_digest="", cover_letter_session_id="", now_factory=None, randint=random.randint)` — основной loop с паузами между циклами.

**Утилиты:**
- `check_llm(settings, provider_factory)` — healthcheck LLM (один запрос).
- `create_llm_provider(settings, database)` — из `llm/factory.py`, используется в `run()` и `check_llm`.
- `format_auto_batch_report(run, auto_applied, next_batch, settings)` — текст отчёта для TG.

### ai_analyzer.py

**Класс `VacancyAnalyzer(settings, provider)`:**
- `assess(vacancy_title, vacancy_description, *, vacancy_id=None, model_override=None, session_id=None, system_override=None) -> SuitabilityResult` — оценка вакансии.
- `generate_cover_letter(vacancy_title, vacancy_description, company_name="", *, vacancy_id=None, model_override=None, session_id=None, system_override=None) -> str` — тело письма (без приветствия/подписи).
- `_request(*, system_instructions, payload, operation, structured, vacancy_id=None, model_override=None, session_id=None, system_override=None) -> LLMRequest` — фабрика LLMRequest.
- `_safe_letter(raw) -> tuple[str, bool]` — валидация письма (срезает markdown, запрещённые фразы).
- `_candidate()`, `_candidate_for_analysis()` — JSON-представление кандидата.

**Модели:**
- `SuitabilityResult` — `suitable`, `requirements: list[Requirement]`, `verdict`, `confidence`, `best_resume`. `_normalize` в model_validator.
- `Requirement` — `level`, `label`, `text`, `match`, `evidence`.

**Константы:**
- `_COVER_LETTER_SYSTEM` — system prompt для писем (GigaChat, старый).
- `_FORBIDDEN_LETTER_PHRASES` — 20 фраз (штампы + «с уважением»).
- `_SERVICE_PREFIXES` — служебные префиксы от LLM («here is your cover letter» и т.п.).
- `_INJECTION_PHRASES` — защита от prompt injection.

### hh_client.py

**Класс `HHClient(context, settings, database, approval_guard)`:**
- `search_vacancies_by_resume(page_callback=None) -> VacancySearchResult` — поиск по RP через API, pagination до `pages`, `seen_vacancies`, `experience`/`junior`/`salary` фильтры.
- `read_vacancy(summary, captcha_handler) -> VacancyDetails` — открывает страницу через CloakBrowser, парсит description.
- `submit_application(permission: ApplicationPermission) -> bool` — apply через UI: клик «Откликнуться», выбор резюме, textarea, submit.
- `_get_resume_id(code: str) -> str | None` — читает `config/resumes.yaml`, возвращает ID по коду (RP/RSP/RPO/ROP).
- `fetch_negotiations_ids(max_pages=20) -> set[str]` — negotiations для sync applied_jobs.
- `ensure_login() -> bool` — проверка авторизации на hh.ru.
- `close()` — cleanup.

### database.py

**Класс `Database(path)`:**
- `init()` — миграции (`ALTER TABLE ADD COLUMN`, создание таблиц).
- `discover(job_id, title, company, company_url, url, description_hash, search_query, discovered_at, salary_from=None, salary_to=None, salary_currency="", description="") -> bool` — INSERT OR IGNORE в vacancies.
- `transition(job_id, expected, target, **fields)` — UPDATE status с whitelist полей (`evaluation_json` в whitelist с AI5).
- `request_approval(*, job_id, cover_letter, llm_decision, llm_reason, confidence, fit_summary="", evaluation_json="", now)` — переход в pending_approval.
- `approve(job_id, user_id, expected_user_id, now, ttl_minutes=30) -> str | None` — permit для apply.
- `record_evaluation(*, vacancy_id, llm_request_id, provider, model, prompt_version, suitable, confidence, verdict, requirements_json, is_calibration=0) -> bool` — INSERT OR REPLACE в evaluations.
- `latest_llm_request_meta(vacancy_id) -> dict | None` — последний успешный vacancy_analysis.
- `mark_seen(job_ids: list[str]) -> int`, `is_seen(job_id) -> bool` — seen_vacancies.
- `reserve_llm_request(*, provider, model, operation, now, daily_limit, vacancy_id=None, prompt_version="") -> int | None` — INSERT в llm_requests.
- `complete_llm_request(request_row_id, *, success, finished_at, input_tokens=None, output_tokens=None, latency_ms=None, provider_request_id=None, error_type="", raw_response=None) -> bool` — UPDATE.
- `is_applied(job_id)`, `mark_applied_external(job_id, title, url, now)` — applied_jobs.

### approval.py

- `ApprovalService.approve_and_apply(job_id, telegram_user_id) -> ApprovalResult` — approve + submit_application (с валидацией письма и permit).
- `ApprovalService.auto_apply(job_id) -> ApprovalResult` — авто-apply (если AUTO_APPLY_ENABLED=true, сейчас false).
- `ApprovalService.skip(job_id, telegram_user_id)`.
- `ApprovalGuard.claim(permission)` — проверка permit перед apply.

### cover_letter.py

- `format_portfolio_footer(letter, required_url, closing, max_length) -> str` — добавляет подпись + портфолио + нормализует.
- `cover_letter_validation_error(text, required_url, closing, max_length) -> str` — валидация (пустая строка = ok, иначе причина).
- `normalize_cover_letter_body(letter, required_url, closing)` — plain-text, без markdown.

### tg_bot.py

- `TelegramService.send_preview(vacancy, include_actions)` — отправить карточку в TG.
- `TelegramService.notify_bootstrap(stats, pending_count)` — при старте.
- `TelegramService.notify_cycle_done(run, pending_count, next_run_at)` — после цикла.
- `TelegramService.notify(text)` — простое уведомление.
- `TelegramService._callback_handler` — обработка кнопок (apply/skip/edit).

### llm/managed.py

- `ManagedLLMProvider._generate(request, response_model) -> tuple[LLMResponse, StructuredResult | None]` — retry, dirtyjson, blacklist, `prompt_version` (только system), `session_id` в reserve.
- `ManagedLLMProvider.generate_text`, `generate_structured` — обёртки.

### llm/providers/openai_compatible.py

- `OpenAICompatibleProvider.complete(request) -> LLMResponse` — POST `/chat/completions`, `session_id` в payload (если есть), json_mode.


---

## WHERE TO LOOK (быстрый индекс)

| Что ищем | Где |
|---|---|
| Как оценивается вакансия | `ai_analyzer.py`: `SuitabilityResult` + `VacancyAnalyzer.assess` |
| Как выбирается резюме для отклика | `hh_client.py`: `submit_application` → `_get_resume_id` → `label[data-magritte-select-option]` |
| Как генерируется сопровод | `ai_analyzer.py`: `generate_cover_letter` + `cover_letter.py`: `format_portfolio_footer` |
| Где правила промпта (1–24) | `ai_analyzer.py`: `assess` (system_instructions) |
| Где шаблон письма (старый) | `ai_analyzer.py`: `_COVER_LETTER_SYSTEM` |
| Retry / кэш / dirtyjson | `llm/managed.py`: `_generate` |
| Где `session_id` для кэша | `llm/providers/openai_compatible.py`: payload |
| Миграции БД | `database.py`: `init()` |
| TG-карточки (bootstrap, preview) | `tg_bot.py`: `send_preview`, `notify_bootstrap` |
| Кнопки apply/skip в TG | `tg_bot.py`: `_callback_handler` |
| Pre-filter (junior/salary/excluded) | `hh_client.py`: `_is_junior_title`, `search_vacancies_by_resume` + `vacancy_filter.py` |
| Список резюме (RP/RSP/RPO/ROP) | `config/resumes.yaml` + `hh_client.py`: `_get_resume_id` |
| Тип `SuitabilityResult` | `ai_analyzer.py`: класс `SuitabilityResult` |
| Как `evaluation_json` попадает в БД | `main.py`: `process_vacancy` → `evaluation_data` → `database.transition`/`request_approval` |
| Как выбрать модель для теста | `scripts/evaluate_models.py --provider gigachat|aitunnel --model X` |
| Как сравнивать модели | `scripts/compare_all.py` + `scripts/compare_vs_manual.py` |

---

## NON-OBVIOUS (что легко упустить)

- **`os.environ` перезаписывает `.env`** — в `config.py:load_settings`:
  `values.update(os.environ)`. Если делал `source .env` для скриптов —
  в этом shell `main.py` получит **старые** значения (например,
  `ENABLE_COVER_LETTER_GENERATION=false`). **Правило:** агент запускать в
  **чистом shell** (без `source .env`); `.env` подгружается через `load_settings`.

- **`cover_letter_analyzer` — отдельный провайдер.** AITUNNEL создаётся
  в `run()` через `_create_cover_letter_analyzer()`, если
  `ENABLE_COVER_LETTER_GENERATION=true` и `AITUNNEL_API_KEY` задан.
  Прокидывается через 5 функций: `process_vacancy`, `run_search_cycle`,
  `retry_due_analyses`, `agent_loop`, `run()`. В `process_vacancy` — если
  `cover_letter_analyzer` не None, генерирует письмо через AITUNNEL с
  `system_override=digest`, `session_id="cover-letter-main"`,
  `model_override="deepseek-v3.2"`. Иначе — fallback на штатный analyzer (GigaChat).

- **Retry в `generate_cover_letter` теряет дайджест.** На первом провале
  `_safe_letter` код заменяет `system_instructions` на **короткую** инструкцию
  («Напиши ТОЛЬКО 3 абзаца…») — **без дайджеста**. Модель справляется, но
  теряет весь контекст DS. **TODO:** добавить дайджест и на retry.

- **`record_evaluation` в `process_vacancy`** — пишется ДО `request_approval`. То есть `evaluations` наполняется сразу после assess, независимо от того, куда пойдёт вакансия (pending/approved/rejected).

- **`best_resume` в `evaluation_json`** — сохраняется в `process_vacancy` (поле `"best_resume": suitability.best_resume`). Используется в `submit_application` через `json.loads(vacancy.evaluation_json)`.

- **AITUNNEL provider создаётся отдельно** (не через `llm/factory.py`). Причина: `create_llm_provider` выбирает провайдер по `LLM_PROVIDER` из `.env`. Для тестов/сопроводов нужен **второй** провайдер параллельно с основным. Образец — `scripts/evaluate_models.py`: `create_aitunnel_provider`.

- **`session_id` для кэша** — передавать **одинаковый** для всей серии вызовов. Если разные — кэш не активируется. DeepSeek сам кэширует при совпадении префикса + `session_id`.

- **`system_override`** — заменяет `system_instructions` **целиком** (в `_request`). Для оценки — `ds_context_digest.md`. Для сопроводов — `ds_cover_letter_digest.md`. НЕ смешивать.

- **`best_resume`** — нормализуется в `_normalize` к RP/RSP/RPO/ROP. Если LLM вернёт «руководитель проектов» — сработает partial match, если «unknown» → None.

- **`_safe_letter`** — отбрасывает письмо, если находит `_FORBIDDEN_LETTER_PHRASES` (включая «с уважением», «буду рад обсудить»). Модель может написать «Добрый день!» — это не forbidden, но `retryable=True` вызовет повтор. Финальный текст собирает `format_portfolio_footer` (приветствие + тело + подпись).

- **`seen_vacancies`** — архив всех ID из API за всё время. Без него page=0 всегда даёт 100 дублей, и никогда не доходим до page=1+. Заполняется в `search_vacancies_by_resume` через `mark_seen`.

- **`page_callback` в `search_vacancies_by_resume`** — вызывается после каждой страницы. Используется для TG-отчётов `📄 Стр. N/M`. Если `TG_REPORT_PER_PAGE=false` — callback игнорируется.

- **`search_runs.new_vacancies`** — считает «новых на hh.ru в текущей выдаче», а не «новых в БД». Может завышать (в TG это уже не критично, но в отчётах путает).

- **Правило «Песец»** — `mascots/tracker.py`. Шлёт картинки песца при analyzing/approve/reject/applied. К LLM не имеет отношения. Можно отключить через `enable_pesec_mode()` — по умолчанию отключён.

- **`back-pressure`** — в `run_search_cycle` (main.py): `max_pending = settings.max_pending_recommendations` (60). Если `len(database.pending()) >= max_pending`, fetch пропускается, только анализ уже начатых. Не путать с `CHECK_INTERVAL_MINUTES` (пауза между циклами, сейчас 30).

---

## CHANGELOG (по коммитам)

- **B3 (18.10)**: интеграция генерации сопроводительных писем через AITUNNEL
  в pipeline: `cover_letter_analyzer` через 5 функций, `aitunnel_*` в
  `LLMSettings`, `description` в `Vacancy`, `ENABLE_COVER_LETTER_GENERATION=true`
- `f5c2551` (18.10): B2 — скрипт генерации сопроводительных писем через AITUNNEL + дайджест (~27 КБ)
- `202deb6` (18.10): B1 — `generate_cover_letter` получает `system_override`/`session_id`/`model_override`; лимит длины `max_length + 400` (1500 при 1100)
- `a3a5087` (18.10): A3 — apply с best_resume (клик по `label[data-magritte-select-option]` в dropdown hh.ru)
- `2e4fecb` (18.10): A1 — `SuitabilityResult.best_resume` (RP/RSP/RPO/ROP), TG-карточка «Резюме: X»
- `d86ee70` (18.10): обогащённый промпт + кэш через `session_id`, F1 1.00 у v3.2 и qwen
- `28e412e` (18.10): manual_labels в git
- `07d0062` (18.10): ручная разметка 11 вакансий + `compare_vs_manual.py`
- `8d937c9` (17.10): скрипты P4 (`evaluate_models`, `compare_all`, `export_calibration_blind`)
- `a528e57` (17.10): `model_override` + `evaluations` pipeline
- `05fc556` (17.10): пагинация до конца + `seen_vacancies` + `evaluations` + TG-отчёты
- `abaf273` (17.10): `scripts/backup_db.sh`
- `32cb620` (17.10): `max_pending_recommendations` из `.env`
- `d64dc3f` (17.10): `.gitignore` — чистка дублей
- `f31992b` (17.10): правила 18a + 23 (промпт)
- `e8097bb` (17.10): `evaluation_json` для rejected_by_llm и pending_approval
- `4a518b9` (17.10): инфраструктура датасета (`prompts.json`, `export_raw.py`)
- `173e1a0` (17.10): `prompt_version` = хеш только system
- `9237504` (16.10): дайджест AI5 v1
- `e62dad4` (16.10): observability шаг 1 — `description`, `vacancy_id`, `prompt_version`, `raw_response`

---

*Создан 18.10.2026. Обновлять каждые 2-3 коммита. При обновлении — старую версию
сохранять как `docs/ARCHITECTURE.md.bak-<timestamp>`.*
