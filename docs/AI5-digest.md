# Дайджест сессии AI5/AI6

**Последнее обновление:** 18 октября 2026, после коммита `f5c2551`.
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
- ⚠️ **В названиях задач/фаз писать полностью** — не «B1», а
  «генерация сопроводительных писем через AITUNNEL». Аббревиатуры
  понимают не все.

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
Ключ в `.env` (`AITUNNEL_API_KEY`, `AITUNNEL_BASE_URL`).
**Баланс на 18.10.2026:** 1079.24 ₽.
**Модели:** deepseek-v3.2 (36/54 ₽ за 1M), deepseek-v4.1-flash (30/120 ₽),
qwen3.7-flash (6/26 ₽). Кэш контекста работает через `session_id`
(89% экономии после первого вызова).

**4 резюме на hh.ru** (config/resumes.yaml):
- RP — Руководитель проектов (активное, поиск идёт по нему)
- RSP — Руководитель сервисных проектов
- RPO — Руководитель проектного офиса
- ROP — Руководитель отдела поддержки

**Search:** пока только по RP (через `search_by_resume`).
Задел на будущее: поиск по всем 4 резюме + дедупликация.

---

## ⚙️ АВТОСТАРТ СЕРВИСОВ (сделано в AI5)

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

## 🧠 МОДЕЛИ — после калибровки P4 (18 окт)

**Текущая production-модель для оценки:** `LLM_MODEL=GigaChat-2-Pro`.
**Но по калибровке 2-Pro — аутсайдер.** Решение отложено до production-фазы.

**Результаты калибровки на 8 ручных (Manual):**

| Модель | F1 после обогащения | Approve/19 | Цена/1M |
|---|---|---|---|
| **deepseek-v3.2** (AITUNNEL) | **1.00** ⭐ | 7 (37%) | ~36/54 ₽ |
| **qwen3.7-flash** (AITUNNEL) | **1.00** ⭐ | 13 (68%) | ~6/26 ₽ |
| GigaChat-2-Max | 0.80 | 9 (47%) | freemium |
| DS-prompt (web) | 0.80 | 14 (74%) | — |
| GigaChat-3-Ultra | 0.67 | 12 (63%) | freemium |
| deepseek-v4.1-flash | 0.67 | 11 (61%) | 30/120 ₽ |
| GigaChat-2-Pro | 0.50 | 9 (47%) | freemium |
| GigaChat-3-Pro | 0.50 | 15 (79%) | freemium |
| **DS-rules (web, эталон)** | **1.00** | 13 (68%) | — |

**Ключевые открытия:**
- **Обогащённый промпт** (дайджест DS ~33 КБ как system prompt) поднял F1
  у v3.2 с 0.67 → **1.00**, у qwen с 0.40 → **1.00**.
- **Кэш DeepSeek** через `session_id` = **89% экономии** (0.72 → 0.08 ₽/вызов).
- **2-Pro системный аутсайдер** — 47.4% согласия с 3-Pro/v3.2/qwen (хуже случайного).
- **qwen3.7-flash** — самый дешёвый (~6₽/1M) и с F1=1.00, но медленный
  (67-85 сек/вызов). **v3.2** — быстрее (~20-30 сек), дороже в 5 раз, F1=1.00.

**Решение по production:** **deepseek-v3.2** (баланс скорости и цены при
одинаковом качестве). Замена GigaChat — в финале сессии.

**Freemium GigaChat:** Ultra 50M / Max 25M / Pro 38M токенов до окт 2027.

**Признаки правил в промпте GigaChat:**
- Правило 19 (~строка 407): СТОП-ФАКТОРЫ
- Правило 18a (~строка 441): отраслевой опыт — формула M/N
- Правило 23 (~строка 457): переносимые роли
- Правило 24 (~строка 470): переносимые AI/LLM-требования
- `prompt_version` = `b4b61dcb327e` (хеш только system_instructions)

---

## ✅ ЧТО СДЕЛАНО В AI5/AI6 (12 коммитов)

**Наблюдаемость:**
- `e62dad4` — `description`, `vacancy_id`, `prompt_version`, `raw_response` в `llm_requests`
- `173e1a0` — `prompt_version = sha256(system_instructions)[:12]` (не зависит от вакансии)
- `e8097bb` — `evaluation_json` для `rejected_by_llm` и `pending_approval`
  через `transition`/`request_approval`

**Правила оценки:**
- `f31992b` — правила 18a (формула пропорции M/N для отрасли) + 23
  (симметричное чтение названия и тела). `prompt_version: e9fc00be54c6 → b4b61dcb327e`

**Пагинация + seen_vacancies + evaluations:**
- `05fc556` — `MAX_PAGES_PER_QUERY=0` (без лимита, до `pages` из API; было 2 — теряли 94%)
  - `seen_vacancies` — архив всех ID из API (иначе page=0 вечно даёт дубли)
  - `evaluations` — мульти-LLM таблица с `UNIQUE(vacancy_id, model, prompt_version)`
  - `page_callback` → TG-отчёты `📄 Стр. N/M` (флаг `TG_REPORT_PER_PAGE`)

**Инфраструктура датасета P4:**
- `4a518b9` — `prompts.json`, `export_raw.py`, README
- `07d0062` — ручная разметка 11 вакансий (8 approve/reject + 3 borderline)
- `28e412e` — `manual_labels` в git (было в `.gitignore`)
- `a528e57` — `model_override` + `evaluations` pipeline
- `8d937c9` — скрипты `evaluate_models.py`, `compare_all.py`, `export_calibration_blind.py`

**Обогащение промпта + кэш:**
- `d86ee70` — `session_id` для кэша DeepSeek, `system_override` в `_request/assess`,
  `docs/datasets/ds_context_digest.md` (~33 КБ, дайджест DS).
  F1: v3.2 0.67 → **1.00**, qwen 0.40 → **1.00**

**best_resume — выбор резюме для отклика:**
- `2e4fecb` (A1) — `SuitabilityResult.best_resume` (RP/RSP/RPO/ROP),
  TG-карточка показывает «Резюме: X», `evaluation_json` содержит `best_resume`
- `a3a5087` (A3) — в `submit_application` клик по
  `label[data-magritte-select-option="<resume_id>"]` в dropdown hh.ru.
  Fallback на RP. Проверено вживую на 138564721 (RSP/RPO/ROP переключаются)

**Сопроводительные письма через AITUNNEL DeepSeek:**
- `202deb6` (B1) — `generate_cover_letter` получает `system_override`, `session_id`,
  `model_override`. Мягкий лимит длины: `max_length + 300 → + 400` (1500 при 1100)
- `f5c2551` (B2) — скрипт `scripts/generate_cover_letters.py`,
  `docs/datasets/ds_cover_letter_digest.md` (~27 КБ, дайджест от DS web).
  Проверено: 9/9 вакансий, ~132 сек, ~1.4 ₽ с кэшем. Стоимость ~0.10-0.15 ₽/письмо

---

## 📊 P4: МУЛЬТИ-LLM КАЛИБРОВКА

**Калибровочный набор:** 19 вакансий (из 20 в `calibration_set.json`,
одна без `description`).
- 10 positive (pending/applied, conf 0.79–0.95)
- 10 negative (rejected, conf 0.44–0.68)
- Покрытие: FP/FN из P4 (Rostic's, Non-IT), отраслевые (18a), роль ≠ функция (23), AI/LLM

**Ручная разметка** (`docs/datasets/manual_labels_20261017.txt`, в git):
- 8 approve/reject: 2 approve, 6 reject
- 3 borderline: `138558358` (ГИП), `138385238` (Chief of Staff), `137526115` (iFCM Product Manager)

**Матрица:** `docs/datasets/calibration_full_matrix.csv` (не в git).

### 🎯 Ключевые открытия

1. **Обогащённый промпт = F1 1.00.** DS-rules (web) остаётся эталоном (8/8).
   С обогащением v3.2 и qwen **достигли 1.00** (до обогащения: v3.2 0.67, qwen 0.40).
   Это доказало: качество web-сессии DS воспроизводится через API **при правильном
   system prompt**.

2. **Кэш DeepSeek через `session_id`** = 89% экономии (0.72 → 0.08 ₽/вызов).
   Промпт 10K+ токенов кэшируется полностью. TTL кэша ~10 минут.

3. **2-Pro системный аутсайдер** — 47.4% согласия с 3-Pro/v3.2/qwen (хуже случайного).
   Ошибся на **обоих** кейсах P4 (Rostic's FP, Non-IT FN).

4. **qwen3.7-flash** — самый дешёвый (~6₽/1M) с F1=1.00, но медленный
   (67-85 сек/вызов).

5. **Два кейса из P4 (для сверки):**
   - `138357711` Rostic's — ручной reject. 2-Pro/3-Pro/3-Ultra ошиблись (approve).
   - `138528225` RUTUBE Non-IT — ручной borderline. 2-Pro/2-Max ошиблись (reject).

### Что НЕ закрыто в P4

- **Расширение калибровки с 8 до 20-30** (для устойчивости F1) — не начато
- **Borderline-класс** (категория `approve | borderline | reject` в
  `SuitabilityResult`) — отложено
- **«Сумма равных» в отсечках** (когда требование дублируется в жёсткой и
  мягкой формулировке — оценивать по жёсткой) — отложено

---

## 📋 ПРИОРИТЕТЫ (на 18 октября 2026)

### Фаза A: выбор резюме для отклика
- ✅ A1 (best_resume в модели + TG + JSON) — сделано (`2e4fecb`)
- ✅ A3 (apply с best_resume через dropdown hh.ru) — сделано (`a3a5087`)
- ⏸ **A2 — search по всем 4 резюме** (сейчас только RP) — **не начато**

### Фаза B: сопроводительные письма через AITUNNEL DeepSeek
- ✅ B1 (`generate_cover_letter` с `system_override`/`session_id`) — сделано (`202deb6`)
- ✅ B2 (скрипт `generate_cover_letters.py` + дайджест) — сделано (`f5c2551`)
- ⏸ **B3 — интеграция в `main.py`** (автоматическая генерация писем в pipeline) — **следующая задача**
- ⏸ B4 — включить `ENABLE_COVER_LETTER_GENERATION=true` в `.env`

### Фаза C: расширение калибровки
- ⏸ Расширение с 8 до 20-30 вакансий (для устойчивости F1)

### Фаза D: production
- ⏸ Заменить `LLM_MODEL=GigaChat-2-Pro` на `deepseek-v3.2` в pipeline оценки
- ⏸ Smoke на реальной вакансии → check → коммит

### P5 — мелочи (по остаточному принципу)
- Вилки «от 200 000» (`salary_to IS NULL`) уходят в `skipped_low_salary` — баг
- `envidence` → `evidence` постпроцессинг
- SQL `datetime('now','-10 minutes')` — ISO-формат

**Порядок:** дайджест → **B3 (интеграция сопроводов)** → B4 → A2 → C → D → P5.

---

## 🎬 СЛЕДУЮЩИЙ ШАГ — B3: интеграция генерации сопроводов в pipeline

**Проблема сейчас:**
`main.py` вызывает `generate_cover_letter` **без** `system_override`/`session_id`,
через GigaChat. А `ENABLE_COVER_LETTER_GENERATION=false` — вообще не вызывает.
Карточки в TG приходят **без сопроводительных писем**.

**Что нужно сделать (1-2 часа):**
1. В `main.run()` создать **второй** `ManagedLLMProvider` (AITUNNEL adapter)
   рядом с GigaChat-провайдером
2. Создать **второй** `VacancyAnalyzer` для сопроводов
3. Загрузить `ds_cover_letter_digest.md` **один раз** при старте
4. В `process_vacancy` вызывать `analyzer_cl.generate_cover_letter(...)` с:
   - `model_override="deepseek-v3.2"`
   - `system_override=digest_text` (дайджест из памяти)
   - `session_id="cover-letter-main"` (для кэша)
5. `ENABLE_COVER_LETTER_GENERATION=true` в `.env`

**Результат:** каждая approve-вакансия получит автоматическое письмо в TG.
Стоимость: ~0.10-0.15 ₽/письмо. Без кэша — ~0.60 ₽/письмо.

**Файлы для правки:** `main.py` (~30-40 строк), `.env` (1 строка).

---

## 📁 КЛЮЧЕВЫЕ ФАЙЛЫ

| Файл | Что там |
|---|---|
| `profile.yaml` (в корне!) | AI/LLM-якоря, технологии, стоп-факторы. В `.gitignore` |
| `config/resumes.yaml` | 4 ID резюме (RP active, RSP/RPO/ROP ждут) |
| `.env` | `LLM_PROVIDER`, `LLM_MODEL=GigaChat-2-Pro`, `ENABLE_COVER_LETTER_GENERATION=false`, `MAX_PAGES_PER_QUERY=0`, `MAX_PENDING_RECOMMENDATIONS=60`, `TG_REPORT_PER_PAGE=true`, `AITUNNEL_API_KEY`, `AITUNNEL_BASE_URL` |
| `ai_analyzer.py` | Промпт (правила 1–24 + 18a), `_request` (с `model_override`, `system_override`, `session_id`), `_safe_letter`, `_COVER_LETTER_SYSTEM` |
| `llm/managed.py` | `_generate` — retry, dirtyjson, blacklist, `prompt_version` |
| `llm/types.py` | `LLMRequest` (`vacancy_id`, `session_id`) |
| `llm/providers/openai_compatible.py` | Адаптер AITUNNEL (`session_id` в payload) |
| `database.py` | Миграции, `applied_jobs`, `seen_vacancies`, `evaluations`, `latest_llm_request_meta` |
| `main.py` | `process_vacancy`, `run_search_cycle`, `record_evaluation`, `check_llm` |
| `hh_client.py` | `_is_junior_title`, `search_by_resume` (пагинация до конца), `_get_resume_id`, `submit_application` (выбор резюме через dropdown) |
| `tg_bot.py` | Bootstrap, notify_cycle_done, show_pending, reply, `📄 Стр. N/M` |
| `cover_letter.py` | `format_portfolio_footer`, `cover_letter_validation_error` (лимит 1500) |
| `scripts/backup_db.sh` | Бэкап `agent.db` с ротацией (10 последних) |
| `scripts/evaluate_models.py` | Прогон моделей (`--provider gigachat\|aitunnel`, `--max-tokens`, `--system-from-file`, `--session-id`) |
| `scripts/compare_all.py` | Матрица N×M, consensus, pairwise, CSV |
| `scripts/compare_vs_manual.py` | Precision/recall/F1 против ручной разметки |
| `scripts/generate_cover_letters.py` | Генерация сопроводов через AITUNNEL с дайджестом |
| `docs/datasets/ds_context_digest.md` | Дайджест для оценки (~33 КБ) |
| `docs/datasets/ds_cover_letter_digest.md` | Дайджест для сопроводов (~27 КБ) |
| `docs/datasets/calibration_set.json` | 20 калибровочных (в git) |
| `docs/datasets/manual_labels_20261017.txt` | Ручная разметка (в git) |

---

## 🛠️ КОМАНДЫ

**Запуск агента:**

    cd ~/hh-ai-agent && source venv/bin/activate && export DISPLAY=:99
    python3 main.py

Остановка — `Ctrl+C` в его вкладке.

**Бэкап БД:**

    cd ~/hh-ai-agent && ./scripts/backup_db.sh <метка>

**Healthcheck LLM:**

    cd ~/hh-ai-agent && source venv/bin/activate
    timeout 60 python3 main.py --check-llm

**Прогон моделей для калибровки:**

    cd ~/hh-ai-agent
    set -a; source .env; set +a
    python3 scripts/evaluate_models.py --calibration --provider aitunnel \
      --models deepseek-v3.2,qwen3.7-flash --max-tokens 16000 --delay 3

**Сравнение с ручной разметкой:**

    python3 scripts/compare_vs_manual.py

**Генерация сопроводов (скрипт):**

    cd ~/hh-ai-agent
    set -a; source .env; set +a
    python3 scripts/generate_cover_letters.py --calibration --show

**Push в форк:** `cd ~/hh-ai-agent && git push`

**Pull из upstream (cherry-pick):** `git fetch upstream && git cherry-pick <commit>`

**Состояние БД:**

    sqlite3 -header -column agent.db "SELECT status, COUNT(*) FROM vacancies GROUP BY status;"
    sqlite3 -header -column agent.db "SELECT model, COUNT(*) FROM evaluations WHERE is_calibration=1 GROUP BY model;"
    sqlite3 agent.db "SELECT COUNT(*) FROM seen_vacancies;"

**Тесты (по одному, не все сразу):**

    python3 -m pytest tests/test_config.py -q --no-header
    python3 -m pytest tests/test_hh_client.py -q --no-header
    python3 -m pytest tests/test_approval.py -q --no-header
    python3 -m pytest tests/ -q --no-header   # полный, baseline 27 fail

---

## ⚠️ ВАЖНЫЕ НЮАНСЫ

- **CloakBrowser free v146, 1 concurrent session.** Не жать две кнопки одновременно.
- **`AUTO_APPLY_ENABLED=false`** — только ручное approval через TG.
- **`ENABLE_COVER_LETTER_GENERATION=false`** — сопроводы пока генерим отдельным скриптом (B2), в pipeline не встроено (B3 — следующий шаг).
- **`MAX_APPLICATIONS_PER_DAY=50`**, но на первый отклик — по одной карточке.
- **AITUNNEL баланс:** 1079.24 ₽ (18.10.2026). DeepSeek v3.2 ~0.52₽/вызов, qwen ~0.14₽/вызов с кэшем.
- **`--max-tokens 16000`** для DeepSeek/Qwen (reasoning-модели).
- **`session_id`** должен быть **одинаковым** для всей серии вызовов (иначе кэш не активируется).
- **Кэш TTL ~10 минут.** Если пауза >10 мин — первый вызов холодный (полная цена).
- **Правило «Песец»** — `mascots/tracker.py`, шлёт картинки песца в TG. К LLM отношения не имеет.
- `.gitignore`: `.env`, `config/`, `agent.db`, `*.bak-*`, `docs/datasets/*.txt`, `docs/datasets/compare.py`, `docs/datasets/export_raw.py`, `docs/datasets/calibration_*.csv`
- Xvfb **1280x800x24** (фактическое).
- **При записи больших файлов** — маленькими блоками `cat >>` по 60-80 строк.
- **В названиях задач — писать полностью**, не «B1», а «генерация сопроводительных писем через AITUNNEL».

---

## 🔗 ССЫЛКИ

- Upstream: https://github.com/fikstt2/hh-ai-agent
- Форк: https://github.com/asverinov/hh-ai-agent
- Ветка: https://github.com/asverinov/hh-ai-agent/tree/my-local-work
- hh.ru резюме РП: https://hh.ru/resume/bc7c9e1aff0fe6b4ae0039ed1f614361526447
- Telegram: @asverinov_bot
- AITUNNEL: https://aitunnel.ru

---

## 📄 РЕЗЮМЕ (для P4 / multi-resume)

4 резюме приложены к диалогу AI5:
- **РП** (Руководитель проектов) — основное, поиск идёт по нему
- РСП (Сервис-менеджер)
- РПО (Руководитель проектного офиса)
- РОП (Руководитель отдела поддержки)

`SuitabilityResult.best_resume` уже выбирает одно из 4 (RP/RSP/RPO/ROP).
Задел: сравнение вакансий со всеми резюме одновременно + search по всем 4.

---

## 📌 БЫСТРЫЙ СТАРТ В НОВОЙ СЕССИИ

1. Скажи «дайджест» — получишь `cat docs/AI5-digest.md`.
2. Проверь, что агент остановлен: `pgrep -af "python3 main.py"`.
3. Проверь сервисы: `ss -tlnp | grep -E ":(8090|5900)"`.
4. Проверь БД: `sqlite3 -header -column agent.db "SELECT status, COUNT(*) FROM vacancies GROUP BY status;"`.
5. Проверь AITUNNEL баланс: `curl` через API.
6. **Следующая задача:** B3 — интеграция генерации сопроводительных писем через AITUNNEL в `main.py`.

**Текущее состояние (18 окт, 17:35):**
- 139 rejected_by_llm, 30 pending, 14 applied, 19 filter, 18 skipped
- 201 evaluations (189 калибровочных)
- 1568 seen_vacancies
- AITUNNEL баланс: 1079.24 ₽
- Тесты: 27 fail, 344 passed (baseline, 0 регрессов)
- 12 коммитов с начала AI6, все в `origin/my-local-work`

---

*Создан в AI6, коммит `f5c2551`. При обновлении — старую версию сохраняем
как `docs/AI5-digest.md.bak-<timestamp>`.*
