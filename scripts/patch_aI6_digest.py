import shutil
from pathlib import Path
from datetime import datetime

p = Path("docs/AI6-digest.md")
shutil.copy(p, str(p) + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M"))
c = p.read_text(encoding="utf-8")

# 1. Добавить timestamp после заголовка
if "**Состояние на" not in c:
    c = c.replace(
        "**Тема:** Каскадная архитектура Stage 0 → 1 → 2 + промпт сопроводов v4.1",
        "**Тема:** Каскадная архитектура Stage 0 → 1 → 2 + промпт сопроводов v4.2\n**Состояние на:** 27.10.2026, 17:45",
        1,
    )

# 2. Добавить раздел "Что сделано сегодня" перед "Следующие шаги"
new_section = '''## 5.1. ЧТО СДЕЛАНО 27.10.2026

| Коммит | Что |
|---|---|
| `814987c` | fix(llm): `max_output_tokens=10000` для cover letter + диагностика `managed.py` (`finish_reason`, `raw_response` при ошибке) |
| `8b54f37` | fix(hh): `classify_page` ловит архивные через `inner_text + re.search`, `count()>0` вместо `is_visible()` |
| `a268385` | feat(regen): `regen_letter_only.py` (догенерация писем без assess) |
| `95ebe8a` | feat(approval): rate-limit 300 сек + skip letter для reject |
| `ec59c59` | feat(cascade): Stage 0→1→2 архитектура + v4.1 промпт |
| `6f7a7ed` | docs: v4.2 промпт + обновления ARCHITECTURE |

**Конфиги:**
- `LLM_MAX_OUTPUT_TOKENS=16000` (mimo reasoning)
- `LLM_TIMEOUT_SECONDS=180`
- `max_output_tokens=10000` (cover letter, qwen)
- Английские ключи в Stage 0: `project manager`, `delivery manager`, `program manager`, `engineering manager`

**Данные (27.10.2026, 17:45):**
- `pending_approval: 28` (все с `description` и `cover_letter`)
- `rejected_by_llm: 237`
- `expired: 6`
- `discovered: 5`
- `best_resume` distribution (по 28 pending): RP ~20, ROP ~4, RSP ~3, RPO ~1

'''

if "## 5.1. ЧТО СДЕЛАНО 27.10.2026" not in c:
    anchor = "## 6. СЛЕДУЮЩИЕ ШАГИ"
    if anchor in c:
        c = c.replace(anchor, new_section + anchor, 1)
        print("OK: раздел 5.1 добавлен")

# 3. Обновить "Следующие шаги"
old_steps = '''## 6. СЛЕДУЮЩИЕ ШАГИ

1. **Патчи v4.2:** Stage 1 для финансов + жёсткое требование доменного кейса + точные цитаты.
2. **Калибровка на 5 ID** из `cover_letter_analysis_20261023.md`: сравнить v4.1 vs v3.3.1 по 6 критериям.
3. **Переключить Stage 1 в проде на AITUNNEL** (сейчас в `main.py` Stage 1 идёт через `analyzer` = GigaChat, что дорого).
4. **Перегенерировать письма** для 20 `pending_approval` + 162 `discovered` (сброс оценок → прогон заново).
5. **Расширить калибровку** с 5 до 20 ID.'''

new_steps = '''## 6. СЛЕДУЮЩИЕ ШАГИ (на 28.10.2026)

1. **`apply_worker.py`** — отдельный сервис очереди откликов. In-memory rate-limit в `approval.py` не переживает рестарт. Схема: таблица `apply_queue` или поле `queued_at` в `vacancies`, новый статус `queued_for_apply`, worker с loop 300 сек.
2. **Калибровка 19 вакансий на MIMO** — F1 каскада vs старого пайплайна. Ответить: `mimo` primary или `deepseek` primary?
3. **Spot-check писем** — 3–5 из 28 pending вручную (overclaiming, domain).
4. **Разбор reject-группы** — вчера было много `conf=0.7-0.9, suitable=false`. Проверить, не слишком ли строг `assess` к `level=1`.
5. **`classify_page` edge cases** — если снова `network_error` на live-вакансии, добавить retry.
6. **Переключить Stage 1 в проде на AITUNNEL** — сейчас в `main.py` Stage 1 через `analyzer` (GigaChat).'''

if old_steps in c:
    c = c.replace(old_steps, new_steps)
    print("OK: раздел 6 обновлён")

p.write_text(c, encoding="utf-8")
print("OK: AI6-digest.md обновлён")
