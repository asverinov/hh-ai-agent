# ДАЙДЖЕСТ СЕССИИ AI6 (26.10.2026)

**Тема:** Каскадная архитектура Stage 0 → 1 → 2 + промпт сопроводов v4.2
**Состояние на:** 27.10.2026, 17:45
**Предыдущий дайджест:** `docs/AI5-digest.md` (от 22.10.2026)

---

## 1. ЧТО СДЕЛАНО

### 1.1. Откат от "сбойной" сессии (24-25.10)
- Откатили код к стабильному `8c4618a` (сессия наворотила: дублирование `best_resume_reason`, дублирование правила 5.1, сломанная Pydantic-модель).
- Сохранили новый промпт v4.1 от эксперта в `docs/datasets/ds_cover_letter_digest.md`.
- Пересоздали `profile.yaml` по версии эксперта + склеили с блоками `candidate/hh/cover_letter` из бэкапа.

### 1.2. Каскадная архитектура (Stage 0 → 1 → 2)
- **Stage 0:** эвристика по `stage0_rules` из `profile.yaml` → 1-3 кода резюме.
- **Stage 1:** LLM (AITUNNEL, `assess_model`) выбирает одно резюме по summaries.
- **Stage 2:** `assess` получает `candidate + full_text[best_resume] + digest_v3`.
- **Cover letter:** `generate_cover_letter` получает `best_resume + resume_text` + промпт v4.1.

### 1.3. Прочие фиксы
- Дайджест оценки переключён на `ds_context_digest_v3.md` (6.4K вместо 34K).
- `select_resume_stage1` переписан: `generate_text` + ручной `json.loads` (вместо `generate_structured(req, dict)`).
- `Stage 1` теперь явно передаёт `model_override=assess_model` (mimo-v2.6-flash).
- Правило Stage 0 "поддержка/мониторинг/24/7" уточнено (убрали ложные срабатывания на "налоговый мониторинг").

---

## 2. РЕЗУЛЬТАТЫ ТЕСТА (Контур.Налоговый мониторинг, 138204825)

| Этап | Результат |
|---|---|
| Stage 0 | `[RPO, RP]` |
| Stage 1 | `RP` (обоснованно) |
| Stage 2 | `suitable=true`, `confidence=0.84` |
| Cover letter (v4.1) | 1004 симв, 3 абзаца, 1С + Банк России (SOAP) |
| **Оценка эксперта** | **9.8/10** (vs 8.0/10 у старого пайплайна) |

**Эксперт подтвердил:** новый каскад правильно определяет домен (финансы/налоги) и выбирает кейсы (1С, Банк России), а не "жирный" ГАС с Oracle/CI/CD.

---

## 3. ТЗ НА v4.2 (от эксперта)

1. **ЭЛАР для PM-ролей:** если вакансия просит сбор требований / бэклог → добавлять ЭЛАР (800+ обращений / 250+ ЗНИ).
2. **Точные цитаты:** использовать формулировки вакансии ("многоуровневая структура заказчика", а не "многоуровневые структуры").
3. **Конкретные цифры:** подхватывать адекватные цифры из вакансии ("6-8 проектов одновременно").
4. **Stage 1 для финансов:** если вакансия про налоги/финансы/1С/банк → приоритет ROP + RPO.

---

## 4. АРХИТЕКТУРА (краткая)

```
hh.ru API → search_by_resume
   ↓
read_vacancy (CloakBrowser)
   ↓
Stage 0: эвристика (profile.yaml.stage0_rules)
   ├─ 1 кандидат → сразу best_resume
   └─ 2-3 кандидата → Stage 1 (LLM, AITUNNEL)
        ↓
Stage 2: assess (candidate + full_text[best_resume] + digest_v3)
   ↓
generate_cover_letter (best_resume + resume_text + v4.1 + qwen3.7-flash)
   ↓
TG-карточка → ручной apply
```

**Модели:**
- `ASSESS_MODEL=mimo-v2.6-flash` (Stage 1 + Stage 2)
- `COVER_LETTER_MODEL=qwen3.7-flash` (письма)
- `LLM_MODEL=GigaChat-2-Pro` - legacy, не используется в каскаде

---

## 5. ФАЙЛЫ

| Файл | Что там |
|---|---|
| `config.py` | `ResumeProfile`, `Stage0Rule`, парсинг `resumes:` / `stage0_rules:` |
| `ai_analyzer.py` | `select_resume_stage0`, `select_resume_stage1`, `assess(..., best_resume, resume_text)`, `generate_cover_letter(..., best_resume, resume_text)` |
| `main.py` | `process_vacancy`: Stage 0 → 1 → 2, дайджест v3 |
| `profile.yaml` | `stage0_rules` (8 правил), `resumes` (4 блока) |
| `data/resumes/resume_*.txt` | 4 полных текста (`.gitignore`) |
| `docs/datasets/ds_context_digest_v3.md` | сжатый дайджест (6.4K) |
| `docs/datasets/ds_cover_letter_digest.md` | v4.1 (32K) |

---

## 5.1. ЧТО СДЕЛАНО 27.10.2026

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

## 6. СЛЕДУЮЩИЕ ШАГИ

1. **Патчи v4.2:** Stage 1 для финансов + жёсткое требование доменного кейса + точные цитаты.
2. **Калибровка на 5 ID** из `cover_letter_analysis_20261023.md`: сравнить v4.1 vs v3.3.1 по 6 критериям.
3. **Переключить Stage 1 в проде на AITUNNEL** (сейчас в `main.py` Stage 1 идёт через `analyzer` = GigaChat, что дорого).
4. **Перегенерировать письма** для 20 `pending_approval` + 162 `discovered`.
5. **Расширить калибровку** с 5 до 20 ID.

---

## 7. КОМАНДЫ

**Запуск агента:**
```bash
cd ~/hh-ai-agent && source venv/bin/activate
unset HH_ACCESS_TOKEN HH_REFRESH_TOKEN HH_TOKEN_EXPIRES_AT
export DISPLAY=:99
python3 main.py
```

**Тест каскада:**
```bash
PYTHONPATH=. python3 scripts/test_stage12_v2.py
PYTHONPATH=. python3 scripts/test_stage3_cover.py
```

**Тест Stage 0 (без LLM):**
```bash
PYTHONPATH=. python3 scripts/test_cascade.py
```

**Откат:**
```bash
git reset --hard 8c4618a
cp profile.yaml.bak-20261026 profile.yaml
```

---

*Создан 26.10.2026. При обновлении - старую версию сохранять как `docs/AI6-digest.md.bak-<timestamp>`.*
