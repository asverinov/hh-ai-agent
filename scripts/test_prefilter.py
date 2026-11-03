"""
test_prefilter.py — диагностика pre-filter на живом ответе API hh.ru.

Делает 1 запрос similar_vacancies для одного резюме, логирует ВСЕ items
с experience.id (без фильтрации), показывает: сколько отсеклось бы на
каждом шаге pre-filter. Не пишет в БД, не вызывает LLM.

Запуск:
  PYTHONPATH=. python3 scripts/test_prefilter.py --resume RP --pages 1
  PYTHONPATH=. python3 scripts/test_prefilter.py --all --pages 1
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from datetime import datetime, timezone

from config import load_settings
from database import Database
from hh_client import HHClient, _is_junior_title, _parse_salary, _normalize_experience
from vacancy_filter import title_rejection_reason


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


async def scan_resume(hh, db, code: str, max_pages: int) -> dict:
    rid = hh._get_resume_id(code)
    if not rid:
        log(f"SKIP {code}: нет ID в config/resumes.yaml")
        return {"code": code, "total": 0, "pages": 0}

    pf = hh.settings.profile.prefilter
    allowed_exp = set(pf.experience_allowed)
    junior_markers = pf.junior_title_markers
    salary_min = pf.salary_hard_min
    excluded_positions = pf.excluded_positions
    excluded_companies = pf.excluded_companies

    log(f"=== {code} (rid={rid}) ===")
    log(f"  allowed_exp = {sorted(allowed_exp)}")
    log(f"  salary_hard_min = {salary_min}")
    log(f"  junior_markers = {len(junior_markers)} шт")
    log(f"  excluded_positions = {len(excluded_positions)} шт, excluded_companies = {len(excluded_companies)} шт")

    exp_counter: Counter = Counter()
    skip_exp = 0
    skip_junior = 0
    skip_salary = 0
    skip_position = 0
    skip_company = 0
    passed = 0
    total = 0

    for page in range(max_pages):
        try:
            payload = await hh.fetch_similar_page(rid, page, per_page=100)
        except Exception as e:
            log(f"  FAIL page={page}: {type(e).__name__}: {str(e)[:150]}")
            break
        items = payload.get("items") or []
        pages = payload.get("pages", 0)
        found = payload.get("found", 0)
        log(f"  page={page}/{max(pages-1, 0)}: items={len(items)} found={found}")

        for item in items:
            total += 1
            vid = str(item.get("id", "")).strip()
            title = str(item.get("name", ""))
            company = str((item.get("employer") or {}).get("name", ""))
            exp_id = (item.get("experience") or {}).get("id") or "None"
            exp_counter[exp_id] += 1

            # 0. experience (defense-in-depth)
            if allowed_exp and exp_id != "None" and exp_id not in allowed_exp:
                skip_exp += 1
                continue

            # 1. junior
            if _is_junior_title(title, junior_markers):
                skip_junior += 1
                continue

            # 2. salary
            s_from, s_to, s_cur = _parse_salary(item)
            peek = s_to if s_to is not None else s_from
            if peek is not None and peek < salary_min:
                skip_salary += 1
                continue

            # 3. excluded positions
            if title_rejection_reason(title, excluded_positions):
                skip_position += 1
                continue

            # 4. excluded companies
            if any(term.casefold() in company.casefold() for term in excluded_companies):
                skip_company += 1
                continue

            passed += 1

        if pages == 0 or page >= pages - 1:
            break

    log(f"  ── ИТОГ {code}: total={total} passed={passed}")
    log(f"     SKIP experience={skip_exp}, junior={skip_junior}, salary={skip_salary}, position={skip_position}, company={skip_company}")
    log(f"     experience breakdown: {dict(exp_counter)}")
    return {
        "code": code, "total": total, "passed": passed,
        "skip_exp": skip_exp, "skip_junior": skip_junior,
        "skip_salary": skip_salary, "skip_position": skip_position,
        "skip_company": skip_company, "exp_counter": dict(exp_counter),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume", default="RP", help="Код резюме (RP/RSP/RPO/ROP)")
    ap.add_argument("--all", action="store_true", help="Все 4 резюме")
    ap.add_argument("--pages", type=int, default=1, help="Сколько страниц (по 100 items)")
    args = ap.parse_args()

    s = load_settings()
    db = Database(str(s.database_path))  # нужен только для HHClient, ничего не пишет
    db.init()

    hh = HHClient(None, s, db, approval_guard=None)  # type: ignore[arg-type]
    codes = ["RP", "RSP", "RPO", "ROP"] if args.all else [args.resume]
    results = []
    try:
        for code in codes:
            r = await scan_resume(hh, db, code, args.pages)
            results.append(r)
    finally:
        try:
            await hh.close()
        except Exception:
            pass

    log("")
    log("=== СВОДКА ===")
    log(f"  {'code':6s} {'total':>6s} {'passed':>7s} {'exp':>5s} {'junior':>7s} {'salary':>7s} {'pos':>5s} {'comp':>5s}")
    for r in results:
        log(f"  {r['code']:6s} {r['total']:>6d} {r['passed']:>7d} {r['skip_exp']:>5d} {r['skip_junior']:>7d} {r['skip_salary']:>7d} {r['skip_position']:>5d} {r['skip_company']:>5d}")

    # Диагноз
    log("")
    log("=== ДИАГНОЗ ===")
    for r in results:
        if r["total"] == 0:
            log(f"  {r['code']}: нет данных (рид не найден или API вернул 0)")
            continue
        exp_dist = r["exp_counter"]
        allowed = set(s.profile.prefilter.experience_allowed)
        leaked = {k: v for k, v in exp_dist.items() if k not in allowed and k != "None"}
        if leaked:
            log(f"  ⚠️  {r['code']}: API вернул НЕразрешённые experience.id: {leaked} (локальная страховка их отсеет)")
        else:
            log(f"  ✅ {r['code']}: API вернул только разрешённые experience.id (или None). Локальная страховка = 0 отсечений по опыту.")
        if r["skip_exp"] > 0:
            log(f"     ⚠️  {r['code']}: локальная страховка отсекла {r['skip_exp']} вакансий по опыту")


if __name__ == "__main__":
    asyncio.run(main())
