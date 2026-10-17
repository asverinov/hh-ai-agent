#!/usr/bin/env python3
"""Экспорт калибровочных вакансий в слепом формате (без оценок LLM).

Использует calibration_set.json + БД. Формат блоков:
    @@@VACANCY_BEGIN@@@ <id>
    TITLE: ...
    COMPANY: ...
    URL: ...
    SALARY: ...
    --- DESCRIPTION ---
    <полный текст>
    @@@VACANCY_END@@@ <id>

Порядок: по vacancy_id (тот же, что в таблице калибровки).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

BEGIN = "@@@VACANCY_BEGIN@@@"
END = "@@@VACANCY_END@@@"


def main() -> int:
    cal_path = REPO / "docs/datasets/calibration_set.json"
    db_path = REPO / "agent.db"

    if not cal_path.exists():
        print(f"ERROR: не найден {cal_path}", file=sys.stderr)
        return 1

    cal = json.loads(cal_path.read_text(encoding="utf-8"))
    all_ids = [v["id"] for v in cal["positive"]] + [v["id"] for v in cal["negative"]]

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        placeholders = ", ".join("?" for _ in all_ids)
        rows = conn.execute(
            f"SELECT id, title, company, url, description, salary_from, salary_to, salary_currency "
            f"FROM vacancies WHERE id IN ({placeholders}) AND description != '' "
            f"ORDER BY id",
            all_ids,
        ).fetchall()

    if not rows:
        print("ERROR: нет вакансий с description", file=sys.stderr)
        return 2

    out = REPO / "docs/datasets" / f"calibration_blind_{datetime.now():%Y%m%d}.txt"

    parts: list[str] = []
    for r in rows:
        salary = ""
        if r["salary_from"] or r["salary_to"]:
            s_from = r["salary_from"] or "?"
            s_to = r["salary_to"] or "?"
            cur = r["salary_currency"] or ""
            salary = f"{s_from}–{s_to} {cur}".strip()
        block = "\n".join([
            f"{BEGIN} {r['id']}",
            f"TITLE: {r['title']}",
            f"COMPANY: {r['company'] or ''}",
            f"URL: {r['url']}",
            f"SALARY: {salary}",
            "",
            "--- DESCRIPTION ---",
            r["description"],
            "",
            f"{END} {r['id']}",
            "",
        ])
        parts.append(block)

    out.write_text("\n".join(parts), encoding="utf-8")

    size = out.stat().st_size
    print(f"OK: {out}")
    print(f"     вакансий: {len(rows)} (из {len(all_ids)} в калибровке)")
    print(f"     размер: {size} байт ({size/1024:.1f} КБ)")
    print()
    print("=== Порядок (для сверки) ===")
    for r in rows:
        print(f"  {r['id']}  {r['title'][:60]}")

    missing = set(all_ids) - {r["id"] for r in rows}
    if missing:
        print()
        print(f"WARN: без description (пропущены): {sorted(missing)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
