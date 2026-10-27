"""
cover_letter_worker: берёт discovered с evaluation_json и llm_decision=1 (suitable=true),
генерирует письмо (AITUNNEL/qwen + ds_cover_letter_digest.md v4.2),
переводит в pending_approval. Без браузера, без flock — только LLM.

Запуск:
  python3 scripts/cover_letter_worker.py --limit 1 --dry-run
  python3 scripts/cover_letter_worker.py --limit 1
  nohup env PYTHONPATH=. python3 scripts/cover_letter_worker.py > /tmp/cover.log 2>&1 &
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

LOG = Path(f"cover_letter_worker_{datetime.now().strftime('%Y%m%d_%H%M')}.log")
CL_DIGEST = Path("docs/datasets/ds_cover_letter_digest.md")
ANALYSIS_MAX_ATTEMPTS = 3
RETRY_AFTER_MINUTES = 5
SLEEP_EMPTY = 60
SLEEP_BETWEEN = (2, 5)


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fit_summary_from_eval(eval_json: str) -> str:
    """Собрать «Отсекающие: X% / Опыт: Y% / Резюме: Z» из evaluation_json.

    Повторяет build_fit_summary из fetch_descr_and_regen.py, но по JSON.
    """
    try:
        data = json.loads(eval_json)
    except (json.JSONDecodeError, TypeError):
        return ""
    requirements = data.get("requirements") or []
    by_level: dict[int, list[dict]] = {}
    for r in requirements:
        try:
            lvl = int(r.get("level", 0))
        except (TypeError, ValueError):
            continue
        by_level.setdefault(lvl, []).append(r)
    parts = []
    for lvl in sorted(by_level.keys()):
        items = by_level[lvl]
        if lvl == 1:
            score = min(float(r.get("match", 0.0)) for r in items)
            parts.append(f"Отсекающие: {score:.0%}")
        else:
            score = sum(float(r.get("match", 0.0)) for r in items) / len(items)
            label = items[0].get("label", "") if items else ""
            parts.append(f"{label}: {score:.0%}")
    best = data.get("best_resume") or ""
    if best:
        parts.append(f"Резюме: {best}")
    return "\n".join(parts)


async def process_one(analyzer, settings, db, row: dict, dry_run: bool) -> str:
    """Возвращает: ok | fail | retry."""
    vid = row["id"]
    title = row["title"]
    desc = row["description"] or ""
    eval_json = row["evaluation_json"] or ""
    try:
        data = json.loads(eval_json)
    except (json.JSONDecodeError, TypeError) as e:
        log(f"BAD_EVAL {vid}: {e}")
        if not dry_run:
            db.mark_analysis_failed(vid, error_type="bad_evaluation_json",
                                    now=datetime.now(),
                                    retry_after=timedelta(minutes=RETRY_AFTER_MINUTES),
                                    max_attempts=ANALYSIS_MAX_ATTEMPTS)
        return "fail"

    best_resume = data.get("best_resume") or ""
    if best_resume not in settings.profile.resumes:
        # fallback: возьмём первый кандидат Stage 0
        cands = analyzer.select_resume_stage0(title, desc)
        best_resume = cands[0] if cands else ""

    resume_text = (
        settings.profile.resumes[best_resume].full_text
        if best_resume in settings.profile.resumes else ""
    )
    if not resume_text:
        log(f"NO_RESUME_TEXT {vid} best={best_resume}")
        if not dry_run:
            db.mark_analysis_failed(vid, error_type="no_resume_text",
                                    now=datetime.now(),
                                    retry_after=timedelta(minutes=RETRY_AFTER_MINUTES),
                                    max_attempts=ANALYSIS_MAX_ATTEMPTS)
        return "fail"

    cl_digest = CL_DIGEST.read_text(encoding="utf-8") if CL_DIGEST.exists() else None
    if not cl_digest:
        log("FATAL: ds_cover_letter_digest.md не найден")
        return "fail"

    try:
        letter = await analyzer.generate_cover_letter(
            title, desc, company_name="",
            vacancy_id=vid,
            model_override=settings.llm.cover_letter_model,
            system_override=cl_digest,
            session_id="cover-letter-main",
            best_resume=best_resume, resume_text=resume_text,
        )
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

    body = (letter or "").strip()
    if not body:
        log(f"EMPTY_LETTER {vid} best={best_resume}")
        if not dry_run:
            db.mark_analysis_failed(vid, error_type="empty_cover_letter",
                                    now=datetime.now(),
                                    retry_after=timedelta(minutes=RETRY_AFTER_MINUTES),
                                    max_attempts=ANALYSIS_MAX_ATTEMPTS)
        return "retry"

    fit = fit_summary_from_eval(eval_json)
    conf = float(row.get("confidence") or data.get("confidence") or 0.0)
    verdict = str(data.get("verdict") or "")

    if dry_run:
        log(f"DRY {vid} '{title[:40]}' best={best_resume} letter_len={len(body)}")
        return "ok"

    ok = db.request_approval(
        job_id=vid, cover_letter=body, llm_decision=True,
        llm_reason=verdict, confidence=conf, fit_summary=fit,
        evaluation_json=eval_json, now=datetime.now(),
    )
    if not ok:
        log(f"FAIL request_approval {vid}")
        return "fail"
    log(f"OK {vid} '{title[:40]}' -> {best_resume} letter_len={len(body)}")
    return "ok"


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
    stats = {"ok": 0, "fail": 0, "retry": 0}
    while True:
        row = db.dequeue_cover_letter()
        if row is None:
            if args.once:
                log("очередь пуста, --once → выход")
                break
            log(f"очередь пуста, пауза {SLEEP_EMPTY} сек")
            await asyncio.sleep(SLEEP_EMPTY)
            continue

        res = await process_one(analyzer, settings, db, row, args.dry_run)
        stats[res] = stats.get(res, 0) + 1
        processed += 1
        log(f"progress: {processed} {stats}")

        if args.limit and processed >= args.limit:
            log(f"--limit {args.limit} достигнут, выход")
            break
        await asyncio.sleep(0.1)

    log(f"ГОТОВО: {stats}")


if __name__ == "__main__":
    asyncio.run(main())
