#!/usr/bin/env python3
"""generate_letters_for_pending.py — одноразовый скрипт для 225 pending_approval без письма.

НЕ трогает статусы. Просто: SELECT pending_approval WHERE cover_letter='' ->
generate_cover_letter -> UPDATE cover_letter / fit_summary.

Отличие от cover_letter_worker: worker ищет discovered и переводит в pending_approval.
Здесь — уже pending_approval, надо только дописать письмо.

Запуск:
  PYTHONPATH=. python3 scripts/generate_letters_for_pending.py --limit 3 --dry-run
  PYTHONPATH=. python3 scripts/generate_letters_for_pending.py --limit 3
  PYTHONPATH=. python3 scripts/generate_letters_for_pending.py
"""
import argparse
import asyncio
import json
import random
import sys
from datetime import datetime
from pathlib import Path

from config import load_settings
from database import Database
from ai_analyzer import VacancyAnalyzer, AnalysisError
from cover_letter import cover_letter_validation_error
from llm.providers.openai_compatible import OpenAICompatibleProvider
from llm.managed import ManagedLLMProvider

CL_DIGEST = Path("docs/datasets/ds_cover_letter_digest.md")
SLEEP_BETWEEN = (2, 5)


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def fit_summary_from_eval(eval_json: str) -> str:
    """Собрать «Отсекающие: X% / Опыт: Y% / Резюме: Z» из evaluation_json."""
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


async def process_one(analyzer, settings, db, row: dict, cl_digest: str, dry_run: bool) -> str:
    """Возвращает: ok | fail | skip."""
    vid = row["id"]
    title = row["title"] or ""
    desc = row["description"] or ""
    eval_json = row["evaluation_json"] or ""

    try:
        data = json.loads(eval_json)
    except (json.JSONDecodeError, TypeError):
        log(f"BAD_EVAL_JSON {vid}")
        return "fail"

    best_resume = data.get("best_resume") or "RP"
    if best_resume not in settings.profile.resumes:
        log(f"WARN {vid}: best_resume={best_resume!r} invalid, using RP")
        best_resume = "RP"
    resume_text = (
        settings.profile.resumes[best_resume].full_text
        if best_resume in settings.profile.resumes else ""
    )
    if not resume_text:
        log(f"NO_RESUME_TEXT {vid} best={best_resume}")
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
    except SystemExit as e:
        # _safe_letter вызвал SystemExit(1) — плохое письмо, пропускаем
        log(f"SAFE_LETTER_ABORT {vid}: code={e.code}")
        return "skip"
    except AnalysisError as e:
        log(f"LLM_FAIL {vid}: {e.error_type}")
        return "fail"
    except Exception as e:
        log(f"EXC {vid}: {type(e).__name__}: {str(e)[:150]}")
        return "fail"

    body = (letter or "").strip()
    if not body:
        log(f"EMPTY_LETTER {vid} best={best_resume}")
        return "fail"

    cl = settings.profile.cover_letter
    err = cover_letter_validation_error(
        body, cl.required_portfolio_url, cl.closing, cl.max_length
    )
    if err:
        log(f"VALIDATION_FAIL {vid}: {err}")
        return "fail"

    fit = fit_summary_from_eval(eval_json)

    if dry_run:
        log(f"DRY {vid} '{title[:40]}' best={best_resume} letter_len={len(body)}")
        return "ok"

    with db._connect() as c:
        cur = c.execute(
            "UPDATE vacancies SET cover_letter = ?, fit_summary = ? "
            "WHERE id = ? AND status = 'pending_approval'",
            (body, fit, vid),
        )
        c.commit()
        if cur.rowcount != 1:
            log(f"UPDATE_FAIL {vid}: rowcount={cur.rowcount}")
            return "fail"

    log(f"OK {vid} '{title[:40]}' best={best_resume} letter_len={len(body)}")
    return "ok"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="Ограничить количество")
    ap.add_argument("--dry-run", action="store_true", help="Не писать в БД")
    args = ap.parse_args()

    settings = load_settings()
    db = Database(str(settings.database_path))
    db.init()

    cl_digest = CL_DIGEST.read_text(encoding="utf-8") if CL_DIGEST.exists() else ""
    if not cl_digest:
        log(f"FATAL: {CL_DIGEST} не найден")
        return

    api_key = (settings.llm.aitunnel_api_key or "").strip()
    base_url = (settings.llm.aitunnel_base_url or "https://api.aitunnel.ru/v1").strip()
    if not api_key:
        log("FATAL: AITUNNEL_API_KEY пуст")
        return
    adapter = OpenAICompatibleProvider(base_url, api_key, json_mode=False, reasoning_enabled=False)
    provider = ManagedLLMProvider(adapter, db, max_retries=1, max_requests_per_day=2000)
    analyzer = VacancyAnalyzer(settings, provider)

    with db._connect() as c:
        c.row_factory = None  # default tuple; используем именованные через db._vacancy? нет, проще dict
    with db._connect() as c:
        c.row_factory = __import__("sqlite3").Row
        sql = """
            SELECT id, title, description, evaluation_json
            FROM vacancies
            WHERE status = 'pending_approval'
              AND (cover_letter IS NULL OR cover_letter = '')
            ORDER BY id
        """
        if args.limit > 0:
            sql += f" LIMIT {int(args.limit)}"
        rows = [dict(r) for r in c.execute(sql).fetchall()]

    log(f"START: {len(rows)} pending_approval без письма (dry_run={args.dry_run})")
    if not rows:
        return

    stats = {"ok": 0, "fail": 0, "skip": 0}
    for i, row in enumerate(rows, 1):
        log(f"[{i}/{len(rows)}] {row['id']} - {(row['title'] or '')[:50]}")
        result = await process_one(analyzer, settings, db, row, cl_digest, args.dry_run)
        stats[result] = stats.get(result, 0) + 1
        if i < len(rows):
            await asyncio.sleep(random.uniform(*SLEEP_BETWEEN))

    log(f"=== ИТОГ: ok={stats['ok']} fail={stats['fail']} skip={stats['skip']} ===")
    try:
        await provider.close()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Прервано пользователем")
        sys.exit(130)
