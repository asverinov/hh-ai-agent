# Дайджест сессии AI5

**Последнее обновление:** 17 октября 2026, после коммита `8d937c9`.
**Как использовать:** в новой сессии скажи «дайджест» — тебе дадут команду
`cat docs/AI5-digest.md`. Копируешь её вывод целиком и присылаешь. Это
**единственное исключение** из правила «никогда не cat целые файлы».

---

## 🔴 ПРАВИЛА РАБОТЫ С ОПЕРАТОРОМ

Александр Сверинов — заказчик и оператор. **Не программист.**

- Команды — целиком, одним `bash`-блоком. Оператор копирует, вставляет, присылает вывод.
- Патчи файлов — python-скриптом, который сам меняет + проверяет + откатывает при ошибке.
- Цикл: ИИ даёт команду → оператор запускает, присылает вывод → ИИ анализирует → следующая.
- Оператор **не должен искать факты сам** — ИИ просит конкретную команду.
- Вывод можно присылать вместе с командой.

**Жёсткие правила экономии контекста:**
- ❌ Никогда не `cat` целые файлы. Исключение: `docs/AI5-digest.md`.
- ✅ Только: `grep -n`, `sed -n 'X,Yp'`, `head -N`, `tail -N`, `awk`, `wc -l`.
- ✅ `git diff` → только `--stat` или `git show <commit> -- file | head -80`.
- ✅ Логи — только хвост: `tail -50 agent.log | grep -E "ERROR|WARNING"`.
- ✅ Одна команда — один вывод.
- ✅ `cp file file.bak-<что-то>` перед изменением.
- ✅ Обновлять дайджест каждые 6–8 часов активной работы.
- ⚠️ **При записи больших файлов** (дайджест, промпты) — маленькими блоками
  через `cat >>` по 60-80 строк. Длинные вставки рвутся.

---

## 🎯 ЦЕЛЬ И РОЛЬ

Автоматизация поиска и оценки вакансий на hh.ru через ИИ-агента:
поиск `similar_vacancies` → LLM-оценка → сопровод → TG-карточка →
**ручной отклик** (`ENABLE_REAL_APPLY=true`, `AUTO_APPLY_ENABLED=false`).

**Роль:** Александр Сверинов — заказчик и оператор (ищет работу).
**Upstream:** `fikstt2` (Евгений), `github.com/fikstt2/hh-ai-agent` — источник cherry-pick, **PR не делаем**.
**Форк:** `github.com/asverinov/hh-ai-agent` — основная линия.
**Ветка работы:** `my-local-work`.

---

## 🏗️ АРХИТЕКТУРА

**Хост:** Windows, ноут HP, i5, 32 ГБ. Hyper-V → Ubuntu 24.04 (виртуалка `hh-ag`).
**Сеть:** InternalSwitch, NAT, IP `192.168.100.10`. SSH: `ssh a@192.168.100.10`.
**Remote'ы git:**
- `origin` = asverinov/hh-ai-agent
- `upstream` = fikstt2/hh-ai-agent
- Ветка `my-local-work` привязана к `origin/my-local-work`
- Credentials: токен в `~/.git-credentials` (mode 600). Push без пароля.

**Telegram:** @asverinov_bot, user_id=215424700.

**gpt2giga:** внутри виртуалки на `localhost:8090` (Anthropic-compatible `/v1/messages`).
**AITUNNEL:** внешний OpenAI-совместимый шлюз, `https://api.aitunnel.ru/v1`.
Ключ в `.env` (`AITUNNEL_API_KEY`, `AITUNNEL_BASE_URL`). Баланс ~1149 ₽.

---

## ⚙️ АВТОСТАРТ СЕРВИСОВ (из AI5)

После ребута ноута всё поднимается автоматически, `Linger=yes`.
4 systemd-user-юнита в `~/.config/systemd/user/`:

| Юнит | Что запускает | Порт/дисплей |
|---|---|---|
| `hh-xvfb.service` | Xvfb :99 (1280x800x24) + ExecStartPre чистит lock | :99 |
| `hh-fluxbox.service` | fluxbox WM | :99 |
| `hh-x11vnc.service` | x11vnc (VNC-сервер) | :5900 |
| `hh-gpt2giga.service` | gpt2giga --port 8090 --mode DEV | :8090 |

**Секреты:** `~/.config/systemd/user/hh-gigachat.env` (mode 600).
Токен меняешь — `systemctl --user restart hh-gpt2giga`.

**Отключён паразит:** system-level `/etc/systemd/system/xvfb.service`
(`disabled`, `inactive`) — занимал `:99` от root.

**Проверка сервисов:**

    systemctl --user --no-pager -l status hh-xvfb hh-fluxbox hh-x11vnc hh-gpt2giga | grep -E "●|Active:"
    ss -tlnp 2>/dev/null | grep -E ":(8090|5900)\b"

**VNC-вход на hh.ru (если сессия протухла):**
Windows TightVNC Viewer → `192.168.100.10:5900` (пароль пустой) →
Chromium → войти на hh.ru → вернуться в SSH, ENTER.

**Агента запускаем вручную** (не через сервис):

    cd ~/hh-ai-agent && source venv/bin/activate && export DISPLAY=:99
    python3 main.py

Остановка — `Ctrl+C` в его вкладке.

---

## 🧠 МОДЕЛИ — обновлено после калибровки 17 окт

**Текущая production-модель:** `LLM_MODEL=GigaChat-2-Pro` (в `.env`).
**Но по калибровке 2-Pro — аутсайдер** (см. раздел P4 ниже).

| Модель | Согласие с DS-rules | Approve/19 | Цена/1M |
|---|---|---|---|
| **qwen3.7-flash** | **84.2%** ⭐ | 13 (68%) | ~6/26 ₽ |
| GigaChat-3-Ultra | 78.9% | 12 (63%) | freemium |
| deepseek-v4.1-flash | 77.8% | ~11 (61%) | 30/120 ₽ |
| GigaChat-2-Max | 73.7% | 9 (47%) | freemium |
| GigaChat-3-Pro | 73.7% | 15 (79%) | freemium |
| deepseek-v3.2 | 73.7% | 7 (37%) | ~36/54 ₽ |
| **GigaChat-2-Pro** | **63.2%** ⚠️ | 9 (47%) | freemium |

**Открытие:** `qwen3.7-flash` — лучший по согласию с ручным эталоном,
дешевле DeepSeek в ~20 раз. Кандидат на замену 2-Pro в production.

**Freemium GigaChat:** Ultra 50M / Max 25M / Pro 38M токенов до окт 2027.

**AITUNNEL:** баланс ~1149 ₽, ~370 моделей. Ключ и base_url в `.env`.

---

## ✅ ЧТО СДЕЛАНО В AI5 (коммиты e62dad4..8d937c9)

1. **Observability шаг 1** (`e62dad4`): `description`, `vacancy_id`,
   `prompt_version`, `raw_response` в `llm_requests`.
   `prompt_version = sha256(system_instructions)[:12]` (не зависит от вакансии).
2. **Дайджест AI5** (`9237504`): `docs/AI5-digest.md`.
3. **prompt_version только system** (`173e1a0`).
4. **Инфраструктура датасета** (`4a518b9`): `prompts.json`, `export_raw.py`, README.
5. **Фикс evaluation_json** (`e8097bb`): для `rejected_by_llm` и `pending_approval`
   через `transition`/`request_approval` (было только для `suitable=true`).
6. **Правила 18a + 23** (`f31992b`):
   - 18a: формула пропорции M/N для отраслевого опыта (+штраф ×0.5 для смежного)
   - 23: симметричное чтение (заголовок ≠ суть, но оба важны)
   - `prompt_version: e9fc00be54c6 → b4b61dcb327e`
7. **BIG: пагинация + seen_vacancies + evaluations + TG-отчёты** (`05fc556`):
   - `MAX_PAGES_PER_QUERY=0` = без лимита, до `pages` из API (было 2 — теряли 94%)
   - `seen_vacancies` — архив всех ID из API (иначе page=0 вечно даёт дубли)
   - `evaluations` — мульти-LLM таблица с `UNIQUE(vacancy_id, model, prompt_version)`
   - `page_callback` → TG-отчёты `📄 Стр. N/M` (флаг `TG_REPORT_PER_PAGE`)
8. **`max_pending_recommendations` из .env** (`32cb620`).
9. **`scripts/backup_db.sh`** (`abaf273`): бэкап с ротацией (10 последних).
10. **`--max-tokens`, `--provider aitunnel`** (`8d937c9`, `a528e57`):
    - `model_override` в `assess/_request`
    - `latest_llm_request_meta` в `database`
    - `record_evaluation` в `main.process_vacancy`
    - `evaluate_models.py` (прогон N моделей × M вакансий)
    - `compare_all.py` (матрица 9 источников, consensus, pairwise)

---

## 📊 P4: МУЛЬТИ-LLM КАЛИБРОВКА (17 окт)

**Цель:** выбрать лучшую модель/ансамбль для production-оценки вакансий.
**Проблема 2-Pro из дайджеста:** «не умеет обобщать роль ≠ функция» +
2 ошибки на 8 вакансиях (FP Rostic's, FN Non-IT).

**Калибровочный набор:** 19 вакансий (из 20 в JSON, одна без description):
- 10 positive (pending/applied, conf 0.79–0.95)
- 10 negative (rejected, conf 0.44–0.68)
- Покрытие: FP/FN из P4, отраслевые (18a), роль≠функция (23), AI/LLM
- `docs/datasets/calibration_set.json` (закоммичен)

**9 источников:**
- 4 GigaChat: 2-Pro, 2-Max, 3-Pro, 3-Ultra (через `evaluate_models.py --provider gigachat`)
- 2 DeepSeek web-сессии: DS-prompt (`@@@`-формат), DS-rules (Фибоначчи + 1000+ откликов)
- 3 AITUNNEL: deepseek-v4.1-flash, deepseek-v3.2, qwen3.7-flash

**Матрица:** `docs/datasets/calibration_full_matrix.csv` (не в git).

### 🎯 Ключевые открытия

1. **qwen3.7-flash — лучший по согласию с DS-rules (84.2%).**
   В 20 раз дешевле DS (~6₽/1M vs 30₽/1M), 13/19 approve.
   **Кандидат на замену 2-Pro в production.**

2. **2-Pro — аутсайдер (63.2% с DS-rules).**
   Против 3-Pro, v3.2, qwen — **47.4%** (антикорреляция, хуже случайного).
   Ошибся на **обоих кейсах P4** (Rostic's FP, Non-IT FN).
   Это не шум, а системный паттерн.

3. **DeepSeek API НЕ воспроизводит web-сессию.**
   v4.1-flash vs v3.2 = 61.1% между собой, оба vs DS-rules ~74-78%.
   Скорее всего, в web-сессии накоплен контекст (правила Фибоначчи,
   4 резюме, 1000+ откликов), которого нет в API-модели.
   **Для production API-DS нужен обогащённый промпт.**

4. **Консенсус 9 источников:**
   - 9/9: 3 вакансии (Senior PM, HR tech, Контур) — все согласны approve
   - 8/9: 4 (в т.ч. Customer Care крипто, Account Mgmt, Тетрика)
   - 7/9: 8
   - 6/9: 1 (Rostic's — реально спорная)
   - 5/9: 3 (iFCM, Партнёры Финтех, ГИП) — **реально borderline**

### 🎯 Два кейса из P4

**`138357711` Rostic's (FP):** 6/9 reject, 3 approve (2-Pro, 3-Pro, 3-Ultra).
Твой вердикт = reject. **2-Pro ошибается как в P4, так и здесь.**

**`138528225` RUTUBE Non-IT (FN):** 7/9 approve, 2 reject (2-Pro, 2-Max).
Твой вердикт = approve с оговорками. **2-Pro и 2-Max ошибаются.**

### 🎯 Borderline-класс (идея для P4-bis)

**`137526115` iFCM (Product Manager B2B SaaS):** 5/9. Ты сказал — интересная
вакансия, стоит кастомного резюме под продакта. **Такие кейсы (~5% рынка)
надо выделять в отдельную категорию `borderline`, не смешивать с approve/reject.**

**План P4-bis:**
- `SuitabilityResult.category: approve | borderline | reject`
- `evaluations.suitable` → `evaluations.category`
- Промпт: модель должна выдавать 3 категории
- TG: кнопка «⚠️ BORDERLINE»
- Backpressure: отдельный счётчик

**~2-3 часа рефакторинга**, отложено до стабилизации P4.

---

## 📊 СОСТОЯНИЕ ТЕСТОВ (baseline 17 окт)

**Полный прогон:** **27 failed, 344 passed**. Это **унаследованный долг**, не наш.
- `test_ai_analyzer`: 16 fail — старая схема `SuitabilityResult`
  (ждут `fit_points`/`reason`, модель выдаёт `requirements`/`verdict`)
- `test_main`: 5 fail — реальные падения, разбираться в P3
- ещё ~6 fail в других файлах

**Зелёные:** `test_config` (23), `test_hh_client` (36),
`test_database` (27), `test_llm_types` (11), `test_llm_managed` (21),
`test_llm_database` (5).

**Правило:** при каждом коммите сверяем с baseline. Если fail > 27 — регресс.

---

## 📋 ПРИОРИТЕТЫ (обновлено 17 окт)

**P1 — Observability. Шаг 1 сделан, шаг 2 отложен.**
- ✅ `vacancy_id`, `prompt_version`, `raw_response`, `description`
- ⏸ Шаг 2: `post_processing_log` — **ждёт данных P4**.

**P4 — Мульти-LLM калибровка. Основной шаг сделан.**
- ✅ 19 вакансий × 9 источников, матрица, consensus, pairwise
- ✅ `evaluate_models.py`, `compare_all.py`, `export_calibration_blind.py`
- ✅ Вывод: qwen3.7-flash лучший по согласию с DS-rules (84.2%)
- ⏸ **P4-bis:** borderline-класс (см. выше) — **отложено**
- ⏸ **P4-ter:** расширить калибровку до 40-50 вакансий — после ручной разметки
- ⏸ **Ручная разметка 5-10 спорных** (iFCM, Партнёры, ГИП, Rostic's) — **сейчас**
- ⏸ **DS API с обогащённым промптом** — если воспроизводим web-сессию

**P2 — Переделка пайплайна. Приоритет №3 (после P4).**
- Заменить 2-Pro на qwen3.7-flash (или ансамбль qwen + 2-Pro)
- Модель под каждый шаг — по результатам P4
- Сопровод по `requirements` (L2-L4 с высоким match, L1 не упоминать)

**P3 — Тесты. Параллельно, дешёвая задача.**
- 16 fail в `test_ai_analyzer` — старая схема `SuitabilityResult`
- 5 fail в `test_main` + остальные (нужно ещё разобрать)

**P5 — Мелочи. По остаточному принципу.**
- Вилки «от 200 000» (`salary_to IS NULL`) уходят в `skipped_low_salary` — баг
- `envidence` → `evidence` постпроцессинг
- SQL `datetime('now','-10 minutes')` — ложная картина (ISO с `T` vs пробел)
- **«СУММА равных» в отсечках:** если требование повторяется в разных
  формулировках (мягче через «ИЛИ», жёстче отдельно) — оценивать по жёсткой.
  Пример: «банки ИЛИ финтех/крипто ИЛИ беттинг» + «понимание криптоиндустрии» → обязателен крипто-опыт.

**Порядок: P1(✅1) → P4(✅основное) → P4-бис/тер → P2 → P3 → P5.**

---

## 🐛 ИЗВЕСТНЫЕ ПРОБЛЕМЫ

**P2 (пайплайн):**
- Шаги 2-5 (извлечение/разбиение/приоритизация/оценка) слиты в один вызов
- Сопровод НЕ использует `requirements` — LLM сама решает, что подчеркнуть → вода
- Правило 24 (AI/LLM) не проверено на реальных AI-вакансиях (только синтетика)
- **2-Pro систематически ошибается** (по калибровке P4)

**P4 (инфраструктура):**
- **DeepSeek — reasoning-модель:** `max_tokens` надо ставить 16000+ (дефолт 6000 мало)
- **DeepSeek v3.2 медленный:** 20-60 сек/вызов (qwen 67-85 сек) — учитывать в batch
- **qwen3.7-flash тоже reasoning** — очень медленный, но дешёвый и точный

**P5 (мелочи):**
- Открытые вилки «от 200 000» отсекаются как низкооплачиваемые (баг)
- `envidence` → `evidence` (постпроцессинг)
- SQL `datetime('now','-10 minutes')` — ISO-формат
- `search_runs.new_vacancies` показывает «новых на hh.ru», не «новых в БД» —
  путает (но в TG отчётах это уже не критично)

---

## 📁 КЛЮЧЕВЫЕ ФАЙЛЫ

| Файл | Что там |
|---|---|
| `profile.yaml` (в корне!) | AI/LLM-якоря, технологии, стоп-факторы. В `.gitignore` |
| `config/resumes.yaml` | 4 ID резюме для hh |
| `.env` | `LLM_PROVIDER`, `LLM_MODEL=GigaChat-2-Pro`, `APP_MODE=approval`, `ENABLE_REAL_APPLY=true`, `AUTO_APPLY_ENABLED=false`, `ENABLE_COVER_LETTER_GENERATION=false`, `TG_REPORT_PER_PAGE=true`, `MAX_PAGES_PER_QUERY=0`, `MAX_PENDING_RECOMMENDATIONS=60`, `AITUNNEL_API_KEY`, `AITUNNEL_BASE_URL` |
| `config.py` | `CandidateProfile`, `HHProfile`, `Settings`. `PROFILE_PATH="profile.yaml"` |
| `ai_analyzer.py` | Промпт (правила 1–24 + 18a), `_coerce_match`, `_normalize`, `_COVER_LETTER_SYSTEM`, `_request` (с `model_override`), `_safe_letter` |
| `llm/managed.py` | `_generate` — retry, dirtyjson, blacklist-фильтр, `prompt_version` |
| `llm/types.py` | `LLMRequest` (с `vacancy_id`, `model_override` через `_request`), `LLMResponse` |
| `llm/providers/anthropic_compatible.py` | Адаптер gpt2giga |
| `llm/providers/openai_compatible.py` | Адаптер AITUNNEL (base_url + api_key) |
| `database.py` | Миграции в `init()`, `_migrate_status_check`, `applied_jobs`, `seen_vacancies`, `evaluations`, `latest_llm_request_meta` |
| `main.py` | `process_vacancy`, `run_search_cycle`, back-pressure, bootstrap, `check_llm`, `record_evaluation` |
| `hh_client.py` | `_is_junior_title`, skip-блоки, `search_by_resume` (с пагинацией до конца и `page_callback`), negotiations |
| `tg_bot.py` | Bootstrap, notify_cycle_done, show_pending, reply, `📄 Стр. N/M` |
| `logging_setup.py` | RotatingFileHandler → `agent.log` (2 МБ × 3 бэкапа) |
| `scripts/backup_db.sh` | Бэкап `agent.db` с ротацией (10 последних) |
| `scripts/evaluate_models.py` | Прогон вакансий через модели (`--provider gigachat|aitunnel`, `--max-tokens`, `--dry-run`, `--force`) |
| `scripts/compare_all.py` | Матрица N×M, consensus, pairwise, CSV |
| `scripts/export_calibration_blind.py` | Слепая выгрузка 19 калибровочных |
| `docs/datasets/calibration_set.json` | 20 калибровочных (в git) |
| `docs/datasets/prompts.json` | Снапшот промптов на 16 окт (в git) |

---

## 🛠️ КОМАНДЫ

**Запуск агента:**

    cd ~/hh-ai-agent && source venv/bin/activate && export DISPLAY=:99
    python3 main.py

Остановка — `Ctrl+C` в его вкладке.

**Бэкап БД:**

    cd ~/hh-ai-agent
    ./scripts/backup_db.sh <метка>

Пример: `./scripts/backup_db.sh pre-calibration`

**Healthcheck LLM (один запрос):**

    cd ~/hh-ai-agent && source venv/bin/activate
    timeout 60 python3 main.py --check-llm

**Прогон моделей для калибровки:**

    cd ~/hh-ai-agent
    set -a; source .env; set +a
    python3 scripts/evaluate_models.py --calibration --provider aitunnel \
      --models deepseek-v3.2,qwen3.7-flash --max-tokens 16000 --delay 3

    # GigaChat:
    python3 scripts/evaluate_models.py --calibration --provider gigachat \
      --models GigaChat-2-Pro,GigaChat-3-Ultra --delay 3

    # Dry-run без записи:
    python3 scripts/evaluate_models.py --calibration --provider aitunnel --dry-run

**Матрица сравнения:**

    cd ~/hh-ai-agent
    python3 scripts/compare_all.py

**Push в форк:**

    cd ~/hh-ai-agent && git push

**Pull из upstream (cherry-pick):**

    git fetch upstream && git cherry-pick <commit>

**Состояние БД:**

    sqlite3 -header -column agent.db "SELECT status, COUNT(*) FROM vacancies GROUP BY status;"
    sqlite3 -header -column agent.db "SELECT model, COUNT(*) FROM evaluations WHERE is_calibration=1 GROUP BY model;"
    sqlite3 agent.db "SELECT COUNT(*) FROM seen_vacancies;"

**Тесты (по одному):**

    python3 -m pytest tests/test_config.py -q --no-header
    python3 -m pytest tests/test_llm_managed.py -q --no-header
    python3 -m pytest tests/ -q --no-header   # полный, baseline 27 fail

---

## ⚠️ ВАЖНЫЕ НЮАНСЫ

- **CloakBrowser free v146, 1 concurrent session.** Не жать две кнопки одновременно.
- **`AUTO_APPLY_ENABLED=false`** — только ручное approval через TG.
- **`ENABLE_COVER_LETTER_GENERATION=false`** — сопровод пока генерим вручную
  в другой сессии DS (экономия токенов GigaChat).
- **`MAX_APPLICATIONS_PER_DAY=50`**, но на первый отклик — по одной карточке.
- **Freemium GigaChat:** 50M Ultra / 25M Max / 38M Pro до окт 2027.
- **AITUNNEL баланс:** ~1149 ₽. DeepSeek v4.1-flash ~0.69₽/вызов,
  v3.2 ~0.52₽/вызов, qwen3.7-flash ~0.14₽/вызов.
- **`--max-tokens 16000`** для DeepSeek/Qwen (reasoning-модели). Дефолт 6000 мало.
- **Правило «Песец»** — `mascots/tracker.py`, шлёт картинки песца в TG
  при analyzing/approve/reject. К LLM отношения не имеет.
- `.gitignore`: `.env`, `config/`, `agent.db`, `*.bak-*`, `patch_*.py`,
  `fix_*.py`, `docs/datasets/*.txt`, `docs/datasets/compare.py`, `docs/datasets/export_raw.py`.
- Xvfb **1280x800x24** (фактическое, не 1920x1080).
- **При записи больших файлов** — маленькими блоками `cat >>` по 60-80 строк.

---

## 🔗 ССЫЛКИ

- Upstream: https://github.com/fikstt2/hh-ai-agent
- Форк: https://github.com/asverinov/hh-ai-agent
- Ветка: https://github.com/asverinov/hh-ai-agent/tree/my-local-work
- hh.ru резюме РП: https://hh.ru/resume/bc7c9e1aff0fe6b4ae0039ed1f614361526447
- Telegram: @asverinov_bot
- AITUNNEL: https://aitunnel.ru

---

## 📄 РЕЗЮМЕ (для P4 / P4-bis)

4 резюме, приложены к диалогу AI5:
- **РП** (Руководитель проектов) — **основное, поиск идёт по нему**
- РСП (Сервис-менеджер)
- РПО (Руководитель проектного офиса)
- РОП (Руководитель отдела поддержки)

Задел на будущее: сравнение вакансий со всеми резюме. Сейчас — только РП.
Резюме — эталон для сверки `profile.yaml` / `config/resumes.yaml`
и для валидации качества LLM-оценок.

---

## 🎬 СЛЕДУЮЩИЙ ШАГ

**Порядок A → C → B** (согласовано с оператором):

**A (сделано):** обновить дайджест.

**C (следующее) — ручная разметка 5-10 спорных вакансий.**
Твоя точка зрения как ground truth — единственный не-LLM эталон.

Спорные из матрицы (консенсус <6/9):
- `137526115` iFCM (Product Manager B2B SaaS) — 5/9, ты сказал «интересная,
  стоит кастомного резюме под продакта»
- `138554274` Партнёры Финтех — 5/9
- `138558358` ГИП на площадке — 5/9
- `138357711` Rostic's — 6/9, ты сказал reject (политика + HoReCa)
- `137969918` Customer Care крипто — 8/9, но DS-prompt ошибается

**Формат разметки:** `id → approve | reject | borderline + короткое обоснование`.
Сохраним в `docs/datasets/manual_labels_YYYYMMDD.txt`. Потом сравним
с 9 источниками, увидим «кто прав».

**B (после C) — расширить калибровку до 40-50 вакансий.**
Прогнать оставшиеся `rejected_by_llm` (с description) через qwen + v4.1-flash + 2-Pro.
~1-2 часа работы, ~50-100₽ на AITUNNEL.

**Цель:** заменить 2-Pro на qwen3.7-flash (или ансамбль) в production
после валидации на большем наборе.

---

## 📌 БЫСТРЫЙ СТАРТ В НОВОЙ СЕССИИ

1. Скажи «дайджест» — получишь `cat docs/AI5-digest.md`.
2. Проверь, что агент остановлен: `pgrep -af "python3 main.py"`.
3. Проверь сервисы: `ss -tlnp | grep -E ":(8090|5900)"`.
4. Проверь БД: `sqlite3 -header -column agent.db "SELECT status, COUNT(*) FROM vacancies GROUP BY status;"`.
5. Проверь AITUNNEL баланс: `sqlite3 agent.db "SELECT ..."` или `curl` через API.

**Текущее состояние (17 окт, 20:45):**
- 139 rejected_by_llm, 30 pending, 14 applied, 19 filter, 18 skipped
- 141 evaluations (132 калибровочных)
- 1568 seen_vacancies
- AITUNNEL баланс ~1149 ₽

---

*Создан в AI5, коммит `8d937c9`. При обновлении — старую версию сохраняем
как `docs/AI5-digest.md.bak-<timestamp>`.*
