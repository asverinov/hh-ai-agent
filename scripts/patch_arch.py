from pathlib import Path
import shutil, datetime

p = Path("docs/ARCHITECTURE.md")
shutil.copy(p, str(p) + ".bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M"))
c = p.read_text(encoding="utf-8")

section = """## CASCADE (Stage 0 -> 1 -> 2) - с 26.10.2026

Убирает "слепоту" генератора писем: `generate_cover_letter` теперь получает `best_resume` + `full_text` выбранного резюме, а не единый профиль.

**Архитектура:**
1. **Stage 0** - эвристика по `profile.yaml.stage0_rules` -> 1-3 кандидата (RP/RSP/RPO/ROP).
2. **Stage 1** - если >1, LLM (AITUNNEL, `assess_model`) выбирает одно резюме по summaries.
3. **Stage 2** - `assess` получает `candidate + resume_text + ds_context_digest_v3.md`.
4. **Cover letter** - `best_resume` + `resume_text` + промпт v4.1 + qwen3.7-flash.

**Ключевые функции:**
- `ai_analyzer.py`: `select_resume_stage0`, `select_resume_stage1`, `assess(best_resume=, resume_text=)`, `generate_cover_letter(best_resume=, resume_text=)`
- `config.py`: `ResumeProfile`, `Stage0Rule`, парсинг `resumes:` / `stage0_rules:`
- `main.py`: `process_vacancy` - Stage 0 -> 1 -> 2

**Тест (Контур.Налоговый мониторинг 138204825):**
- Stage 0: `[RPO, RP]`
- Stage 1: `RP`
- Stage 2: `suitable=true, confidence=0.84`
- Cover letter: 1004 симв, 3 абзаца, 1С + Банк России
- Эксперт: **9.8/10** (vs 8.0/10 у старого пайплайна)

**ТЗ на v4.2 (от эксперта):**
1. ЭЛАР для PM-ролей (сбор требований, бэклог).
2. Точные цитаты из вакансии.
3. Конкретные цифры вакансии (6-8 проектов).
4. Stage 1: для финансов/налогов - приоритет ROP.

**Откат:** `git reset --hard 8c4618a` + `cp profile.yaml.bak-20261026 profile.yaml`

---

"""

anchor = "## FILES (high-level)"
if anchor in c and "## CASCADE" not in c:
    c = c.replace(anchor, section + anchor, 1)
    p.write_text(c, encoding="utf-8")
    print("OK: ARCHITECTURE.md обновлён")
else:
    print("SKIP: anchor не найден или раздел уже есть")
