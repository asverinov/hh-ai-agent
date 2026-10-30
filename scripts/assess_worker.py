"""
assess_worker: берёт discovered с description и без evaluation_json,
прогоняет Stage 0 -> Stage 1 -> Stage 2 (AITUNNEL/mimo + дайджест v3),
сохраняет evaluation_json. Не пишет письма (это cover_letter_worker).
Без браузера, без flock — только LLM.

Запуск:
  python3 scripts/assess_worker.py --limit 1 --dry-run
  python3 scripts/assess_worker.py --limit 1
  nohup env PYTHONPATH=. python3 scripts/assess_worker.py > /tmp/assess.log 2>&1 &
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from config import load_settings
from database import Database
from ai_analyzer import VacancyAnalyzer, AnalysisError
from llm.providers.openai_compatible import OpenAICompatibleProvider
from llm.managed import ManagedLLMProvider

LOG = Path(f"assess_worker_{datetime.now().strftime('%Y%m%d_%H%M')}.log")
ASSESS_DIGEST = Path("docs/datasets/ds_context_digest_v3.md")
ANALYSIS_MAX_ATTEMPTS = 3
RETRY_AFTER_MINUTES = 5
SLEEP_EMPTY = 60
SLEEP_BETWEEN = (2, 5)


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def build_evaluation_json(suitability, best_resume: str, reason: str) -> str:
    data = {
        "verdict": suitability.verdict,
        "requirements": [
            {"level": r.level, "label": r.label, "text": r.text,
             "match": r.match, "evidence": r.evidence}
            for r in suitability.requirements
        ],
        "confidence": suitability.confidence,
        "best_resume": suitability.best_resume or best_resume,
        "best_resume_reason": reason,
    }
    return json.dumps(data, ensure_ascii=False)


async def assess_one(analyzer, settings, db, row: dict, dry_run: bool) -> str:
    """Возвращает: ok | reject | fail | retry."""
    vid = row["id"]
    title = row["title"]
    desc = row["description"] or ""
    try:
        candidates = analyzer.select_resume_stage0(title, desc)
        if not candidates:
            log(f"NO_CANDIDATES {vid} '{title[:40]}'")
            if not dry_run:
                db.mark_analysis_failed(vid, error_type="no_stage0_candidates",
                                        now=datetime.now(),
                                        retry_after=timedelta(minutes=RETRY_AFTER_MINUTES),
                                        max_attempts=ANALYSIS_MAX_ATTEMPTS)
            return "fail"
        if len(candidates) > 1:
            best_resume, reason = await analyzer.select_resume_stage1(title, desc, candidates, vacancy_id=vid)
        else:
            best_resume, reason = candidates[0], "Stage 0 heuristic"

        resume_text = (
            settings.profile.resumes[best_resume].full_text
            if best_resume in settings.profile.resumes else ""
        )
        digest = ASSESS_DIGEST.read_text(encoding="utf-8") if ASSESS_DIGEST.exists() else None
        suitability = await analyzer.assess(
            title, desc, vacancy_id=vid,
            model_override=settings.llm.assess_model,
            session_id="assess-worker",
            system_override=digest,
            best_resume=best_resume, resume_text=resume_text,
        )

        eval_json = build_evaluation_json(suitability, best_resume, reason)

        if dry_run:
            log(f"DRY {vid} '{title[:40]}' suitable={suitability.suitable} "
                f"conf={suitability.confidence:.2f} best={best_resume}")
            return "ok"

        if suitability.suitable:
            ok = db.store_analysis(
                vid, cover_letter="", llm_decision=True,
                llm_reason=suitability.verdict,
                confidence=suitability.confidence,
                fit_summary="", evaluation_json=eval_json,
            )
            if not ok:
                log(f"FAIL store_analysis {vid}")
                return "fail"
            log(f"OK {vid} '{title[:40]}' -> {best_resume} conf={suitability.confidence:.2f}")
            return "ok"
        else:
            ok = db.reject_by_assess(
                vid, evaluation_json=eval_json,
                confidence=suitability.confidence, verdict=suitability.verdict[:300],
            )
            if not ok:
                log(f"FAIL reject_by_assess {vid}")
                return "fail"
            log(f"REJECT {vid} '{title[:40]}' -> {best_resume} conf={suitability.confidence:.2f}")
            return "reject"

    except AnalysisError as e:
        log(f"LLM_FAIL {vid}: {e.error_type}")
        if not dry_run:
            db.mark_analysis_failed(vid, error_type=e.error_type, now=datetime.now(),
                                    retry_after=timedelta(minutes=RETRY_AFTER_MINUTES),
                                    max_attempts=ANALYSIS_MAX_ATTEMPTS)
        return "retry"
    except Exception as e:
        log(f"EXC {vid}: {type(e).__name__}: {str(e)[:200]}")
        if not dry_run:
            db.mark_analysis_failed(vid, error_type=type(e).__name__, now=datetime.now(),
                                    retry_after=timedelta(minutes=RETRY_AFTER_MINUTES),
                                    max_attempts=ANALYSIS_MAX_ATTEMPTS)
        return "fail"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--once", action="store_true")
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

    processed = 0
    stats = {"ok": 0, "reject": 0, "fail": 0, "retry": 0}
    while True:
        row = db.dequeue_assess()
        if row is None:
            if args.once:
                log("очередь пуста, --once → выход")
                break
            log(f"очередь пуста, пауза {SLEEP_EMPTY} сек")
            await asyncio.sleep(SLEEP_EMPTY)
            continue

        res = await assess_one(analyzer, settings, db, row, args.dry_run)
        stats[res] = stats.get(res, 0) + 1
        processed += 1
        log(f"progress: {processed} {stats}")

        if args.limit and processed >= args.limit:
            log(f"--limit {args.limit} достигнут, выход")
            break
        await asyncio.sleep(0.1)  # без пауз: LLM сам тормозит

    log(f"ГОТОВО: {stats}")


if __name__ == "__main__":
    asyncio.run(main())
