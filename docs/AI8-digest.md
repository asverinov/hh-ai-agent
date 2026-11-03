# ДАЙДЖЕСТ СЕССИИ AI8 (01–02.11.2026)
**Тема:** Переход на новую 2-этапную архитектуру оценки (Stage 1 + Stage 2), исправление багов воркеров, миграция базы.

**Состояние на:** 02.11.2026, 17:30
**Предыдущий дайджест:** `docs/AI7-digest.md`
**Технический долг:** `docs/TECH_DEBT.md`

# 1. ЧТО СДЕЛАНО

## 1.1. Новая архитектура оценки (v1.4)
- **Проблема:** `profile.yaml` противоречил промптам, `assess` слепо выбирал `best_resume` без полного текста резюме.
- **Решение:** `profile.yaml` полностью выпилен из LLM. 
  - **Stage 1 (`assess1.md`):** Qwen3.7-flash. Скрининг по 4 summary. `reasoning=False`.
  - **Stage 2 (`assess2.md` v1.4):** MiMo. Оценка по полному `resume_text`. Добавлены триггеры ядра роли (`is_required`).
  - **Post-processing:** Код-страховка в `assess_pipeline.py` (level=1 match<0.3 -> принудительный `is_required=True`, `hard_reject`).
- **Кеширование:** Настроено для AITUNNEL. Qwen кеширует `system` через `cache_control`. MiMo кеширует implicit (экономия ~50-90%).

## 1.2. Воркеры
- **`search_worker`:** Удален legacy `search_page`. Внедрен `fetch_similar_page` с фильтром опыта `experience_filters`. Добавлен pre-filter: отсев junior, низких ЗП, excluded-компаний.
- **`read_description`:** Защита от банов DDoS-Guard (паузы 10-30 сек, батчи по 150-190 вак с отдыхом 10-20 мин). Починен `ensure_login`.

## 1.3. Миграция базы (`migrate_assess.py`)
- Запущена переоценка **1380 вакансий**. Сброшены старые оценки. Работает в фоне (~10-12 часов).
- Написаны скрипты мониторинга: `check_migration_stats.py` и `audit_migration.py`.

## 1.4. Аудит и Эксперт
- Пилот v3 (30 вакансий): A+B+C+D = 23/25 true. E (контрольные false) = 5/5 с `hard_filter_reason="is_required match=..."`.
- `assess2.md` обновлен до v1.4 (добавлены жесткие триггеры `is_required`).

# 2. РЕЗУЛЬТАТЫ (на момент завершения сессии)

## 2.1. Статистика миграции (промежуточная, 78/1380)
- Обработано: 78.
- Ошибки парсинга (invalid JSON): 2 (1.1%) — скрипт откатывает их в `discovered`.
- `suitable=true`: 50.0% (выше ожидаемого, но выборка мала).
- `hard_filter_triggered`: 47.4%.
- Доля `is_required` в фильтрах: 64.9% (код-страховка компенсирует остальное).
- Стоп-сигналы: 🟢 нет.

## 2.2. Фоновые процессы (оставлены работать)
- `migrate_assess.py` (PID 23524)
- `search_worker.py` (PID 23551)
- `read_description.py` (PID 23554)

# 3. АРХИТЕКТУРА (краткая)
```text
hh.ru API (search_worker, фильтр опыта) -> discovered
   ↓
read_description (батчи, анти-бан) -> description
   ↓
assess_stage1 (Qwen, скрининг, cache) -> ScreenResult (reject/pass)
   ↓
assess_stage2 (MiMo, оценка, код-страховка) -> evaluation_json
   ↓
cover_letter_worker (Qwen, v4.7) -> pending_approval
