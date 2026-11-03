"""
assess_worker: берёт discovered с description и без evaluation_json,
прогоняет Stage 1 (assess1.md, Qwen) -> Stage 2 (assess2.md, MiMo + код-страховка),
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
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from config import load_settings
from database import Database
from ai_analyzer import VacancyAnalyzer, AnalysisError
from llm.providers.openai_compatible import OpenAICompatibleProvider
from llm.managed import ManagedLLMProvider
from assess_pipeline import assess_stage1, assess_stage2, extract_hints

LOG = Path(f"assess_worker_{datetime.now().strftime('%Y%m%d_%H%M')}.log")
ANALYSIS_MAX_ATTEMPTS = 3
RETRY_AFTER_MINUTES = 5
SLEEP_EMPTY = 60
SLEEP_BETWEEN = (2, 5)


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def build_evaluation_json(suitability, best_resume: str, reason: str, *, employment_type_special: bool = False, employment_type_note: str = "") -> str:
    data = {
        "verdict": suitability.verdict,
        "requirements": [r.model_dump() for r in suitability.requirements],
        "confidence": suitability.confidence,
        "best_resume": suitability.best_resume or best_resume,
        "best_resume_reason": reason,
        "hard_filter_triggered": getattr(suitability, "hard_filter_triggered", False),
        "hard_filter_reason": getattr(suitability, "hard_filter_reason", ""),
        "overqualified": getattr(suitability, "overqualified", False),
        "overqualified_reason": getattr(suitability, "overqualified_reason", ""),
        "employment_type_special": employment_type_special,
        "employment_type_note": employment_type_note,
    }
    return json.dumps(data, ensure_ascii=False)


def check_prefilter(desc: str, settings) -> tuple[str, bool, str]:
    """AI9: prefilter для assess_worker.

    Проверяет excluded_keywords (substring, casefolded) — reject без LLM.
    Проверяет employment_type_flags (regex+exceptions) — флаг, НЕ reject.

    Возвращает: (reject_reason, employment_type_special, employment_type_note).
    reject_reason == "" означает «не отсекать».
    """
    pf = settings.profile.prefilter

    def _kw_match(kw: str, text: str) -> bool:
        """AI9-fix: короткие аббревиатуры (РКО, PKI) — word boundary,
        иначе substring. Иначе «РКО» ловит «банковского», «руководство».
        """
        kw_cf = kw.casefold()
        if not kw_cf:
            return False
        # Короткая аббревиатура: len<=4, все символы — буквы, всё в верхнем регистре (в оригинале)
        is_short_abbr = len(kw) <= 4 and kw.isalpha() and kw.isupper()
        if is_short_abbr:
            return re.search(rf"(?<!\w){re.escape(kw_cf)}(?!\w)", text) is not None
        return kw_cf in text

    for kw in pf.excluded_keywords:
        if _kw_match(kw, desc.casefold()):
            return (f"keyword: {kw}", False, "")

    et_special = False
    et_note = ""
    et_filter = pf.employment_type_filter
    if et_filter is not None:
        matched_pattern = ""
        for pattern in et_filter.patterns:
            try:
                if re.search(pattern, desc, re.IGNORECASE):
                    matched_pattern = pattern
                    break
            except re.error:
                continue
        if matched_pattern:
            matched_exception = False
            for exc in et_filter.exceptions:
                try:
                    if re.search(exc, desc, re.IGNORECASE):
                        matched_exception = True
                        break
                except re.error:
                    continue
            if not matched_exception:
                et_special = True
                et_note = et_filter.note or ""
    return ("", et_special, et_note)


async def assess_one(analyzer, settings, db, row: dict, dry_run: bool) -> str:
    """Возвращает: ok | reject | fail | retry."""
    vid = row["id"]
    title = row["title"]
    desc = row["description"] or ""
    try:
        # AI9 prefilter: excluded_keywords (reject) + employment_type_flags (flag)
        prefilter_reject, et_special, et_note = check_prefilter(desc, settings)
        if prefilter_reject:
            if dry_run:
                log(f"DRY-PREFILTER {vid} '{title[:40]}' reason={prefilter_reject}")
                return "reject"
            eval_json = json.dumps({
                "stage": 0, "verdict": f"prefilter: {prefilter_reject}",
                "best_resume": None, "confidence": 0.0,
            }, ensure_ascii=False)
            ok = db.reject_by_filter(vid, reason=prefilter_reject, evaluation_json=eval_json)
            if not ok:
                log(f"FAIL reject_by_filter {vid}")
                return "fail"
            log(f"REJECT-PREFILTER {vid} '{title[:40]}' reason={prefilter_reject}")
            return "reject"

        summaries = {k: v.summary for k, v in settings.profile.resumes.items()}
        hints = extract_hints(title, desc)

        # Stage 1: скрининг (Qwen)
        res1 = await assess_stage1(
            analyzer=analyzer, title=title, description=desc,
            summaries=summaries, hints=hints, vacancy_id=vid,
        )
        if not res1.suitable:
            if dry_run:
                log(f"DRY-STAGE1-REJECT {vid} '{title[:40]}' reason={(res1.reason or '')[:80]}")
                return "reject"
            eval_json = json.dumps({
                "stage": 1, "verdict": res1.reason,
                "best_resume": None, "confidence": res1.confidence or 0.0,
                "employment_type_special": et_special,
                "employment_type_note": et_note,
            }, ensure_ascii=False)
            ok = db.reject_by_assess(
                vid, evaluation_json=eval_json,
                confidence=res1.confidence or 0.0, verdict=f"Stage 1: {res1.reason}"[:300],
            )
            if not ok:
                log(f"FAIL reject_by_assess (stage1) {vid}")
                return "fail"
            log(f"REJECT-STAGE1 {vid} '{title[:40]}' reason={(res1.reason or '')[:80]}")
            return "reject"

        # Stage 2: оценка (MiMo + код-страховка)
        best_resume = res1.best_resume or "RP"
        if best_resume not in settings.profile.resumes:
            log(f"NO_RESUME {vid} best_resume={best_resume}")
            if not dry_run:
                db.mark_analysis_failed(vid, error_type="resume_not_found",
                                        now=datetime.now(),
                                        retry_after=timedelta(minutes=RETRY_AFTER_MINUTES),
                                        max_attempts=ANALYSIS_MAX_ATTEMPTS)
            return "fail"
        resume_text = settings.profile.resumes[best_resume].full_text
        alternatives = {
            k: {"title": v.title, "summary_short": v.summary[:200]}
            for k, v in settings.profile.resumes.items() if k != best_resume
        }
        suitability = await assess_stage2(
            analyzer=analyzer, title=title, description=desc,
            best_resume=best_resume, resume_text=resume_text,
            alternatives=alternatives, vacancy_id=vid,
        )
        reason = res1.reason or ""

        eval_json = build_evaluation_json(suitability, best_resume, reason, employment_type_special=et_special, employment_type_note=et_note)

        if dry_run:
            log(f"DRY {vid} '{title[:40]}' suitable={suitability.suitable} "
                f"conf={suitability.confidence:.2f} best={best_resume} "
                f"hf={suitability.hard_filter_triggered}")
            return "ok" if suitability.suitable else "reject"

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
    adapter = OpenAICompatibleProvider(base_url, api_key, json_mode=False, reasoning_enabled=False)
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
