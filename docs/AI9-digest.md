# ДАЙДЖЕСТ СЕССИИ AI9 (03.11.2026)

**Тема:** Фикс prefilter (experience, junior, РКО), порт assess_worker на assess_pipeline, reconcile через similar_vacancies, фикс багов validate_stage2_result, запуск миграции 533.

**Состояние на:** 03.11.2026, 23:30
**Предыдущий:** `docs/AI8-digest.md`
**Техдолг:** `docs/TECH_DEBT.md`
**Правила общения:** `docs/INTERACTION.md`

---

## 1. ЧТО СДЕЛАНО

### 1.1. Pre-filter (закрыт)
- **`_is_junior_title`:** +`trainee`, `intern` в дефолт `config.py` (были только в мёртвом `stage0_rules.yaml`). Тест 7/7.
- **`experience_filters_allowed`** (мёртвый ключ в `stage0_rules.yaml`) — заменён на `prefilter_rules.yaml` + локальная проверка `item_exp` в `search_worker` до `discover()`. API-фильтр `experience` подтверждён (`test_prefilter.py`: 800 items, 0 leaks).
- **`stage0_rules.yaml` → `stage1_hints.yaml`:** удалён мёртвый `reject:` (77 строк), остался только `hint:` для Stage 1.
- **`prefilter_rules.yaml`** (новый, от Эксперта): 8 секций, `config.py:PrefilterRules`, `search_worker` читает `profile.prefilter.*`.
- **`check_prefilter`** в `assess_worker` (до Stage 1): `excluded_keywords` (substring) → reject; `employment_type_flags` (regex) → флаг, не reject.
- **Фикс коротких аббревиатур:** `РКО` (3 буквы) ловило «банковского», «контролем», «фреймворков», «CROC». Word boundary для `len≤4, isalpha, isupper`. Unit 10/10.

### 1.2. Оценка (закрыт сплит-мозг)
- **`assess_worker` → `assess_pipeline`** (Stage 1 Qwen + Stage 2 MiMo). Был на старом `analyzer.assess()` + `ds_context_digest_v3.md` (26.10), пока миграция — на `assess_pipeline`. Теперь оба на новой.
- **`select_resume_stage0` удалён** (Q1, решение Эксперта). Fallback в `cover_letter_worker`/`regenerate_pending`/`main.py` → RP + warning.
- **`SuitabilityResult`:** +`overqualified: bool`, +`overqualified_reason: str`. `_normalize` не выбрасывает (root_keys дополнен).
- **`validate_stage2_result`:** (а) код-страховка `level<=2 && match<0.5` (было `level=1 && match<0.3`); (б) фикс `verdict-contains` → `startswith` («Подаваться… отказ по OQ» больше не reject).

### 1.3. Reconcile (вместо backfill)
- **`GET /vacancies/{id}` ЗАКРЫТ для физлиц с апреля 2026.** `backfill_experience.py` удалён.
- **`scripts/reconcile_vacancies.py`** (новый): обход `similar_vacancies` для 4 резюме (68 страниц, 2742 ID, ~2.5 мин, 0×429), двухфазный (collect → apply).
- **Статус `irrelevant`** (новый): «была в БД, больше не в similar_vacancies». `llm_reason=not_in_similar_<дата>`. Не путать с `expired`.
- **Результат:** 779 discovered → 779 backfill (experience_id), 323 irrelevant. `reject_exp=0`, `errors=0`.

### 1.4. Миграция + авто-стоп
- **`migrate_assess.py`:** только `discovered` с `description` (533), **не трогает** `pending_approval` (141). Сохраняет `experience_id` от reconcile (не сбрасывает `evaluation_json=''`).
- **Авто-стоп упрощён:** только жёсткие факапы (`invalid>3%`, `infra>5%`, `timeslow>2×`, `mismatch>3%`). Статистика — в `check_migration_stats.py`.
- **`_migrate_status_check` был сломан** (хардкод старой CREATE TABLE, падал при `IRRELEVANT`). Фикс: реальный `CREATE TABLE` из `sqlite_master` + замена только CHECK.

### 1.5. Control run (30 вакансий, 2 прогона)
| Метрика | v1 (до фиксов) | v2 (после) |
|---|---|---|
| Prefilter reject | 4 | 1 |
| Stage 1 reject | 7 | 9 |
| Stage 2 reached | 19 | 20 |
| suitable=true | 8 | 10 |
| hard_filter / verdict | 11/0 | 10/0 |
| leaks / errors | 0/0 | 0/0 |

Цифры 5–20% / ≥85% — **гипотеза Пользователя**, не KPI. Ориентир — по большой выборке.

### 1.6. Документация
- `docs/INTERACTION.md` (новый) — правила общения с Пользователем (printf, бекапы, «стоп всё», контентные файлы).
- `docs/ARCHITECTURE.md` — reconcile, `irrelevant`, API-факты (`/vacancies/{id}` закрыт, `similar_vacancies` без rate limit), `migrate_assess` без pending.
- `docs/TECH_DEBT.md` — консистентность модели, `_migrate_status_check` fix, Unclosed session в `managed.py`, 246 без description, `profile.yaml` снижение.

---

## 2. ТЕКУЩЕЕ СОСТОЯНИЕ (на 23:30)

**Миграция 533 discovered запущена** (foreground, ~6 ч, PID в терминале Пользователя). В `reports/migration_results.jsonl` — ~35 записей и растёт. Авто-стоп каждые 20.

**После миграции:**
- `pending_approval`: 141 старых + новые (suitable=true) — ждут `cover_letter_worker`.
- 246 discovered без description — ждут `read_description` + второй проход миграции.
- `irrelevant`: 323 (не идут в миграцию).

---

## 3. СЛЕДУЮЩИЕ ШАГИ (после миграции 533)

1. **`check_migration_stats.py`** — сводка (% true, hard_filter%, is_required%, best_resume%).
2. **`audit_migration.py`** — сэмпл для Эксперта (20 true + 10 false).
3. **Отчёт Эксперту** — цифры 533. Если >35% true — ужесточаем `assess1` (Stage 1).
4. **`cover_letter_worker`** — письма для pending (v4.7, 141 + новые).
5. **246 без description** — `read_description` + второй проход.
6. **Эксперт: `ds_cover_letter_digest.md v4.8`** — после 533.
7. **`apply_worker.py`** — единая очередь с `read_description`, приоритет apply, rate limit 5 мин.

---

## 4. КОМАНДЫ (для следующей сессии)

**Миграция (если не запущена):**
    cd ~/hh-ai-agent && source venv/bin/activate
    cp agent.db bak/agent.db.bak-pre-migration-$(date +%Y%m%d_%H%M%S)
    PYTHONPATH=. python3 -u scripts/migrate_assess.py

**Reconcile (раз в N дней):**
    PYTHONPATH=. python3 -u scripts/reconcile_vacancies.py --skip-collect
    # или полный с API:
    PYTHONPATH=. python3 -u scripts/reconcile_vacancies.py

**Control run (10-30 для проверки):**
    PYTHONPATH=. python3 -u scripts/control_run.py --limit 30 --dates 2026-11-02,2026-10-30

**Test prefilter (1 запрос, без БД):**
    PYTHONPATH=. python3 scripts/test_prefilter.py --all --pages 1

**Мониторинг миграции:**
    PYTHONPATH=. python3 scripts/check_migration_stats.py
    tail -f reports/migration_results.jsonl | wc -l

---

## 5. ЧТО НЕ ДЕЛАТЬ

- **Не пытаться `GET /vacancies/{id}`** — закрыт с апреля 2026. Только `similar_vacancies` или браузер.
- **Не запускать `migrate_assess` на pending** — user их видит в TG.
- **Не добавлять `excluded_keywords` в `search_worker`** — там нет `description`, только в `assess_worker` до Stage 1.
- **Не менять `assess1`/`assess2`** — зона Эксперта. Главы — через Пользователя.
- **Не запускать фоновые процессы** без «поехали» от Пользователя (после «стоп всё»).
- **Не коммитить `prefilter_rules.yaml` / `profile.yaml` / `resume_*.txt`** — в `.gitignore`.

---

*Создан 03.11.2026. При обновлении — старую версию в `docs/bak/AI9-digest.md.bak-<timestamp>`.*
