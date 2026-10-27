import shutil, subprocess
from pathlib import Path
from datetime import datetime

p = Path("docs/ARCHITECTURE.md")
shutil.copy(p, str(p) + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M"))
c = p.read_text(encoding="utf-8")

# 1. Обновить раздел CASCADE — добавить LLM config и scripts
if "### LLM CONFIG (актуально на 27.10.2026)" not in c:
    new_section = '''### LLM CONFIG (актуально на 27.10.2026)

| Параметр | Значение | Зачем |
|---|---|---|
| `ASSESS_MODEL` | `mimo-v2.6-flash` | Stage 1 + Stage 2 (reasoning-модель) |
| `COVER_LETTER_MODEL` | `qwen3.7-flash` | Письма |
| `LLM_MAX_OUTPUT_TOKENS` | `16000` | mimo уходит в reasoning, 6000 не хватало |
| `LLM_TIMEOUT_SECONDS` | `180` | Оценка + письмо, ~3 мин на сложные вакансии |
| `max_output_tokens` (cover letter) | `10000` | qwen обрезался на 6000 → letter=0 |
| `session_id` | `context-main` / `cover-letter-main` | Кэш AITUNNEL |

**Диагностика `managed.py`:** при `empty/truncated` логируется `finish_reason` + `text_len`, `raw_response` сохраняется при ошибке.

**mimo-v2.6-flash — reasoning-модель.** При `max_tokens=6000` reasoning съедал всё, `content=null` → `invalid_response`. При 16000 — хватает.

### SCRIPTS (актуально на 27.10.2026)

| Скрипт | Что делает |
|---|---|
| `fetch_descr_and_regen.py` | Дотягивает description для pending без него + каскад. Cleanup Chromium перед стартом. `VACANCY_REMOVED/HIDDEN/ACCESS_DENIED` → `expired`. |
| `regen_letter_only.py` | Догенерация `cover_letter` для pending с `description!=''` и `cover_letter=''`. |
| `regenerate_pending.py` | Полный прогон каскада для pending с description. |
| `apply_cascade_patch.py` | Патч Stage 0→1→2 (историческое). |
| `test_cascade.py`, `test_stage12_v2.py`, `test_stage3_cover.py` | Тесты каскада. |

### RATE-LIMIT (актуально на 27.10.2026)

- `approval.py`: `asyncio.Lock` + `_last_apply_at` в памяти процесса, cooldown 300 сек.
- **Ограничение:** не переживает рестарт. **TODO:** заменить на `apply_worker.py` — очередь в БД + отдельный процесс.

### CLASSIFY_PAGE (актуально на 27.10.2026)

- `checks` использует `count() > 0` вместо `is_visible()` (последний бросал на невидимых).
- Fallback через `page.locator("body").inner_text()` + `re.search` (игнорирует `\\xa0`, разбивку по узлам).
- Маркеры архива: `Вакансия в архиве`, `в архиве с N`, `не принимает отклики`, `Работодатель, вероятно, уже нашел`.

'''

    # Вставить перед "### Откат"
    anchor = "### Откат"
    if anchor in c:
        c = c.replace(anchor, new_section + anchor, 1)
        print("OK: ARCHITECTURE.md обновлён (LLM config, scripts, rate-limit, classify_page)")
    else:
        # Или перед "## FILES (high-level)"
        anchor2 = "## FILES (high-level)"
        if anchor2 in c:
            c = c.replace(anchor2, new_section + anchor2, 1)
            print("OK: ARCHITECTURE.md обновлён (вставлено перед FILES)")
        else:
            print("WARN: anchor не найден")
            raise SystemExit(1)

p.write_text(c, encoding="utf-8")

r = subprocess.run(["python3", "-m", "py_compile", "docs/ARCHITECTURE.md"], capture_output=True)
# ARCHITECTURE.md — не python, py_compile не подойдёт, просто пропускаем
print("OK: ARCHITECTURE.md записан")
