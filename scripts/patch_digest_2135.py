import shutil
from pathlib import Path
from datetime import datetime

p = Path("docs/AI6-digest.md")
shutil.copy(p, str(p) + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M"))
c = p.read_text(encoding="utf-8")

# 1. Обновить timestamp
import re
c = re.sub(
    r"\*\*Состояние на:\*\* 27\.10\.2026, \d{2}:\d{2}",
    "**Состояние на:** 27.10.2026, 21:35",
    c, count=1,
)

# 2. Добавить раздел "Что сделано вечером" перед "## 6. СЛЕДУЮЩИЕ ШАГИ"
new_section = '''## 5.2. ЧТО СДЕЛАНО ВЕЧЕРОМ 27.10.2026 (19:00–21:35)

| Коммит | Что |
|---|---|
| `7d83b91` | feat(search): `search_worker` — API `similar_vacancies` для 4 резюме, `per_page=100`, дедуп, `HHClient.close()` в `finally` |
| `b8c83e7` | feat(db): миграция `queued_at` + `queue_priority` (2 индекса, `integrity_check=ok`) |

**Состояние на 21:35:**
- `discovered: 1708` (было 78, `search_worker` добавил 1630)
- `pending_approval: 28`
- `rejected_by_llm: 237`
- `search_worker` крутится в фоне (PID 15169), полный цикл ~28 мин, API-нагрузка ~2.8/мин (в 600 раз меньше лимита 30/сек)

**Написано:** `docs/PLAN_workers_v1.md` — детальный план 5 модулей + flock + методы `database.py`.

'''

anchor = "## 6. СЛЕДУЮЩИЕ ШАГИ"
if anchor in c and "## 5.2. ЧТО СДЕЛАНО ВЕЧЕРОМ" not in c:
    c = c.replace(anchor, new_section + anchor, 1)
    print("OK: раздел 5.2 добавлен")

# 3. Обновить "Следующие шаги" — ссылка на план
old = "1. **`apply_worker.py`** — отдельный сервис очереди откликов."
new = "1. **`docs/PLAN_workers_v1.md`** — детальный план 5 модулей. Начинать с `read_description.py` (см. план, раздел 4.1).\n2. **`apply_worker.py`** — отдельный сервис очереди откликов."
if old in c:
    c = c.replace(old, new, 1)
    print("OK: раздел 6 обновлён (ссылка на PLAN)")

p.write_text(c, encoding="utf-8")
print("OK: AI6-digest.md обновлён")
