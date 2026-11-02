import asyncio
import json
import yaml
from pathlib import Path
from config import load_settings
from database import Database
from ai_analyzer import VacancyAnalyzer
from assess_pipeline import assess_stage1, assess_stage2, extract_hints
from llm.providers.openai_compatible import OpenAICompatibleProvider
from llm.managed import ManagedLLMProvider

async def main():
    settings = load_settings()
    db = Database(str(settings.database_path))
    
    # Берем вакансии, которые ждут оценки, ИЛИ pending, которых надо переоценить
    with db._connect() as c:
        rows = c.execute("""
            SELECT id, title, description FROM vacancies 
            WHERE (status='discovered' AND description!='') 
               OR status='pending_approval'
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
        c.execute(f"UPDATE vacancies SET status='discovered', evaluation_json='', llm_decision=NULL, llm_reason='' WHERE id IN ({ph})", ids)
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

    for idx, row in enumerate(rows, 1):
        vid = row["id"]
        title = row["title"]
        desc = row["description"]
        
        print(f"[{idx}/{len(rows)}] {vid} - {title[:40]}...")
        hints = extract_hints(title, desc)
        
        try:
            # Stage 1
            res1 = await assess_stage1(analyzer=analyzer, title=title, description=desc, summaries=summaries, hints=hints, vacancy_id=vid)
            if not res1.suitable:
                with db._connect() as c:
                    c.execute("UPDATE vacancies SET status='rejected_by_llm', llm_decision=0, llm_reason=? WHERE id=?", (f"Stage 1: {res1.reason}", vid))
                    c.commit()
                print(f"  Stage 1 REJECT: {res1.reason[:60]}...")
                continue

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
                "hard_filter_triggered": res2.hard_filter_triggered, "hard_filter_reason": res2.hard_filter_reason
            }
            
            status = 'pending_approval' if res2.suitable else 'rejected_by_llm'
            llm_decision = 1 if res2.suitable else 0

            with db._connect() as c:
                c.execute("UPDATE vacancies SET status=?, llm_decision=?, llm_reason=?, confidence=?, evaluation_json=? WHERE id=?", 
                          (status, llm_decision, res2.verdict[:300], res2.confidence, json.dumps(eval_json, ensure_ascii=False), vid))
                c.commit()

            with out_file.open("a", encoding="utf-8") as f:
                record = {"id": vid, "title": title, "url": f"https://hh.ru/vacancy/{vid}", "stage1": res1.model_dump(), "stage2": res2.model_dump()}
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        except Exception as e:
            print(f"  ❌ Ошибка: {e}")
            # При ошибке (например, таймауте) возвращаем в discovered, чтобы не потерять
            with db._connect() as c:
                c.execute("UPDATE vacancies SET status='discovered' WHERE id=?", (vid,))
                c.commit()

    print(f"\n=== Миграция завершена. Результаты в {out_file} ===")

if __name__ == "__main__":
    asyncio.run(main())
