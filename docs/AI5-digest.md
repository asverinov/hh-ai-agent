# Дайджест сессии AI5

**Последнее обновление:** 16 октября 2026, сразу после коммита `e62dad4`.
**Как использовать:** в новой сессии скажи «дайджест» — тебе дадут команду
`cat docs/AI5-digest.md`. Копируешь её вывод целиком и присылаешь. Это
**единственное исключение** из правила «никогда не cat целые файлы».

---

## 🔴 ПРАВИЛА РАБОТЫ С ОПЕРАТОРОМ

Александр Сверинов — заказчик и оператор. **Не программист.**

- Команды — целиком, одним `bash`-блоком. Оператор копирует, вставляет, присылает вывод.
- Патчи файлов — python-скриптом, который сам меняет + проверяет + откатывает при ошибке. **Не «вставь строчку».**
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

---

## ⚙️ АВТОСТАРТ СЕРВИСОВ (сделано в AI5)

После ребута ноута всё поднимается автоматически, `Linger=yes`.
4 systemd-user-юнита в `~/.config/systemd/user/`:

| Юнит | Что запускает | Порт/дисплей |
|---|---|---|
| `hh-xvfb.service` | Xvfb :99 (1280x800x24) + ExecStartPre чистит lock | :99 |
| `hh-fluxbox.service` | fluxbox WM | :99 |
| `hh-x11vnc.service` | x11vnc (VNC-сервер для Windows TightVNC) | :5900 |
| `hh-gpt2giga.service` | gpt2giga --port 8090 --mode DEV | :8090 |

**Секреты:** `~/.config/systemd/user/hh-gigachat.env` (mode 600).
Токен меняешь в этом файле — `systemctl --user restart hh-gpt2giga`.

**Отключён паразит:** system-level `/etc/systemd/system/xvfb.service`
(`disabled`, `inactive`) — занимал `:99` от root.

**Проверка состояния сервисов:**

    systemctl --user --no-pager -l status hh-xvfb hh-fluxbox hh-x11vnc hh-gpt2giga | grep -E "●|Active:"
    ss -tlnp 2>/dev/null | grep -E ":(8090|5900)\b"
    curl -s -o /dev/null -w "HTTP %{http_code}\n" --max-time 5 \
      -X POST http://localhost:8090/v1/messages \
      -H "Content-Type: application/json" -H "anthropic-version: 2023-06-01" \
      -d '{"model":"GigaChat-2-Pro","max_tokens":10,"messages":[{"role":"user","content":"ok"}]}'

**VNC-вход на hh.ru (если сессия протухла):**
Windows TightVNC Viewer → `192.168.100.10:5900` (пароль пустой) →
Chromium → войти на hh.ru → вернуться в SSH, ENTER.

---

## 🧠 МОДЕЛИ

**LLM_MODEL=GigaChat-2-Pro** (через gpt2giga). **Не менять без P4.**

| Модель | Результат |
|---|---|
| **GigaChat-2-Pro** | ✅ Выбрана. Извлекает 7-10 требований, конкретные match |
| GigaChat-3-Pro | ❌ Мягкая: AppSec=true (должно false), теряет требования (2 вместо 7) |
| GigaChat-3-Ultra | ❌ Мягкая: AppSec=true, e-com=true |
| GigaChat-2-Max | ⚪ Не тестировалась глубоко |

**Проблема 2-Pro:** не умеет обобщать «роль ≠ функция»
(incident manager ≠ service manager). Частично лечится правилом 23 + few-shot,
не до конца.

**Freemium:** Ultra 50M / Max 25M / Pro 38M токенов до окт 2027.

---

## ✅ ЧТО СДЕЛАНО В AI5 (коммит `e62dad4`)

**Observability, шаг 1 (P1).** Собрать факты для датасета P4.

Добавлено в БД:
- `vacancies.description` — текст вакансии (было только `description_hash`)
- `llm_requests.vacancy_id` — привязка запроса к вакансии
- `llm_requests.prompt_version` — `sha256(system+user)[:12]`,
  автоматически ловит изменение промпта
- `llm_requests.raw_response` — сырой ответ LLM при успехе

**Политика БД:** `raw_response` пишем, **промпт (`system_instructions`)
и `user_content` — НЕ пишем**. Логи чисты.

**Прокидка `vacancy_id`:**
main.process_vacancy → analyzer.assess / generate_cover_letter →
ai_analyzer._request → LLMRequest.vacancy_id → managed._generate →
reserve_llm_request.

**Миграции** — паттерн `database.init`, `ALTER TABLE ADD COLUMN`,
идемпотентны.

**Проверено:** `main.py --check-llm` → `llm_requests.id=728`,
`prompt_version=d5ee43ed6cac`, `raw_response='OK'`.

**Тесты:** 5 новых регрессов (fake-анализаторы в `test_main`, схема
в `test_llm_database`) — починены. 0 регрессов.

---

## 📊 СОСТОЯНИЕ ТЕСТОВ (baseline после `e62dad4`)

**Полный прогон:** **27 failed, 344 passed**. Это **унаследованный долг**, не наш.

- `test_ai_analyzer`: 16 fail — старая схема `SuitabilityResult`
  (ждут `fit_points`/`reason`, модель выдаёт `requirements`/`verdict`)
- `test_main`: 5 fail — реальные падения, разбираться в P3
- ещё ~6 fail в других файлах

**Зелёные:** `test_config` (23), `test_hh_client` (36),
`test_database` (27), `test_llm_types` (11), `test_llm_managed` (21),
`test_llm_database` (5).

---

## 📋 ПРИОРИТЕТЫ (обновлено в AI5)

**P1 — Observability. Шаг 1 сделан, шаг 2 отложен.**
- ✅ `vacancy_id`, `prompt_version`, `raw_response`, `description`
- ⏸ Шаг 2: `post_processing_log` — **ждёт данных P4**.
  Проектировать под факты, а не гипотезу.

**P4 — Датасет и сравнение моделей. Приоритет №2.**
- 50 вакансий
- Прогнать агентом → в БД уже есть `raw_response`, `evaluation_json`,
  `requirements`
- Эталонная разметка через DeepSeek (10-15 калибровочных, потом полная)
- Скрипт: Precision / Recall / F1 по каждой модели
- **Классификация типов ошибок:** ложный отказ, ложное срабатывание,
  потерянное требование, выдуманное требование, неверный match,
  неверный verdict
- **Вывод: какая модель для какого шага** (извлечение / оценка / сопровод)
- Сравниваем: GigaChat-2-Pro vs 3.x vs DeepSeek

**P2 — Переделка пайплайна. Приоритет №3 (после P4).**
- Разделение шагов 2-5 на два LLM-вызова (извлечение + оценка)
- Модель под каждый шаг — по результатам P4
- Сопровод по `requirements` (L2-L4 с высоким match, L1 не упоминать)

**P3 — Тесты. Параллельно, дешёвая задача.**
- 16 fail в `test_ai_analyzer` — старая схема `SuitabilityResult`
- 5 fail в `test_main` + остальные (нужно ещё разобрать)

**P5 — Мелочи. По остаточному принципу.**
- Вилки «от 200 000» (`salary_to IS NULL`) уходят в `skipped_low_salary` — баг
- `envidence` → `evidence` постпроцессинг
- SQL `datetime('now','-10 minutes')` — ложная картина (ISO с `T` vs пробел)

**Порядок: P1(✅1) → P4 → P2 → P3 → P5.**

---

## 🐛 ИЗВЕСТНЫЕ ПРОБЛЕМЫ

**P2 (пайплайн):**
- Шаги 2-5 (извлечение/разбиение/приоритизация/оценка) слиты в один вызов
- Сопровод НЕ использует `requirements` — LLM сама решает, что подчеркнуть → вода
- Правило 24 не проверено на реальных AI-вакансиях (только синтетика)

**P5 (мелочи):**
- Открытые вилки «от 200 000» отсекаются как низкооплачиваемые (баг)
- `envidence` → `evidence` (постпроцессинг)
- SQL `datetime('now','-10 minutes')` — ISO-формат

---

## 📁 КЛЮЧЕВЫЕ ФАЙЛЫ

| Файл | Что там |
|---|---|
| `profile.yaml` (в корне!) | AI/LLM-якоря, технологии, стоп-факторы. В `.gitignore` |
| `config/resumes.yaml` | 4 ID резюме для hh |
| `.env` | `LLM_PROVIDER=anthropic_compatible`, `LLM_MODEL=GigaChat-2-Pro`, `APP_MODE=approval`, `ENABLE_REAL_APPLY=true`, `AUTO_APPLY_ENABLED=false`, `MAX_APPLICATIONS_PER_DAY=50` |
| `config.py` | `CandidateProfile`, `HHProfile`, `Settings`. `PROFILE_PATH="profile.yaml"` |
| `ai_analyzer.py` | Промпт (правила 1–24), `_coerce_match`, `_normalize`, `_COVER_LETTER_SYSTEM`, `_request` |
| `llm/managed.py` | `_generate` — retry, dirtyjson, blacklist-фильтр, `prompt_version` |
| `llm/types.py` | `LLMRequest` (с `vacancy_id`), `LLMResponse` |
| `llm/providers/anthropic_compatible.py` | Адаптер gpt2giga |
| `database.py` | Миграции в `init()`, `_migrate_status_check`, `applied_jobs` |
| `main.py` | `process_vacancy`, `run_search_cycle`, back-pressure, bootstrap, `check_llm` |
| `hh_client.py` | `_is_junior_title`, skip-блоки, `search_by_resume`, negotiations |
| `tg_bot.py` | Bootstrap, notify_cycle_done, show_pending, reply |
| `logging_setup.py` | RotatingFileHandler → `agent.log` (2 МБ × 3 бэкапа) |

---

## 🔬 ВАЛИДАТОРЫ В `ai_analyzer.py`

- `_coerce_match` (строка ~150) — приводит `"0.5"` (строку) к `float`
- `_normalize` (строка ~168) — срезает extra-поля, синтезирует verdict

**НЕ СУЩЕСТВУЕТ в коде:** `_downgrade_role_rejections`,
`_is_role_only_requirement`. Правило 23 — только текст в промпте,
постпроцессинга нет.

**Правила в промпте:**
- Правило 19 (~строка 407): СТОП-ФАКТОРЫ (без AI/LLM, добавлены Java, 1С)
- Правило 23 (~строка 441): ПЕРЕНОСИМЫЕ РОЛИ
- Правило 24 (~строка 457): ПЕРЕНОСИМЫЕ AI/LLM-ТРЕБОВАНИЯ

---

## 🛠️ КОМАНДЫ

**Запуск агента:**

    cd ~/hh-ai-agent && source venv/bin/activate && export DISPLAY=:99
    python3 main.py 2>&1 | tee /tmp/agent_$(date +%H%M).log

**Healthcheck LLM (быстро, один запрос):**

    cd ~/hh-ai-agent && source venv/bin/activate
    timeout 60 python3 main.py --check-llm

**Push в форк:** `cd ~/hh-ai-agent && git push`

**Pull из upstream (cherry-pick):**
`git fetch upstream && git cherry-pick <commit>`

**Состояние БД:**

    sqlite3 -header -column agent.db "SELECT status, COUNT(*) FROM vacancies GROUP BY status;"
    sqlite3 -header -column agent.db "SELECT id, title FROM vacancies WHERE status='pending_approval';"

**Схема БД:**

    sqlite3 agent.db ".schema vacancies"
    sqlite3 agent.db ".schema llm_requests"

**Тесты (по одному):**

    python3 -m pytest tests/test_config.py -q --no-header
    python3 -m pytest tests/test_llm_managed.py -q --no-header

---

## ⚠️ ВАЖНЫЕ НЮАНСЫ

- CloakBrowser free v146, **1 concurrent session**. Не жать две кнопки одновременно.
- `AUTO_APPLY_ENABLED=false` — только ручное approval через TG.
- `MAX_APPLICATIONS_PER_DAY=50`, но на первый отклик — по одной карточке.
- **GigaChat 3.x не использовать** — мягкие.
- Freemium: 50M Ultra / 25M Max / 38M Pro токенов до окт 2027.
- Правило «Песец» — оставлено.
- `.gitignore`: `.env`, `config/`, `agent.db`, `*.bak-*`, `patch_*.py`, `fix_*.py`.
- Старые токены GitHub могли утечь — при подозрении отозвать
  https://github.com/settings/tokens
- Xvfb **1280x800x24** (фактическое, не 1920x1080).

---

## 🔗 ССЫЛКИ

- Upstream: https://github.com/fikstt2/hh-ai-agent
- Форк: https://github.com/asverinov/hh-ai-agent
- Ветка: https://github.com/asverinov/hh-ai-agent/tree/my-local-work
- hh.ru резюме РП: https://hh.ru/resume/bc7c9e1aff0fe6b4ae0039ed1f614361526447
- Telegram: @asverinov_bot

---

## 📄 РЕЗЮМЕ (для P4)

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

**P4, шаг 1 — проверка достаточности логирования.**
Вопрос: хватает ли текущих полей (`description`, `raw_response`,
`evaluation_json`, `vacancy_id`, `prompt_version`) для сбора датасета?
Если да — идём собирать 50 вакансий. Если нет — минимальное расширение.

---

*Создан в AI5, коммит `e62dad4`. При обновлении — старую версию сохраняем
как `docs/AI5-digest.md.bak-<timestamp>`.*
