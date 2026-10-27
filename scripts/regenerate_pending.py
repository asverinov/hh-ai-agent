"""
Регенерация pending_approval через каскад Stage 0->1->2 + cover letter v4.2.
Обновляет cover_letter, evaluation_json, fit_summary, confidence, llm_decision, llm_reason.
Статус НЕ меняет (остаётся pending_approval).

Запуск:
  python3 scripts/regenerate_pending.py --limit 3 --dry-run   # smoke, не пишет в БД
  python3 scripts/regenerate_pending.py --limit 3             # smoke, пишет в БД
  nohup python3 scripts/regenerate_pending.py > /tmp/regen.log 2>&1 &   # все 34 в фоне
"""
import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from config import load_settings
from ai_analyzer import VacancyAnalyzer
from database import Database
from llm.providers.openai_compatible import OpenAICompatibleProvider
from llm.managed import ManagedLLMProvider

LOG = Path(f"regen_{datetime.now().strftime('%Y%m%d_%H%M')}.log")
ASSESS_DIGEST = Path("docs/datasets/ds_context_digest_v3.md")
CL_DIGEST = Path("docs/datasets/ds_cover_letter_digest.md")

def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def build_fit_summary(suitability, best_resume):
    by_level = {}
    for r in suitability.requirements:
        by_level.setdefault(r.level, []).append(r)
    parts = []
    for lvl in sorted(by_level.keys()):
        items = by_level[lvl]
        if lvl == 1:
            score = min(r.match for r in items)
            parts.append(f"Отсекающие: {score:.0%}")
        else:
            score = sum(r.match for r in items) / len(items)
            label = items[0].label if items else ""
            parts.append(f"{label}: {score:.0%}")
    if best_resume:
        parts.append(f"Резюме: {best_resume}")
    return "\n".join(parts)

async def process_one(analyzer, settings, db, vid, title, desc, company, dry_run):
    try:
        candidates = analyzer.select_resume_stage0(title, desc)
        if len(candidates) > 1:
            best_resume, reason = await analyzer.select_resume_stage1(title, desc, candidates)
        else:
            best_resume, reason = candidates[0], "Stage 0 (single)"
        resume_text = settings.profile.resumes[best_resume].full_text if best_resume in settings.profile.resumes else ""

        digest = ASSESS_DIGEST.read_text(encoding="utf-8") if ASSESS_DIGEST.exists() else None
        suitability = await analyzer.assess(
            title, desc, vacancy_id=vid,
            model_override=settings.llm.assess_model,
            session_id="regen-assess",
            system_override=digest,
            best_resume=best_resume, resume_text=resume_text,
        )

        if suitability.suitable:
            cl_digest = CL_DIGEST.read_text(encoding="utf-8") if CL_DIGEST.exists() else None
            letter = await analyzer.generate_cover_letter(
                title, desc, company_name=company,
                vacancy_id=vid,
                model_override=settings.llm.cover_letter_model,
                system_override=cl_digest,
                session_id="regen-cover",
                best_resume=best_resume, resume_text=resume_text,
            )
        else:
            letter = ""

        eval_data = {
            "verdict": suitability.verdict,
            "requirements": [
                {"level": r.level, "label": r.label, "text": r.text,
                 "match": r.match, "evidence": r.evidence}
                for r in suitability.requirements
            ],
            "confidence": suitability.confidence,
            "best_resume": best_resume,
            "best_resume_reason": reason,
        }
        fit = build_fit_summary(suitability, best_resume)

        if not dry_run:
            with sqlite3.connect(str(db.path)) as conn:
                if suitability.suitable:
                    conn.execute(
                        """UPDATE vacancies SET
                           cover_letter=?, evaluation_json=?, fit_summary=?,
                           confidence=?, llm_decision=?, llm_reason=?
                           WHERE id=?""",
                        (letter, json.dumps(eval_data, ensure_ascii=False), fit,
                         suitability.confidence, 1, suitability.verdict, vid),
                    )
                else:
                    conn.execute(
                        """UPDATE vacancies SET
                           evaluation_json=?, fit_summary=?,
                           confidence=?, llm_decision=0, llm_reason=?,
                           status='rejected_by_llm'
                           WHERE id=? AND status='pending_approval'""",
                        (json.dumps(eval_data, ensure_ascii=False), fit,
                         suitability.confidence, suitability.verdict, vid),
                    )
                conn.commit()

        return (vid, best_resume, suitability.confidence, len(letter), suitability.suitable, None)
    except Exception as e:
        return (vid, None, None, None, None, f"{type(e).__name__}: {str(e)[:200]}")

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = все")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    settings = load_settings()
    db = Database(str(settings.database_path))
    db.init()

    api_key = (settings.llm.aitunnel_api_key or "").strip()
    base_url = (settings.llm.aitunnel_base_url or "https://api.aitunnel.ru/v1").strip()
    if not api_key:
        log("FATAL: AITUNNEL_API_KEY пуст")
        sys.exit(1)
    adapter = OpenAICompatibleProvider(base_url, api_key, json_mode=False, reasoning_enabled=None)
    provider = ManagedLLMProvider(adapter, db, max_retries=1, max_requests_per_day=2000)
    analyzer = VacancyAnalyzer(settings, provider)

    conn = sqlite3.connect(str(db.path))
    sql = "SELECT id, title, description, company FROM vacancies WHERE status='pending_approval' AND description != '' ORDER BY id"
    if args.limit:
        sql += f" LIMIT {args.limit}"
    rows = conn.execute(sql).fetchall()
    conn.close()

    log(f"Найдено {len(rows)} pending с description. dry_run={args.dry_run}")
    ok, fail, reject = 0, 0, 0
    for i, (vid, title, desc, company) in enumerate(rows, 1):
        res = await process_one(analyzer, settings, db, vid, title, desc, company, args.dry_run)
        vid_, best, conf, letter_len, suitable, err = res
        if err:
            fail += 1
            log(f"[{i}/{len(rows)}] FAIL {vid} '{title[:40]}': {err}")
        else:
            if suitable:
                ok += 1
                log(f"[{i}/{len(rows)}] OK {vid} '{title[:40]}' -> {best} conf={conf:.2f} letter={letter_len}симв")
            else:
                reject += 1
                log(f"[{i}/{len(rows)}] REJECT {vid} '{title[:40]}' -> {best} conf={conf:.2f} (suitable=false)")
        await asyncio.sleep(2)
    log(f"ГОТОВО: ok={ok} reject={reject} fail={fail} из {len(rows)}")

if __name__ == "__main__":
    asyncio.run(main())
