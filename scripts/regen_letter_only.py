"""
Догенерирует cover_letter для pending, у которых оценка есть (suitable=true),
но письмо пустое (сбой qwen на 6000 токенах).
Запуск: python3 scripts/regen_letter_only.py [--limit N] [--dry-run]
"""
import argparse, asyncio, json, sqlite3, sys
from datetime import datetime
from pathlib import Path
from config import load_settings
from database import Database
from ai_analyzer import VacancyAnalyzer
from llm.providers.openai_compatible import OpenAICompatibleProvider
from llm.managed import ManagedLLMProvider

LOG = Path(f"regen_letters_{datetime.now().strftime('%Y%m%d_%H%M')}.log")
CL_DIGEST = Path("docs/datasets/ds_cover_letter_digest.md")


def log(m):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {m}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    s = load_settings()
    db = Database(str(s.database_path)); db.init()
    adapter = OpenAICompatibleProvider(s.llm.aitunnel_base_url, s.llm.aitunnel_api_key, json_mode=False)
    provider = ManagedLLMProvider(adapter, db, max_retries=1, max_requests_per_day=2000)
    analyzer = VacancyAnalyzer(s, provider)

    conn = sqlite3.connect(str(db.path))
    sql = """SELECT id, title, description, company, evaluation_json
             FROM vacancies
             WHERE status='pending_approval'
               AND description != ''
               AND (cover_letter IS NULL OR cover_letter = '')
             ORDER BY id"""
    if args.limit:
        sql += f" LIMIT {args.limit}"
    rows = conn.execute(sql).fetchall()
    conn.close()

    log(f"Найдено {len(rows)} pending с пустым письмом. dry_run={args.dry_run}")
    cl_digest = CL_DIGEST.read_text(encoding="utf-8") if CL_DIGEST.exists() else None

    ok, fail = 0, 0
    for i, (vid, title, desc, company, eval_json) in enumerate(rows, 1):
        try:
            best_resume = None
            if eval_json:
                try:
                    best_resume = json.loads(eval_json).get("best_resume")
                except Exception:
                    pass
            if not best_resume:
                best_resume = "RP"  # fallback
            resume_text = s.profile.resumes[best_resume].full_text if best_resume in s.profile.resumes else ""

            letter = await analyzer.generate_cover_letter(
                title, desc, company_name=company or "",
                vacancy_id=vid,
                model_override=s.llm.cover_letter_model,
                system_override=cl_digest,
                session_id="regen-letter",
                best_resume=best_resume,
                resume_text=resume_text,
            )

            if not letter.strip():
                fail += 1
                log(f"[{i}/{len(rows)}] FAIL {vid} '{title[:40]}' empty letter after retry")
                continue

            if not args.dry_run:
                with sqlite3.connect(str(db.path)) as c:
                    c.execute("UPDATE vacancies SET cover_letter=? WHERE id=?", (letter, vid))
                    c.commit()
            ok += 1
            log(f"[{i}/{len(rows)}] OK {vid} '{title[:40]}' -> {best_resume} letter={len(letter)}симв")
        except Exception as e:
            fail += 1
            log(f"[{i}/{len(rows)}] EXC {vid}: {type(e).__name__}: {str(e)[:200]}")
        await asyncio.sleep(2)

    log(f"ГОТОВО: ok={ok} fail={fail} из {len(rows)}")


if __name__ == "__main__":
    asyncio.run(main())
