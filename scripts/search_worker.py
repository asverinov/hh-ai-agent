"""
search_worker: крутит similar_vacancies для 4 резюме, наполняет read_queue.
Не трогает CloakBrowser — только API (aiohttp).

Запуск:
  python3 scripts/search_worker.py --limit 1    # тест: 1 страница на резюме
  python3 scripts/search_worker.py               # полный цикл
  nohup env PYTHONPATH=. python3 scripts/search_worker.py > /tmp/search.log 2>&1 &
"""
import argparse
import asyncio
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

from config import load_settings
from database import Database
from hh_client import HHClient
from hh_client import _is_junior_title, _parse_salary
from vacancy_filter import title_rejection_reason

LOG = Path(f"search_worker_{datetime.now().strftime('%Y%m%d_%H%M')}.log")
RESUME_CODES = ["RP", "RSP", "RPO", "ROP"]
PER_PAGE = 100
SLEEP_BETWEEN_PAGES = (5, 10)   # сек между API-запросами
SLEEP_AFTER_CYCLE = 600           # 10 мин после полного обхода


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _salary_below_threshold(
    s_from: int | None, s_to: int | None, currency: str,
    hard_min_rur: int, hard_min_usd: int,
) -> bool:
    """True = зарплата ниже порога (отбросить).

    Логика (максимум важнее минимума):
      - есть "to" (вилка "от X до Y" / "до Y"): режем если Y <= threshold
      - только "from" ("от X"): режем если X < threshold
    RUR/пусто → RUR-порог, USD/EUR → USD-порог, прочие валюты — не режем.
    """
    cur = (currency or "").upper()
    if cur in ("", "RUR", "RUB"):
        threshold = hard_min_rur
    elif cur in ("USD", "EUR"):
        threshold = hard_min_usd
    else:
        return False  # KZT, BYR, etc. — не режем

    if s_to is not None and s_to > 0:      # верхний предел задан
        return s_to <= threshold
    if s_from is not None and s_from > 0:  # только "от X"
        return s_from < threshold
    return False


async def search_one_resume(hh, db, code: str, max_pages: int, seen_acc: dict) -> tuple[int, int]:
    """Возвращает (new_count, pages_scanned)."""
    rid = hh._get_resume_id(code)
    if not rid:
        log(f"SKIP {code}: нет ID в config/resumes.yaml")
        return 0, 0
    allowed_exp: set[str] = set(hh.settings.profile.prefilter.experience_allowed)
    page = 0
    new_total = 0
    while True:
        if max_pages and page >= max_pages:
            break
        try:
            result = await hh.fetch_similar_page(rid, page, per_page=PER_PAGE)
        except Exception as e:
            log(f"FAIL {code} page={page}: {type(e).__name__}: {str(e)[:150]}")
            break
        items = result.get("items") or []
        pages = result.get("pages", 0)
        found = result.get("found", 0)
        new_on_page = 0
        for item in items:
            vid = str(item.get("id", "")).strip()
            if not vid:
                continue
            seen_acc[vid] = {
                "experience_id": (item.get("experience") or {}).get("id"),
                "resume_code": code,
            }
            if db.is_seen(vid):
                continue
                
            # 0. Опыт (defense-in-depth: API может вернуть не то или измениться)
            item_exp = (item.get("experience") or {}).get("id")
            if allowed_exp and item_exp and item_exp not in allowed_exp:
                log(f"SKIP_EXPERIENCE {vid} exp={item_exp} allowed={sorted(allowed_exp)}")
                continue

            title = str(item.get("name", ""))
            company = str((item.get("employer") or {}).get("name", ""))
            
            # 1. Junior-тайтлы
            if marker := _is_junior_title(title, hh.settings.profile.prefilter.junior_title_markers):
                log(f"SKIP_JUNIOR {vid} marker={marker}")
                continue
                
            # 2. Зарплата (hard_min)
            s_from, s_to, s_cur = _parse_salary(item)
            _pf = hh.settings.profile.prefilter
            if _salary_below_threshold(
                s_from, s_to, s_cur,
                _pf.salary_hard_min, _pf.salary_hard_min_usd,
            ):
                log(f"SKIP_SALARY {vid} {s_from}..{s_to} {s_cur} (RUR<{_pf.salary_hard_min} / USD<{_pf.salary_hard_min_usd})")
                continue
                
            # 3. Исключенные позиции
            if reason := title_rejection_reason(title, hh.settings.profile.prefilter.excluded_positions):
                log(f"SKIP_POSITION {vid} {reason}")
                continue
                
            # 4. Исключенные компании
            if company_match := next((term for term in hh.settings.profile.prefilter.excluded_companies if term.casefold() in company.casefold()), None):
                log(f"SKIP_COMPANY {vid} {company_match}")
                continue
                
            try:
                db.discover(
                    job_id=vid,
                    title=str(item.get("name", "")),
                    company=str((item.get("employer") or {}).get("name", "")),
                    url=str(item.get("alternate_url", "")),
                    description_hash="",
                    search_query=f"similar_vacancies:{code}",
                    discovered_at=datetime.now(timezone.utc),
                    salary_from=(item.get("salary") or {}).get("from"),
                    salary_to=(item.get("salary") or {}).get("to"),
                    salary_currency=(item.get("salary") or {}).get("currency", ""),
                )
                db.mark_seen([vid])
                new_on_page += 1
            except Exception as e:
                log(f"DISCOVER_FAIL {vid}: {e}")
        new_total += new_on_page
        log(f"{code} page={page}/{max(pages-1,0)}: items={len(items)} new={new_on_page} found={found}")
        if pages == 0 or page >= pages - 1:
            break
        page += 1
        await asyncio.sleep(random.uniform(*SLEEP_BETWEEN_PAGES))
    return new_total, page + 1


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0=все страницы, N=макс N страниц на резюме")
    ap.add_argument("--once", action="store_true", help="один цикл и выход (без sleep 600)")
    ap.add_argument("--reconcile-dry-run", action="store_true",
                    help="Reconcile: только отчёт, БД не трогает")
    ap.add_argument("--dry-run", action="store_true",
                    help="Ничего не пишет в БД (discover + reconcile dry)")
    args = ap.parse_args()

    s = load_settings()
    db = Database(str(s.database_path))
    db.init()

    # CloakBrowser не нужен — context=None
    hh = HHClient(None, s, db, approval_guard=None)  # type: ignore[arg-type]
    try:
        while True:
            cycle_start = datetime.now()
            total_new = 0
            seen_acc: dict = {}
            for code in RESUME_CODES:
                new, pages = await search_one_resume(hh, db, code, args.limit, seen_acc)
                total_new += new
                log(f"ИТОГ {code}: new={new} pages={pages}")

            # Reconcile: сверка всех "активных" с API-выдачей
            if not args.dry_run:
                from reconcile import apply_reconcile
                allowed_exp = set(s.profile.prefilter.experience_allowed)
                rec = apply_reconcile(
                    db, seen_acc, allowed_exp,
                    dry_run=args.reconcile_dry_run,
                )
                log(f"RECONCILE{' (DRY)' if args.reconcile_dry_run else ''}: {rec['stats']}")
                for vid, from_st, action in rec["touched"][:15]:
                    log(f"  {vid}  {from_st} -> {action}")
                if len(rec["touched"]) > 15:
                    log(f"  ... ещё {len(rec['touched']) - 15}")
            else:
                log(f"DRY-RUN: reconcile пропущен, seen={len(seen_acc)} ID")

            elapsed = (datetime.now() - cycle_start).total_seconds()
            log(f"ЦИКЛ ЗАВЕРШЁН: new={total_new} seen={len(seen_acc)} за {elapsed:.0f} сек")
            if args.once:
                break
            log(f"Пауза {SLEEP_AFTER_CYCLE} сек до следующего цикла")
            await asyncio.sleep(SLEEP_AFTER_CYCLE)
    finally:
        try:
            await hh.close()
            log("HHClient закрыт")
        except Exception as e:
            log(f"close failed: {e}")


if __name__ == "__main__":
    asyncio.run(main())