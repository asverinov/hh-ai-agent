#!/usr/bin/env python3
"""evaluate_discovered.py — оценка discovered-вакансий через DeepSeek + дайджест.

Не трогает CloakBrowser, не идёт через поиск, не задевает circuit breaker.
Берёт discovered с description, оценивает, обновляет статусы.

Запуск:
    python3 scripts/evaluate_discovered.py --dry-run
    python3 scripts/evaluate_discovered.py
    python3 scripts/evaluate_discovered.py --limit 10
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime, UTC, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_settings
from database import Database, VacancyStatus
from main import (
    _create_cover_letter_analyzer,
    _load_context_digest,
    _load_cover_letter_digest,
)
from ai_analyzer import AnalysisError

CONTEXT_SESSION = "context-main"
COVER_SESSION = "cover-letter-main"
ANALYSIS_MAX_ATTEMPTS = 3


async def evaluate_one(db, analyzer, settings, context_digest, cover_digest, row):
    vid, title, desc, url, company = row
    company = company or ""

    try:
        suitability = await analyzer.assess(
            title, desc, vacancy_id=vid,
            model_override=settings.llm.assess_model,
            session_id=CONTEXT_SESSION,
            system_override=context_digest,
        )
    except AnalysisError as exc:
        db.mark_analysis_failed(
            vid, error_type=exc.error_type, now=datetime.now(UTC),
            retry_after=timedelta(minutes=30),
            max_attempts=ANALYSIS_MAX_ATTEMPTS,
        )
        return f"analysis_error:{exc.error_type}"
    except Exception as exc:
        return f"error:{type(exc).__name__}:{exc}"

    eval_data = {
        "verdict": suitability.verdict,
        "requirements": [
            {"level": r.level, "label": r.label, "text": r.text,
             "match": r.match, "evidence": r.evidence}
            for r in suitability.requirements
        ],
        "confidence": suitability.confidence,
        "best_resume": suitability.best_resume,
        "best_resume_reason": suitability.best_resume_reason,
    }
    eval_json = json.dumps(eval_data, ensure_ascii=False)

    llm_meta = db.latest_llm_request_meta(vid)
    if llm_meta:
        try:
            db.record_evaluation(
                vacancy_id=vid, llm_request_id=llm_meta["id"],
                provider=llm_meta["provider"], model=llm_meta["model"],
                prompt_version=llm_meta["prompt_version"],
                suitable=suitability.suitable,
                confidence=suitability.confidence,
                verdict=suitability.verdict,
                requirements_json=eval_json, is_calibration=0,
            )
        except Exception as exc:
            print(f"    record_evaluation_warn: {exc}")

    if not suitability.suitable:
        db.transition(
            vid, VacancyStatus.DISCOVERED, VacancyStatus.REJECTED_BY_LLM,
            llm_decision=False, llm_reason=suitability.verdict,
            confidence=suitability.confidence,
            evaluation_json=eval_json, error_text="",
        )
        return f"rejected conf={suitability.confidence:.2f}"

    # suitable=True: генерируем сопровод через qwen + v3.3.1
    try:
        letter = await analyzer.generate_cover_letter(
            title, desc, company_name=company,
            vacancy_id=vid,
            model_override=settings.llm.cover_letter_model,
            system_override=cover_digest,
            session_id=COVER_SESSION,
        )
    except Exception as exc:
        db.transition(
            vid, VacancyStatus.DISCOVERED, VacancyStatus.APPLY_FAILED,
            llm_decision=True, llm_reason=suitability.verdict,
            confidence=suitability.confidence,
            evaluation_json=eval_json,
            error_text=f"cover_letter_failed:{type(exc).__name__}",
        )
        return f"cover_letter_failed:{type(exc).__name__}"

    if not letter.strip():
        db.transition(
            vid, VacancyStatus.DISCOVERED, VacancyStatus.APPLY_FAILED,
            llm_decision=True, llm_reason=suitability.verdict,
            confidence=suitability.confidence,
            evaluation_json=eval_json, error_text="cover_letter_empty",
        )
        return "cover_letter_empty"

    db.request_approval(
        job_id=vid, cover_letter=letter,
        llm_decision=True, llm_reason=suitability.verdict,
        confidence=suitability.confidence,
        fit_summary=suitability.verdict[:200],
        evaluation_json=eval_json,
        now=datetime.now(UTC),
    )
    return f"approved conf={suitability.confidence:.2f} len={len(letter)}"


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    db = Database(settings.database_path)

    context_digest = _load_context_digest()
    cover_digest = _load_cover_letter_digest()
    if not context_digest:
        sys.exit("ERROR: ds_context_digest.md не найден")
    if not cover_digest:
        sys.exit("ERROR: ds_cover_letter_digest.md не найден")

    print(f"assess_model={settings.llm.assess_model}")
    print(f"cover_model ={settings.llm.cover_letter_model}")
    print(f"context_digest={len(context_digest)} chars")
    print(f"cover_digest  ={len(cover_digest)} chars")

    analyzer = _create_cover_letter_analyzer(settings, db)
    if analyzer is None:
        sys.exit("ERROR: AITUNNEL analyzer не создан")

    with db._connect() as conn:
        rows = conn.execute("""
            SELECT id, title, description, url, company
            FROM vacancies
            WHERE status = 'discovered' AND description != ''
            ORDER BY discovered_at ASC
        """).fetchall()

    if args.limit:
        rows = rows[: args.limit]

    total = len(rows)
    print(f"\nВсего к оценке: {total}")

    if args.dry_run:
        for row in rows[:10]:
            print(f"  {row[0]} — {row[1][:60]}")
        if total > 10:
            print(f"  ... (всего {total})")
        return

    ok_pending = 0
    ok_rejected = 0
    failed = 0

    for i, row in enumerate(rows, 1):
        vid = row[0]
        current = db.get(vid)
        if current is None or current.status != VacancyStatus.DISCOVERED.value:
            print(f"  [{i}/{total}] {vid} SKIP (status={current.status if current else '?'})")
            continue

        print(f"  [{i}/{total}] {vid} — {row[1][:50]}")
        result = await evaluate_one(
            db, analyzer, settings, context_digest, cover_digest, row,
        )
        print(f"    → {result}")

        if result.startswith("approved"):
            ok_pending += 1
        elif result.startswith("rejected"):
            ok_rejected += 1
        else:
            failed += 1

        await asyncio.sleep(2)

    print()
    print("=== DONE ===")
    print(f"  approved (pending_approval): {ok_pending}")
    print(f"  rejected:                    {ok_rejected}")
    print(f"  failed:                      {failed}")

    try:
        await analyzer.close()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
