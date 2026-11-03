"""
Дотягивает description для pending_approval без него, прогоняет полный каскад.
- VACANCY_REMOVED/HIDDEN/ACCESS_DENIED -> status='expired'
- VACANCY_LOADED -> UPDATE description + каскад Stage 0->1->2 + письмо v4.2
- CAPTCHA_DETECTED/PAGE_STRUCTURE_CHANGED/NETWORK_ERROR -> лог, статус не меняем

Запуск:
  python3 scripts/fetch_descr_and_regen.py --limit 1 --dry-run
  python3 scripts/fetch_descr_and_regen.py --limit 1
  nohup env PYTHONPATH=. python3 scripts/fetch_descr_and_regen.py > /tmp/fetch.log 2>&1 &
"""
import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from config import load_settings
from database import Database
from approval import ApprovalGuard
from browser_backend import create_browser_backend
from hh_client import HHClient, PageState, VacancySummary
from ai_analyzer import VacancyAnalyzer
from llm.providers.openai_compatible import OpenAICompatibleProvider
from llm.managed import ManagedLLMProvider

LOG = Path(f"fetch_{datetime.now().strftime('%Y%m%d_%H%M')}.log")
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


async def run_cascade(analyzer, settings, vid, title, desc, company, dry_run, db):
    """Возвращает (best_resume, conf, letter_len, suitable, err_or_None)."""
    try:
        # AI9: select_resume_stage0 удалён. DEPRECATED скрипт, fallback на RP.
        candidates = ["RP"]
        if len(candidates) > 1:
            best_resume, reason = await analyzer.select_resume_stage1(title, desc, candidates)
        else:
            best_resume, reason = candidates[0], "Stage 0 (single)"
        resume_text = settings.profile.resumes[best_resume].full_text if best_resume in settings.profile.resumes else ""

        digest = ASSESS_DIGEST.read_text(encoding="utf-8") if ASSESS_DIGEST.exists() else None
        suitability = await analyzer.assess(
            title, desc, vacancy_id=vid,
            model_override=settings.llm.assess_model,
            session_id="fetch-assess",
            system_override=digest,
            best_resume=best_resume, resume_text=resume_text,
        )

        letter = ""
        if suitability.suitable:
            cl_digest = CL_DIGEST.read_text(encoding="utf-8") if CL_DIGEST.exists() else None
            letter = await analyzer.generate_cover_letter(
                title, desc, company_name=company,
                vacancy_id=vid,
                model_override=settings.llm.cover_letter_model,
                system_override=cl_digest,
                session_id="fetch-cover",
                best_resume=best_resume, resume_text=resume_text,
            )

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
                           description=?, cover_letter=?, evaluation_json=?, fit_summary=?,
                           confidence=?, llm_decision=1, llm_reason=?
                           WHERE id=?""",
                        (desc, letter, json.dumps(eval_data, ensure_ascii=False), fit,
                         suitability.confidence, suitability.verdict, vid),
                    )
                else:
                    conn.execute(
                        """UPDATE vacancies SET
                           description=?, evaluation_json=?, fit_summary=?,
                           confidence=?, llm_decision=0, llm_reason=?,
                           status='rejected_by_llm'
                           WHERE id=? AND status='pending_approval'""",
                        (desc, json.dumps(eval_data, ensure_ascii=False), fit,
                         suitability.confidence, suitability.verdict, vid),
                    )
                conn.commit()

        return (best_resume, suitability.confidence, len(letter), suitability.suitable, None, suitability.verdict[:300])
    except Exception as e:
        return (None, None, None, None, f"{type(e).__name__}: {str(e)[:200]}", "")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = все 20")
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
    sql = ("SELECT id, title, url FROM vacancies WHERE status='pending_approval' "
           "AND (description IS NULL OR description='') ORDER BY id")
    if args.limit:
        sql += f" LIMIT {args.limit}"
    rows = conn.execute(sql).fetchall()
    conn.close()

    log(f"Найдено {len(rows)} pending без description. dry_run={args.dry_run}")

    backend = create_browser_backend(settings)
    context = await backend.start()
    hh_client = None
    try:
        guard = ApprovalGuard(settings, db)
        hh_client = HHClient(context, settings, db, guard)
        if not await hh_client.ensure_login():
            log("FATAL: hh.ru login required")
            sys.exit(1)

        ok, expired, skip, fail = 0, 0, 0, 0
        for i, (vid, title, url) in enumerate(rows, 1):
            try:
                summary = VacancySummary(id=vid, title=title, url=url, search_query="")
                details = await hh_client.read_vacancy(summary, None)
                state = details.state
                desc = (details.description or "").strip()
                company = details.company or ""

                if state in (PageState.VACANCY_REMOVED, PageState.VACANCY_HIDDEN, PageState.ACCESS_DENIED):
                    if not args.dry_run:
                        with sqlite3.connect(str(db.path)) as c:
                            c.execute(
                                "UPDATE vacancies SET status='expired', llm_reason=? WHERE id=? AND status='pending_approval'",
                                (f"fetch_descr: {state.value}", vid),
                            )
                            c.commit()
                    expired += 1
                    log(f"[{i}/{len(rows)}] EXPIRED {vid} '{title[:40]}' ({state.value})")
                    continue

                if state is not PageState.VACANCY_LOADED or not desc:
                    skip += 1
                    log(f"[{i}/{len(rows)}] SKIP {vid} '{title[:40]}' state={state.value} desc_len={len(desc)}")
                    continue

                res = await run_cascade(analyzer, settings, vid, title, desc, company, args.dry_run, db)
                best, conf, letter_len, suitable, err, verdict_short = res
                if err:
                    fail += 1
                    verdict_short = ""
                    log(f"[{i}/{len(rows)}] FAIL {vid} '{title[:40]}': {err}")
                elif suitable:
                    ok += 1
                    log(f"[{i}/{len(rows)}] OK {vid} '{title[:40]}' -> {best} conf={conf:.2f} letter={letter_len}симв")
                else:
                    fail += 1
                    log(f"[{i}/{len(rows)}] REJECT {vid} '{title[:40]}' -> {best} conf={conf:.2f} verdict={verdict_short}")
            except Exception as e:
                fail += 1
                log(f"[{i}/{len(rows)}] EXC {vid}: {type(e).__name__}: {str(e)[:200]}")
            await asyncio.sleep(2)

        log(f"ГОТОВО: ok={ok} reject/fail={fail} expired={expired} skip={skip} из {len(rows)}")
    finally:
        if hh_client is not None:
            try:
                await hh_client.close()
            except Exception:
                pass
        try:
            await backend.close()
        except Exception:
            try:
                await context.close()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
