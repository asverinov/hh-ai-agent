"""
reconcile_vacancies.py — сверка БД с similar_vacancies (API).

Одна задача, две функции:
  1. Backfill experience: для discovered, которые ЕСТЬ в выдаче — записать experience_id.
     Если experience не в allowed -> rejected_by_filter (reason="experience: X").
  2. Reconcile: для discovered, которых НЕТ в выдаче — статус 'irrelevant'
     (hh больше не рекомендует; НЕ expired — не путать).

Резюме (4) обходятся последовательно. Первое, которое увидело вакансию — фиксируется.
Rate limit: SLEEP_BETWEEN=2 сек между страницами (проверено: 68 страниц ~2.5 мин без 429).

Запуск:
  PYTHONPATH=. python3 -u scripts/reconcile_vacancies.py --dry-run           # все 4, без записи
  PYTHONPATH=. python3 -u scripts/reconcile_vacancies.py --dry-run --resume RP
  PYTHONPATH=. python3 -u scripts/reconcile_vacancies.py                     # боевой

Двухфазный:
  Phase 1 — collect: обход API, дамп в reports/reconcile_seen.json.
  Phase 2 — apply:   чтение JSON + UPDATE БД.

JSONL + Markdown отчёт: reports/reconcile_results.jsonl, reports/reconcile_report.md.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from config import load_settings
from database import Database, VacancyStatus
from hh_client import HHClient


SLEEP_BETWEEN = 2.0
REPORT_JSONL = Path("reports/reconcile_results.jsonl")
REPORT_MD = Path("reports/reconcile_report.md")
SEEN_JSON = Path("reports/reconcile_seen.json")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def merge_evaluation_json(existing: str, new_fields: dict) -> str:
    try:
        data = json.loads(existing) if existing else {}
    except (json.JSONDecodeError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.update(new_fields)
    return json.dumps(data, ensure_ascii=False)


async def collect_phase(hh: HHClient, codes: list[str], sleep_between: float) -> dict:
    """Phase 1: обход similar_vacancies для резюме. Возвращает {vid: {...}}."""
    seen: dict[str, dict] = {}
    for code in codes:
        rid = hh._get_resume_id(code)
        if not rid:
            log(f"  SKIP {code}: нет ID")
            continue
        page = 0
        while True:
            try:
                payload = await hh.fetch_similar_page(rid, page, per_page=100)
            except Exception as e:
                log(f"  FAIL {code} page={page}: {type(e).__name__}: {str(e)[:150]}")
                break
            items = payload.get("items") or []
            if not items:
                break
            new_here = 0
            for item in items:
                vid = str(item.get("id", "")).strip()
                if not vid:
                    continue
                if vid in seen:
                    continue
                exp_id = (item.get("experience") or {}).get("id")
                seen[vid] = {
                    "experience_id": str(exp_id) if exp_id else None,
                    "resume_code": code,
                }
                new_here += 1
            pages = payload.get("pages", 0)
            log(f"  {code} page={page}/{max(pages-1,0)}: items={len(items)} new={new_here} total_seen={len(seen)}")
            if pages == 0 or page >= pages - 1:
                break
            page += 1
            await asyncio.sleep(sleep_between)
    return seen


def apply_phase(db: Database, seen: dict, settings, dry_run: bool) -> dict:
    """Phase 2: reconcile discovered по seen. Возвращает stats."""
    allowed_exp = set(settings.profile.prefilter.experience_allowed)
    with db._connect() as c:
        rows = c.execute(
            "SELECT id, title, status, evaluation_json FROM vacancies WHERE status=?",
            (VacancyStatus.DISCOVERED.value,),
        ).fetchall()

    log(f"  Phase 2: discovered в БД = {len(rows)}, в API = {len(seen)}")

    stats = Counter()
    reject_exp = Counter()
    et_flag = 0

    for idx, row in enumerate(rows, 1):
        vid = row["id"]
        existing = row["evaluation_json"] or ""
        s = seen.get(vid)

        if s is None:
            # not in API -> irrelevant
            record = {"id": vid, "action": "irrelevant",
                      "reason": "not_in_similar", "experience_id": None}
            if not dry_run:
                ok = db.mark_irrelevant(
                    vid, reason="not_in_similar",
                    evaluation_json=merge_evaluation_json(
                        existing, {"reconcile": "not_in_similar"}
                    ),
                )
                if not ok:
                    stats["errors"] += 1
                    record["action"] = "error"
            stats["irrelevant"] += 1
            _write(record)
            continue

        exp_id = s["experience_id"]
        record = {"id": vid, "action": "", "reason": "",
                  "experience_id": exp_id, "resume_code": s["resume_code"]}

        if exp_id and allowed_exp and exp_id not in allowed_exp:
            # in API but experience outside allowed -> reject_by_filter
            reason = f"experience: {exp_id}"
            record["action"] = "reject_exp"
            record["reason"] = reason
            if not dry_run:
                ok = db.reject_by_filter(
                    vid, reason=reason,
                    evaluation_json=merge_evaluation_json(
                        existing,
                        {"experience_id": exp_id, "reconcile": "reject_experience"},
                    ),
                )
                if not ok:
                    stats["errors"] += 1
                    record["action"] = "error"
            stats["reject_exp"] += 1
            reject_exp[exp_id] += 1
        else:
            # in API, experience OK or unknown -> backfill experience_id
            record["action"] = "backfill"
            if not dry_run:
                ok = db.set_evaluation_json(
                    vid,
                    merge_evaluation_json(
                        existing,
                        {
                            "experience_id": exp_id,
                            "reconcile": "backfill",
                            "reconciled_at": datetime.now(timezone.utc).isoformat(),
                        },
                    ),
                )
                if not ok:
                    stats["errors"] += 1
                    record["action"] = "error"
            stats["backfill"] += 1

        _write(record)
        if idx % 100 == 0:
            log(f"  progress {idx}/{len(rows)}: {dict(stats)}")

    return {
        "stats": dict(stats),
        "reject_exp": dict(reject_exp),
        "et_flag": et_flag,
        "total_db": len(rows),
        "total_api": len(seen),
    }


def _write(record: dict) -> None:
    with REPORT_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _write_report(result: dict, dry_run: bool) -> None:
    st = result["stats"]
    lines = [
        f"# Reconcile report — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"**Dry run:** {dry_run}",
        f"**Discovered в БД:** {result['total_db']}",
        f"**В API (similar_vacancies):** {result['total_api']}",
        "",
        "## Действия",
        f"| Действие | Кол-во |",
        f"|---|---|",
        f"| backfill (есть в API, experience OK) | {st.get('backfill', 0)} |",
        f"| reject_exp (есть в API, experience не allowed) | {st.get('reject_exp', 0)} |",
        f"| irrelevant (нет в API) | {st.get('irrelevant', 0)} |",
        f"| errors | {st.get('errors', 0)} |",
        "",
        "## Reject по experience",
    ]
    if result["reject_exp"]:
        for k, v in sorted(result["reject_exp"].items()):
            lines.append(f"- `{k}`: {v}")
    else:
        lines.append("- (нет)")
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--resume", default="", help="Один код (RP/RSP/RPO/ROP), по умолчанию все 4")
    ap.add_argument("--sleep", type=float, default=SLEEP_BETWEEN)
    ap.add_argument("--skip-collect", action="store_true",
                    help="Использовать уже собранный reports/reconcile_seen.json")
    args = ap.parse_args()

    settings = load_settings()
    db = Database(str(settings.database_path))
    db.init()

    codes = [args.resume] if args.resume else ["RP", "RSP", "RPO", "ROP"]

    REPORT_JSONL.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSONL.write_text("", encoding="utf-8")

    if args.skip_collect:
        if not SEEN_JSON.exists():
            log(f"FATAL: {SEEN_JSON} не найден, --skip-collect невозможен")
            return
        seen = json.loads(SEEN_JSON.read_text(encoding="utf-8"))
        log(f"Phase 1 (skip): загружено {len(seen)} ID из {SEEN_JSON}")
    else:
        hh = HHClient(None, settings, db, approval_guard=None)  # type: ignore[arg-type]
        log(f"Phase 1: collect для {codes}, sleep={args.sleep}s")
        try:
            seen = await collect_phase(hh, codes, args.sleep)
        finally:
            try:
                await hh.close()
            except Exception:
                pass
        SEEN_JSON.write_text(
            json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log(f"Phase 1: собрано {len(seen)} уникальных ID, сохранено в {SEEN_JSON}")

    log(f"Phase 2: apply (dry_run={args.dry_run})")
    result = apply_phase(db, seen, settings, args.dry_run)

    _write_report(result, args.dry_run)
    log("")
    log(f"=== ИТОГ: {result['stats']} ===")
    log(f"  discovered в БД: {result['total_db']}, в API: {result['total_api']}")
    log(f"  reject_exp breakdown: {result['reject_exp']}")
    log(f"Отчёты: {REPORT_JSONL} и {REPORT_MD}")
    log(f"seen: {SEEN_JSON}")


if __name__ == "__main__":
    asyncio.run(main())
