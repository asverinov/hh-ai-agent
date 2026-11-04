# ТЕХНИЧЕСКИЙ ДОЛГ ПРОЕКТА (актуально на AI9, 02.11.2026)

Здесь фиксируются архитектурные и технические проблемы, которые известны, но не блокируют текущую работу.

**Не входит в эту итерацию** (AI9): см. секцию «Отложено после стабилизации».

---

## 🟡 Шаблоны контентных файлов (после финализации промптов)

**Проблема:** контентные файлы (`assess*.md`, `ds_*.md`, `stage1_hints.yaml`, `prompts.json`, `calibration_set.json`) выведены из git (AI10, `.gitignore`). Для публичного репо нужны обезличенные шаблоны.

**Что сделать:** на основе рабочих вариантов — `assess1.example.md`, `assess2.example.md`, `ds_cover_letter_digest.example.md`, `stage1_hints.example.yaml`, `prefilter_rules.example.yaml`, `profile.example.yaml`, `resumes.example.yaml`. Без персональных данных, только структура + правила + примеры.

**Когда:** после финализации промптов (сейчас v1.7/v1.8 в работе). До этого — не имеет смысла.

**Статус:** не начато.

---

## 🔴 Высокий приоритет (следующий шаг)

1. **`apply_worker.py`** — не сделан. Берет `pending_approval` с `queue_priority=1`, делает `submit_application` через CloakBrowser (flock + `.browser-apply-pending`). Сейчас apply — вручную через TG-карточку. **Единая очередь с `read_description`** (один браузер): приоритет apply выше read, rate limit 5 мин на уровне БД.
2. **`tg_bot` кнопка `enqueue_apply`** — сейчас бот делает `approve_and_apply` (синхронно). Надо переделать на `enqueue_apply` (ставит `queue_priority=1`, `apply_worker` подхватывает).
3. **Генерация писем для новых pending** — после миграции `migrate_assess.py` надо запустить `cover_letter_worker.py` для всех новых `pending_approval` (v4.7 промпт). 141 старых pending + новые.
4. **246 discovered без description** — после миграции 533 (с description) останутся 246 без. Нужен `read_description` (браузер) + вторым проходом `migrate_assess`. Отдельный шаг.
5. **`reconcile_vacancies.py` — расписание** — сейчас ручной запуск. Нужен cron/periodic (раз в N дней), чтобы БД не копила stale.

---

## 🟡 Средний приоритет (архитектурная чистка)

6. **`main.py` — DEPRECATED, удалить после стабилизации.** С 27.10 не менялся, не запускается. Воркеры — единственный путь. Удаление: после 2-3 итераций без инцидентов. До этого — оставить как fallback, но **не запускать**.
7. **`profile.yaml` — полное выпиливание.** Сейчас используется: `hh.*` (search_worker, read_description), `candidate.excluded_*` (search_worker), `cover_letter.closing/max_length/required_portfolio_url` (cover_letter.py), `resumes.*` (summary + full_text). В LLM **не идёт**. План: вынести в отдельные конфиги (например, `prefilter_rules.yaml` + `resumes.yaml` уже есть), удалить файл.
8. **`excluded_keywords` не проверяется нигде.** Нужен `description`, а `search_worker` работает до чтения. Варианты: (а) в `read_description` — regex после чтения; (б) в `assess_worker` — перед Stage 1; (в) отдельный воркер. Техдолг.
9. **`ASSESS_MODEL` из `.env` игнорируется** (AI9). После порта `assess_worker` на `assess_pipeline` модели берутся из дефолтов `assess_stage1` (`qwen3.7-flash`) / `assess_stage2` (`mimo-v2.6-flash`). Если `.env` переопределял — теперь не работает. Решение: либо явно прокидывать из `settings.llm.assess_model`, либо задокументировать как «фиксированные модели».
10. **Retry в `generate_cover_letter` теряет дайджест.** На первом провале `_safe_letter` заменяет `system_instructions` на короткую инструкцию без дайджеста. Модель справляется, но теряет контекст DS. TODO: добавить дайджест и на retry.
11. **`Unclosed client session`** — aiohttp-сессия не закрывается в `ManagedLLMProvider`/провайдере. Мусорит в логах.
12. **Консистентность модели на пограничных кейсах** (AI9). Одна и та же вакансия (`138488642` QC контакт-центра) в двух прогонах дала `reject` → `accept+OQ`. `138978638/639` (Head of Customer Support) — в первом прогоне противоречие, во втором согласовано. Эксперт: «норма, вариативность v1.5». Отслеживать на 779: если >10% повторных прогонов дают разные решения — система, разбираем.

---

## 🟡 AI9: разделение empty / invalid_json (отложено)

**`LLMInvalidResponseError`** (category=`invalid_response`) покрывает и `empty` (reasoning съел контент, `content=null`), и `invalid_json` (модель вернула невалидный JSON). В `migrate_assess` авто-стоп считает их вместе: `invalid_count > 3%`.

**Почему отложено:** `raw_response` сохраняется при ошибке (`managed.py`), диагностика возможна руками. Если `empty` начнёт расти на полной миграции — разделить в `llm/errors.py`: `LLMEmptyResponseError` (category=`empty_response`) + `LLMTruncatedResponseError` (category=`truncated_response`), поднимать в `managed.py` в нужных местах. Оценка: 1–1.5 ч + тесты.

**Сигнал для возврата:** при авто-стопе `invalid_response` — проверить `raw_response` в `llm_requests`. Если большинство — `""` или reasoning-хвост → это `empty`, делать (B).

---

## 🟢 Низкий приоритет (оптимизация)

13. **`_migrate_status_check` был сломан** (AI9-fix). Хардкодил старую CREATE TABLE (без `queued_at`/`description`/`evaluation_json`), падал при добавлении нового статуса в CHECK. Фикс: берёт реальный `CREATE TABLE` из `sqlite_master`, заменяет только `CHECK (status IN (...))`. **Проверено:** при добавлении `IRRELEVANT` миграция прошла, 4372 записи сохранены.
14. **`Unclosed client session` в `managed.py`** (AI9). `control_run.py` фиксит `provider.close()`, но `ManagedLLMProvider` и провайдеры (`openai_compatible`, `anthropic_compatible`) не имеют `close()`. `assess_worker`/`migrate_assess` могут оставлять aiohttp-сессии. Техдолг.
15. **`stage1_select` без `vacancy_id`** — в `llm_requests` для Stage 1 не пишется `vacancy_id` (исправлено в `assess_pipeline.py`, но старые записи остались).
16. **`migrate_assess` без CLI-флагов.** Нет `--limit`, `--batch`, `--dry-run`. Сейчас — полный проход. Добавить для гибкости (особенно для тестовых прогонов 10-20).
17. **`watch_workers.sh`** — монитор воркеров. Запланирован в `PLAN_workers_v1.md`, не реализован.
18. **`scripts/migrate_assess.py` — stop при пустом `resume_text`** (AI9). Сейчас: `resume_text=""`, если файла нет. `assess_worker` делает `mark_analysis_failed`. Расхождение. Выровнять.
19. **`pyproject`/`requirements`** — не проверял. Может, есть неиспользуемые зависимости после выпиливания `main.py`.
20. **`profile.yaml` — снижение доли.** После AI9: `prefilter` (`hh.*`, `candidate.excluded_*`) читается из `prefilter_rules.yaml`. В `profile.yaml` осталось: `candidate.*` (кроме excluded), `hh.resume_name/search_queries/areas/remote_only`, `cover_letter.*`, `resumes.*`. Следующий шаг — `resumes.yaml`, `cover_letter.yaml`, `discover.yaml`. Не сейчас.

---

## Отложено после стабилизации (не в AI9)

- Удаление `main.py` (пункт 6).
- Вынос `profile.yaml` в отдельные конфиги (пункт 7).
- `excluded_keywords` в `read_description` (пункт 8).
- Дифференциация моделей по этапам — **уже сделано** (AI9: Stage 1 Qwen, Stage 2 MiMo). Не техдолг.

---

## История

- **AI9 (03.11.2026):** обновлён. Убрано: backfill (удалён, `/vacancies/{id}` закрыт), контрольный прогон (сделан), авто-стоп пороги (согласованы), docstring cover_letter_worker (сделан). Добавлено: `reconcile_vacancies.py` расписание (п.5), 246 без description (п.4), консистентность модели (п.12), `_migrate_status_check` fix (п.13), `Unclosed session` в `managed.py` (п.14), `profile.yaml` снижение (п.20).
- **AI9 (02.11.2026):** переписан. Добавлены: backfill (п.4), контрольный прогон (п.5), `main.py` DEPRECATED (п.6), `ASSESS_MODEL` игнорируется (п.9), авто-стоп пороги (п.12), docstring `cover_letter_worker` (п.13), `migrate_assess` stop (п.17), `pyproject` (п.18). Убрано: «модели не дифференцированы» (сделано).
- **AI8 (02.11.2026):** создан. Пункты: `apply_worker`, `enqueue_apply`, генерация писем, `profile.yaml`, `os.environ`, `Unclosed client session`, retry дайджеста, `stage1_select` без `vacancy_id`.
