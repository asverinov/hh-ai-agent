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

PILOT_IDS = [
    '136956781', '138651275', '138662079', '138752481', '138287554',
    '138851849', '138483418', '138626560', '137526115', '138430655',
    '138456410', '138513392', '138528462', '138437589', '138445183',
    '138627596', '138474868', '138678262', '138749115', '138737356',
    '138764209', '138891533', '138398725', '138566167', '138610361'
]

async def main():
    settings = load_settings()
    db = Database(str(settings.database_path))
    
    with db._connect() as c:
        ph = ','.join('?' * len(PILOT_IDS))
        c.execute(f"UPDATE vacancies SET status='discovered', evaluation_json='', llm_decision=NULL, llm_reason='' WHERE id IN ({ph})", PILOT_IDS)
        c.commit()
    
    print(f"Запуск пилота v3 на {len(PILOT_IDS)} вакансиях...")
    
    api_key = (settings.llm.aitunnel_api_key or "").strip()
    base_url = (settings.llm.aitunnel_base_url or "https://api.aitunnel.ru/v1").strip()
    # Отключаем reasoning на уровне провайдера для Stage 1
    adapter = OpenAICompatibleProvider(base_url, api_key, json_mode=False, reasoning_enabled=False)
    provider = ManagedLLMProvider(adapter, db, max_retries=2, max_requests_per_day=2000)
    analyzer = VacancyAnalyzer(settings, provider)

    data = yaml.safe_load(Path("profile.yaml").read_text())
    resumes = data.get("resumes", {})
    summaries = {k: v.get("summary", "") for k, v in resumes.items()}

    out_file = Path("reports/pilot_v3_results.jsonl")
    out_file.write_text("", encoding="utf-8")

    for idx, vid in enumerate(PILOT_IDS, 1):
        with db._connect() as c:
            row = c.execute("SELECT title, description FROM vacancies WHERE id=?", (vid,)).fetchone()
        if not row: continue
            
        title, desc = row["title"], row["description"]
        print(f"\n[{idx}/{len(PILOT_IDS)}] {vid} - {title[:50]}...")

        hints = extract_hints(title, desc)
        
        try:
            res1 = await assess_stage1(analyzer=analyzer, title=title, description=desc, summaries=summaries, hints=hints, vacancy_id=vid)
            print(f"  Stage 1: suitable={res1.suitable}, best={res1.best_resume}, conf={res1.confidence}")
            if not res1.suitable:
                with db._connect() as c:
                    c.execute("UPDATE vacancies SET status='rejected_by_llm', llm_decision=0, llm_reason=? WHERE id=?", (f"Stage 1: {res1.reason}", vid))
                    c.commit()
                continue
        except Exception as e:
            print(f"  Ошибка Stage 1: {e}")
            continue

        best_code = res1.best_resume or "RP"
        resume_path = Path(f"data/resumes/resume_{best_code}.txt")
        resume_text = resume_path.read_text(encoding="utf-8") if resume_path.exists() else ""
        alternatives = {k: {"title": v.get("title", ""), "summary_short": v.get("summary", "")[:200]} for k, v in resumes.items() if k != best_code}

        try:
            res2 = await assess_stage2(analyzer=analyzer, title=title, description=desc, best_resume=best_code, resume_text=resume_text, alternatives=alternatives, vacancy_id=vid)
            
            hf_flag = "🔥 HARD REJECT" if res2.hard_filter_triggered else ""
            print(f"  Stage 2: suitable={res2.suitable}, best={res2.best_resume}, conf={res2.confidence} {hf_flag}")
            
            eval_json = {
                "verdict": res2.verdict,
                "requirements": [r.model_dump() for r in res2.requirements],
                "best_resume": res2.best_resume,
                "confidence": res2.confidence,
                "hard_filter_triggered": res2.hard_filter_triggered,
                "hard_filter_reason": res2.hard_filter_reason
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
            print(f"  Ошибка Stage 2: {e}")

    print(f"\n=== Пилот v3 завершен. Результаты в {out_file} ===")

if __name__ == "__main__":
    asyncio.run(main())
