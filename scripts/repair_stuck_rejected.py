#!/usr/bin/env python3
"""fix_stuck_rejected.py — доприменяет rejected_by_llm для застрявших discovered.

Баг: evaluate_discovered.py передавал строку "discovered" вместо VacancyStatus.DISCOVERED,
db.transition возвращал False. evaluations записались, статусы — нет.

Решение: evaluations за сутки с suitable=0 → transition с enum. Без LLM.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_settings
from database import Database, VacancyStatus


def main():
    dry_run = "--dry-run" in sys.argv[1:]

    settings = load_settings()
    db = Database(settings.database_path)

    with db._connect() as conn:
        rows = conn.execute("""
            SELECT e.vacancy_id, e.confidence, e.verdict, e.requirements_json
            FROM evaluations e
            JOIN vacancies v ON v.id = e.vacancy_id
            WHERE e.suitable = 0
              AND e.created_at > datetime('now', '-1 day')
              AND v.status = 'discovered'
        """).fetchall()

    print(f"Найдено застрявших: {len(rows)}")
    if dry_run:
        for row in rows[:10]:
            print(f"  {row[0]} conf={row[1]:.2f} verdict={row[2][:50]}")
        if len(rows) > 10:
            print(f"  ... (всего {len(rows)})")
        return

    ok = 0
    failed = 0
    for row in rows:
        vid, conf, verdict, eval_json = row
        try:
            success = db.transition(
                vid,
                VacancyStatus.DISCOVERED,
                VacancyStatus.REJECTED_BY_LLM,
                llm_decision=False,
                llm_reason=verdict or "",
                confidence=conf,
                evaluation_json=eval_json or "",
                error_text="",
            )
            if success:
                ok += 1
            else:
                failed += 1
                if failed <= 5:
                    print(f"  FAIL {vid}: transition returned False")
        except Exception as exc:
            failed += 1
            if failed <= 5:
                print(f"  FAIL {vid}: {type(exc).__name__}: {exc}")

    print()
    print("=== DONE ===")
    print(f"  ok:     {ok}")
    print(f"  failed: {failed}")


if __name__ == "__main__":
    main()
