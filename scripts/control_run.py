"""
control_run.py — контрольный прогон 20-30 вакансий из discovered (dry-run).

Не пишет в БД. Использует ту же логику, что assess_worker:
  prefilter -> Stage 1 (Qwen) -> Stage 2 (MiMo)
Пишет JSONL + Markdown отчёт для Эксперта.

Запуск:
  PYTHONPATH=. python3 -u scripts/control_run.py --limit 30
  PYTHONPATH=. python3 -u scripts/control_run.py --limit 30 --dates 2026-11-02,2026-10-30

Ожидания (Эксперт/Пользователь):
  % true (от всех в выборке): 5-20% норма, >30% мягкость, <2% поломка
  % reject (от всех): ≥85%
  overqualified=true при suitable=true: если >40% от suitable=true — модель мягко ставит pass
  experience leaks: 0 (API-фильтр)
  junior/salary leaks: если >0 — replay prefilter нужен
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from config import load_settings
from database import Database
from ai_analyzer import VacancyAnalyzer, AnalysisError
from llm.providers.openai_compatible import OpenAICompatibleProvider
from llm.managed import ManagedLLMProvider
from assess_pipeline import assess_stage1, assess_stage2, extract_hints
from scripts.assess_worker import check_prefilter


REPORT_JSONL = Path("reports/control_run.jsonl")
REPORT_MD = Path("reports/control_run_report.md")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def write_record(record: dict) -> None:
    with REPORT_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


async def run_one(analyzer, settings, row: dict) -> dict:
    vid = row["id"]
    title = row["title"] or ""
    desc = row["description"] or ""

    rec = {
        "id": vid,
        "title": title[:80],
        "discovered_at": row["discovered_at"],
        "experience_id": row.get("experience_id_from_json") or "",
        "stage": "",
        "action": "",
        "reason": "",
        "suitable": None,
        "best_resume": None,
        "confidence": None,
        "hard_filter_triggered": None,
        "hard_filter_reason": "",
        "overqualified": None,
        "overqualified_reason": "",
        "employment_type_special": False,
        "stage1_reason": "",
        "stage1_best_resume": None,
        "verdict": "",
    }

    # 1. Prefilter (excluded_keywords + employment flag)
    prefilter_reject, et_special, et_note = check_prefilter(desc, settings)
    rec["employment_type_special"] = et_special
    if prefilter_reject:
        rec["stage"] = "prefilter"
        rec["action"] = "reject"
        rec["reason"] = prefilter_reject
        return rec

    # 2. Stage 1
    summaries = {k: v.summary for k, v in settings.profile.resumes.items()}
    hints = extract_hints(title, desc)
    try:
        res1 = await assess_stage1(
            analyzer=analyzer, title=title, description=desc,
            summaries=summaries, hints=hints, vacancy_id=vid,
        )
    except AnalysisError as e:
        rec["stage"] = "stage1"
        rec["action"] = "error"
        rec["reason"] = f"stage1 {e.error_type}"
        return rec

    rec["stage1_reason"] = res1.reason or ""
    rec["stage1_best_resume"] = res1.best_resume
    if not res1.suitable:
        rec["stage"] = "stage1"
        rec["action"] = "reject"
        rec["reason"] = (res1.reason or "")[:200]
        return rec

    # 3. Stage 2
    best = res1.best_resume or "RP"
    if best not in settings.profile.resumes:
        rec["stage"] = "stage1"
        rec["action"] = "error"
        rec["reason"] = f"resume_not_found: {best}"
        return rec

    resume_text = settings.profile.resumes[best].full_text
    alternatives = {
        k: {"title": v.title, "summary_short": v.summary[:200]}
        for k, v in settings.profile.resumes.items() if k != best
    }
    try:
        res2 = await assess_stage2(
            analyzer=analyzer, title=title, description=desc,
            best_resume=best, resume_text=resume_text,
            alternatives=alternatives, vacancy_id=vid,
        )
    except AnalysisError as e:
        rec["stage"] = "stage2"
        rec["action"] = "error"
        rec["reason"] = f"stage2 {e.error_type}"
        return rec

    rec["stage"] = "stage2"
    rec["suitable"] = res2.suitable
    rec["best_resume"] = res2.best_resume or best
    rec["confidence"] = res2.confidence
    rec["hard_filter_triggered"] = res2.hard_filter_triggered
    rec["hard_filter_reason"] = res2.hard_filter_reason
    rec["overqualified"] = res2.overqualified
    rec["overqualified_reason"] = res2.overqualified_reason
    rec["verdict"] = res2.verdict[:250]
    rec["action"] = "accept" if res2.suitable else "reject"
    if not res2.suitable and res2.hard_filter_triggered:
        rec["reason"] = f"hard_filter: {res2.hard_filter_reason[:150]}"
    return rec


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--dates", default="",
                    help="CSV дат (YYYY-MM-DD), фильтр discovered_at. Пусто = все с description")
    ap.add_argument("--include-no-desc", action="store_true",
                    help="Включить discovered без description (НЕ рекомендуется)")
    args = ap.parse_args()

    settings = load_settings()
    db = Database(str(settings.database_path))
    db.init()

    where = ["status='discovered'"]
    if not args.include_no_desc:
        where.append("description != ''")
    params: list = []
    if args.dates:
        dates = [d.strip() for d in args.dates.split(",") if d.strip()]
        ph = ",".join("?" * len(dates))
        where.append(f"substr(discovered_at,1,10) IN ({ph})")
        params.extend(dates)
    sql = f"""
        SELECT id, title, description, discovered_at, evaluation_json
        FROM vacancies
        WHERE {' AND '.join(where)}
        ORDER BY discovered_at DESC, id DESC
        LIMIT ?
    """
    params.append(args.limit)
    with db._connect() as c:
        rows = [dict(r) for r in c.execute(sql, params).fetchall()]

    if not rows:
        log("Нет вакансий для контрольного прогона.")
        return

    # Извлечь experience_id из evaluation_json
    for r in rows:
        try:
            ej = json.loads(r["evaluation_json"]) if r["evaluation_json"] else {}
        except (json.JSONDecodeError, TypeError):
            ej = {}
        r["experience_id_from_json"] = ej.get("experience_id", "")

    log(f"CONTROL RUN: {len(rows)} вакансий, dates={args.dates or 'all'}")
    log(f"  с description: {sum(1 for r in rows if r['description'])}/{len(rows)}")

    # LLM provider (AITUNNEL, как в assess_worker)
    api_key = (settings.llm.aitunnel_api_key or "").strip()
    base_url = (settings.llm.aitunnel_base_url or "https://api.aitunnel.ru/v1").strip()
    if not api_key:
        log("FATAL: AITUNNEL_API_KEY пуст")
        return
    adapter = OpenAICompatibleProvider(base_url, api_key, json_mode=False, reasoning_enabled=False)
    provider = ManagedLLMProvider(adapter, db, max_retries=1, max_requests_per_day=2000)
    analyzer = VacancyAnalyzer(settings, provider)

    REPORT_JSONL.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSONL.write_text("", encoding="utf-8")

    results: list[dict] = []
    for idx, row in enumerate(rows, 1):
        log(f"[{idx}/{len(rows)}] {row['id']} {row['title'][:50]}...")
        rec = await run_one(analyzer, settings, row)
        write_record(rec)
        results.append(rec)
        log(f"  stage={rec['stage']} action={rec['action']} "
            f"suitable={rec['suitable']} oq={rec['overqualified']} "
            f"reason={(rec['reason'] or '')[:80]}")

    # --- Метрики ---
    total = len(results)
    stage0_reject = sum(1 for r in results if r["stage"] == "prefilter" and r["action"] == "reject")
    stage1_reject = sum(1 for r in results if r["stage"] == "stage1" and r["action"] == "reject")
    stage2_accept = sum(1 for r in results if r["stage"] == "stage2" and r["action"] == "accept")
    stage2_reject = sum(1 for r in results if r["stage"] == "stage2" and r["action"] == "reject")
    errors = sum(1 for r in results if r["action"] == "error")

    reached_stage2 = stage2_accept + stage2_reject
    rejected_total = stage0_reject + stage1_reject + stage2_reject
    pct_true_total = stage2_accept / total * 100 if total else 0
    pct_true_from_stage2 = stage2_accept / reached_stage2 * 100 if reached_stage2 else 0
    pct_reject_total = rejected_total / total * 100 if total else 0

    oq_true = sum(1 for r in results if r["stage"] == "stage2" and r["action"] == "accept" and r["overqualified"])
    pct_oq = oq_true / stage2_accept * 100 if stage2_accept else 0

    # Experience leaks (should be 0)
    exp_leaks = [r for r in results if r["experience_id"] and r["experience_id"] not in settings.profile.prefilter.experience_allowed]
    # Junior/salary leaks — проверяем title/salary
    from hh_client import _is_junior_title
    junior_leaks = [r for r in results if _is_junior_title(r["title"], settings.profile.prefilter.junior_title_markers)]

    # Hard filter / verdict breakdown
    hard_filter = sum(1 for r in results if r["stage"] == "stage2" and r["action"] == "reject" and r["hard_filter_triggered"])
    verdict_filter = sum(1 for r in results if r["stage"] == "stage2" and r["action"] == "reject" and not r["hard_filter_triggered"])

    # --- Markdown отчёт ---
    lines = [
        f"# Control run — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"**Выборка:** {total} вакансий (status=discovered, с description)",
        f"**Даты:** {args.dates or 'все'}",
        "",
        "## Воронка",
        f"| Стадия | Кол-во | % |",
        f"|---|---|---|",
        f"| Prefilter reject (Stage 0) | {stage0_reject} | {stage0_reject/total*100:.1f}% |",
        f"| Stage 1 reject | {stage1_reject} | {stage1_reject/total*100:.1f}% |",
        f"| Stage 2 reached | {reached_stage2} | {reached_stage2/total*100:.1f}% |",
        f"| → suitable=true | {stage2_accept} | {stage2_accept/total*100:.1f}% |",
        f"| → suitable=false | {stage2_reject} | {stage2_reject/total*100:.1f}% |",
        f"| → из них hard_filter | {hard_filter} | — |",
        f"| → из них verdict | {verdict_filter} | — |",
        f"| errors | {errors} | — |",
        "",
        "## Ключевые метрики (ожидания: % true 5-20%, reject ≥85%)",
        f"- **% true (от всех):** {pct_true_total:.1f}%",
        f"- **% true (от дошедших до Stage 2):** {pct_true_from_stage2:.1f}%",
        f"- **% reject (от всех):** {pct_reject_total:.1f}%",
        f"- **overqualified при suitable=true:** {oq_true}/{stage2_accept} = {pct_oq:.1f}% (порог мягкости: >40%)",
        "",
        "## Leaks (должно быть 0)",
        f"- **experience leaks** (experience_id не в allowed): {len(exp_leaks)}",
    ]
    if exp_leaks:
        for r in exp_leaks[:10]:
            lines.append(f"  - {r['id']} exp={r['experience_id']}")
    lines.append(f"- **junior title leaks**: {len(junior_leaks)}")
    if junior_leaks:
        for r in junior_leaks[:10]:
            lines.append(f"  - {r['id']} {r['title'][:60]}")

    lines += ["", "## Детали Stage 2 reject"]
    for r in results:
        if r["stage"] == "stage2" and r["action"] == "reject":
            lines.append(f"- `{r['id']}` **{r['title'][:60]}** — {r['reason'][:120]}")

    lines += ["", "## Suitable=true (для аудита)"]
    for r in results:
        if r["stage"] == "stage2" and r["action"] == "accept":
            oq = f" ⚠️ OQ: {r['overqualified_reason'][:80]}" if r["overqualified"] else ""
            lines.append(f"- `{r['id']}` **{r['title'][:60]}** conf={r['confidence']:.2f} resume={r['best_resume']}{oq}")

    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    log("")
    log("=== ИТОГ ===")
    log(f"  Всего: {total}")
    log(f"  Prefilter reject: {stage0_reject} ({stage0_reject/total*100:.1f}%)")
    log(f"  Stage 1 reject: {stage1_reject} ({stage1_reject/total*100:.1f}%)")
    log(f"  Stage 2 accept: {stage2_accept} ({pct_true_total:.1f}%)")
    log(f"  Stage 2 reject: {stage2_reject} ({stage2_reject/total*100:.1f}%)")
    log(f"  % true от всех: {pct_true_total:.1f}% (норма 5-20%)")
    log(f"  % true от Stage 2: {pct_true_from_stage2:.1f}%")
    log(f"  % reject от всех: {pct_reject_total:.1f}% (ожидание ≥85%)")
    log(f"  overqualified при suitable=true: {oq_true}/{stage2_accept} ({pct_oq:.1f}%)")
    log(f"  errors: {errors}")
    log(f"  experience leaks: {len(exp_leaks)}, junior leaks: {len(junior_leaks)}")
    log(f"  Отчёты: {REPORT_JSONL} и {REPORT_MD}")

    # Закрыть aiohttp-сессию провайдера (устраняет Unclosed client session)
    try:
        adapter_obj = getattr(provider, "adapter", None) or provider
        close_method = getattr(adapter_obj, "close", None)
        if close_method is not None:
            result = close_method()
            if hasattr(result, "__await__"):
                await result
            log("  provider closed")
    except Exception as e:
        log(f"  WARN: provider close failed: {e}")


if __name__ == "__main__":
    asyncio.run(main())
