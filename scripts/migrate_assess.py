import asyncio
import json
import time
import yaml
from pathlib import Path
from config import load_settings
from database import Database
from ai_analyzer import VacancyAnalyzer, AnalysisError
from assess_pipeline import assess_stage1, assess_stage2, extract_hints
from llm.providers.openai_compatible import OpenAICompatibleProvider
from llm.managed import ManagedLLMProvider

# --- АВТО-СТОП: только жёсткие факапы. Статистика (suitable%, hard_filter%, etc) ---
# --- → check_migration_stats.py, не стоп.                                              ---
STOP_CHECK_EVERY = 20              # проверять каждые N вакансий
STOP_ERROR_MIN = 20                # не проверять % ошибок, пока не обработано N
STOP_AFTER_MIN = 50                # mismatch проверять только после N
STOP_ERROR_PCT_MAX = 5.0           # API/LLM infra errors (timeout/429/5xx) > этого -> стоп
STOP_INVALID_PCT_MAX = 3.0         # invalid_response (empty+invalid_json) > этого -> стоп
STOP_MISMATCH_PCT_MAX = 3.0        # suitable=true + 'не подаваться' > этого (после 50) -> стоп
STOP_TIMESLOW_FACTOR = 2.0         # avg_last_20 > N × avg_first_20 -> стоп
STOP_BASELINE_WARMUP = 40          # первые N — прогрев, потом baseline


def _quick_stop_check(db) -> str | None:
    """Только mismatch: suitable=true + 'не подаваться' > 3% после 50.

    Остальное (suitable%, hard_filter%, is_required%, best_resume%) — статистика
    в check_migration_stats.py, не стоп.
    """
    with db._connect() as c:
        rows = c.execute("""
            SELECT status, llm_decision, llm_reason, evaluation_json
            FROM vacancies
            WHERE status IN ('pending_approval', 'rejected_by_llm')
              AND evaluation_json LIKE '%hard_filter_triggered%'
        """).fetchall()
    total = len(rows)
    if total < STOP_AFTER_MIN:
        return None
    mismatch = 0
    for r in rows:
        ej = json.loads(r['evaluation_json']) if r['evaluation_json'] else {}
        if not ej.get('requirements'):
            continue
        if ej.get('suitable') and 'не подаваться' in (ej.get('verdict') or '').lower():
            mismatch += 1
    pct_mismatch = mismatch / total * 100
    if pct_mismatch > STOP_MISMATCH_PCT_MAX:
        return f"mismatch: suitable=true + 'не подаваться' {mismatch}/{total} ({pct_mismatch:.1f}%) > {STOP_MISMATCH_PCT_MAX}%"
    return None


async def main():
    settings = load_settings()
    db = Database(str(settings.database_path))
    
    # Берем вакансии, которые ждут оценки, ИЛИ pending, которых надо переоценить
    with db._connect() as c:
        rows = c.execute("""
            SELECT id, title, description FROM vacancies 
            WHERE (status='discovered' AND description!='')
            ORDER BY id
        """).fetchall()
    
    if not rows:
        print("Нет вакансий для миграции.")
        return

    print(f"Найдено {len(rows)} вакансий для миграции (discovered + pending).")
    
    # Сбрасываем им статус и старую оценку, чтобы они корректно перезаписались
    with db._connect() as c:
        ids = [r["id"] for r in rows]
        ph = ','.join('?' * len(ids))
        c.execute(f"UPDATE vacancies SET llm_decision=NULL, llm_reason='' WHERE id IN ({ph})", ids)
        c.commit()

    api_key = (settings.llm.aitunnel_api_key or "").strip()
    base_url = (settings.llm.aitunnel_base_url or "https://api.aitunnel.ru/v1").strip()
    adapter = OpenAICompatibleProvider(base_url, api_key, json_mode=False, reasoning_enabled=False)
    provider = ManagedLLMProvider(adapter, db, max_retries=2, max_requests_per_day=5000)
    analyzer = VacancyAnalyzer(settings, provider)

    data = yaml.safe_load(Path("profile.yaml").read_text())
    resumes = data.get("resumes", {})
    summaries = {k: v.get("summary", "") for k, v in resumes.items()}

    out_file = Path("reports/migration_results.jsonl")
    out_file.write_text("", encoding="utf-8")

    processed = 0
    error_count = 0        # все ошибки (для совместимости)
    invalid_count = 0      # AnalysisError: invalid_response
    infra_count = 0        # AnalysisError: timeout/rate_limit/5xx/etc + Exception
    times: list[float] = []  # длительность обработки, для rolling baseline
    stopped_reason = None

    for idx, row in enumerate(rows, 1):
        vid = row["id"]
        title = row["title"]
        desc = row["description"]

        print(f"[{idx}/{len(rows)}] {vid} - {title[:40]}...")
        hints = extract_hints(title, desc)
        _t0 = time.time()

        try:
            # Stage 1
            res1 = await assess_stage1(analyzer=analyzer, title=title, description=desc, summaries=summaries, hints=hints, vacancy_id=vid)
            if not res1.suitable:
                s1_eval = {"stage": 1, "verdict": res1.reason, "best_resume": None,
                           "confidence": res1.confidence or 0.0}
                with db._connect() as _c:
                    _row = _c.execute(
                        "SELECT evaluation_json FROM vacancies WHERE id=?", (vid,)
                    ).fetchone()
                if _row and _row["evaluation_json"]:
                    try:
                        _prev = json.loads(_row["evaluation_json"])
                        for _k in ("experience_id", "reconcile", "reconciled_at", "prefilter_pass"):
                            if _k in _prev:
                                s1_eval[_k] = _prev[_k]
                    except (json.JSONDecodeError, TypeError):
                        pass
                with db._connect() as c:
                    c.execute(
                        "UPDATE vacancies SET status='rejected_by_llm', llm_decision=0, "
                        "llm_reason=?, evaluation_json=? WHERE id=?",
                        (f"Stage 1: {res1.reason}",
                         json.dumps(s1_eval, ensure_ascii=False), vid),
                    )
                    c.commit()
                print(f"  Stage 1 REJECT: {res1.reason[:60]}...")
            else:
                # Stage 2
                best_code = res1.best_resume or "RP"
                resume_path = Path(f"data/resumes/resume_{best_code}.txt")
                resume_text = resume_path.read_text(encoding="utf-8") if resume_path.exists() else ""
                alternatives = {k: {"title": v.get("title", ""), "summary_short": v.get("summary", "")[:200]} for k, v in resumes.items() if k != best_code}

                res2 = await assess_stage2(analyzer=analyzer, title=title, description=desc, best_resume=best_code, resume_text=resume_text, alternatives=alternatives, vacancy_id=vid)

                hf_flag = "🔥 HARD REJECT" if res2.hard_filter_triggered else ""
                print(f"  Stage 2: suitable={res2.suitable} {hf_flag}")

                eval_json = {
                    "verdict": res2.verdict, "requirements": [r.model_dump() for r in res2.requirements],
                    "best_resume": res2.best_resume, "confidence": res2.confidence,
                    "hard_filter_triggered": res2.hard_filter_triggered, "hard_filter_reason": res2.hard_filter_reason,
                    "overqualified": getattr(res2, "overqualified", False),
                    "overqualified_reason": getattr(res2, "overqualified_reason", ""),
                }
                # AI9: сохранить experience_id от reconcile (не теряем backfill)
                with db._connect() as _c:
                    _row = _c.execute(
                        "SELECT evaluation_json FROM vacancies WHERE id=?", (vid,)
                    ).fetchone()
                if _row and _row["evaluation_json"]:
                    try:
                        _prev = json.loads(_row["evaluation_json"])
                        for _k in ("experience_id", "reconcile", "reconciled_at", "prefilter_pass"):
                            if _k in _prev:
                                eval_json[_k] = _prev[_k]
                    except (json.JSONDecodeError, TypeError):
                        pass

                status = 'pending_approval' if res2.suitable else 'rejected_by_llm'
                llm_decision = 1 if res2.suitable else 0

                with db._connect() as c:
                    c.execute("UPDATE vacancies SET status=?, llm_decision=?, llm_reason=?, confidence=?, evaluation_json=? WHERE id=?",
                              (status, llm_decision, res2.verdict[:300], res2.confidence, json.dumps(eval_json, ensure_ascii=False), vid))
                    c.commit()

                with out_file.open("a", encoding="utf-8") as f:
                    record = {"id": vid, "title": title, "url": f"https://hh.ru/vacancy/{vid}", "stage1": res1.model_dump(), "stage2": res2.model_dump()}
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")

        except AnalysisError as e:
            error_count += 1
            if e.error_type == "invalid_response":
                invalid_count += 1
            else:
                infra_count += 1
            print(f"  ❌ LLM: {e.error_type}")
            with db._connect() as c:
                c.execute("UPDATE vacancies SET status='discovered' WHERE id=?", (vid,))
                c.commit()
        except Exception as e:
            error_count += 1
            infra_count += 1
            print(f"  ❌ Ошибка: {type(e).__name__}: {e}")
            with db._connect() as c:
                c.execute("UPDATE vacancies SET status='discovered' WHERE id=?", (vid,))
                c.commit()

        times.append(time.time() - _t0)
        processed += 1
        if processed % STOP_CHECK_EVERY == 0:
            # 1. Infra errors (timeout/429/5xx/Exception)
            if processed >= STOP_ERROR_MIN and infra_count / processed * 100 > STOP_ERROR_PCT_MAX:
                stopped_reason = f"infra errors {infra_count}/{processed} ({infra_count/processed*100:.1f}%) > {STOP_ERROR_PCT_MAX}%"
                break
            # 2. invalid_response (empty + invalid_json)
            if processed >= STOP_ERROR_MIN and invalid_count / processed * 100 > STOP_INVALID_PCT_MAX:
                stopped_reason = f"invalid_response {invalid_count}/{processed} ({invalid_count/processed*100:.1f}%) > {STOP_INVALID_PCT_MAX}%"
                break
            # 3. Rolling baseline: avg_last_20 > 2× avg_first_20 (после 40)
            if processed >= STOP_BASELINE_WARMUP:
                first_20 = sum(times[:20]) / 20
                last_20 = sum(times[-20:]) / 20
                if first_20 > 0 and last_20 > STOP_TIMESLOW_FACTOR * first_20:
                    stopped_reason = (
                        f"timeslow: avg_last_20={last_20:.1f}s > "
                        f"{STOP_TIMESLOW_FACTOR:.0f}×avg_first_20={first_20:.1f}s"
                    )
                    break
            # 4. mismatch (suitable=true + 'не подаваться' > 3%)
            reason = _quick_stop_check(db)
            if reason:
                stopped_reason = reason
                break

    if stopped_reason:
        print(f"\n🛑 АВТО-СТОП на {processed}/{len(rows)}: {stopped_reason}")
        print(f"   Проверь /tmp/migrate_assess.log и реши: чинить или откатывать.")
    else:
        print(f"\n=== Миграция завершена. Результаты в {out_file} ===")


if __name__ == "__main__":
    asyncio.run(main())
