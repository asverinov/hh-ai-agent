#!/usr/bin/env python3
"""prefilter_audit_pending.py — одноразовая чистка pending_approval.

Удаляет (status='irrelevant') вакансии, которые не должны были попасть в pending:
  - salary_from < 200000 (Пользователь: "ниже 200 - удаляем без сожаления")
  - exp_id in (between1And3, noExperience)
  - exp_id IS NULL и в description явно "1-3 года"
  - title содержит junior-маркер (junior/jr/trainee/intern/стажёр/интерн)

НЕ трогает rejected_by_llm / discovered / applied.

Запуск:
  PYTHONPATH=. python3 scripts/prefilter_audit_pending.py --dry-run
  PYTHONPATH=. python3 scripts/prefilter_audit_pending.py
"""
import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from config import load_settings

SALARY_MIN_RUR = 200_000
SALARY_MIN_USD = 3_000
BAD_EXP = {"between1And3", "noExperience"}
JUNIOR_MARKERS = ("junior", " jr", "jr ", "trainee", "intern",
                  "стажёр", "стажер", "интерн", "начинающий")
EXP_IN_DESC = re.compile(
    r"(1\s*[-–]\s*3\s*(год|лет)|от\s*1\s*(года|год)\s*до\s*3|1-3\s*года|опыт\s*от\s*1\s*года)",
    re.I,
)


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def reason_for(row: sqlite3.Row) -> str | None:
    title = (row["title"] or "").lower()
    desc = row["description"] or ""
    exp = row["experience_id"]

    _from = row["salary_from"]
    _to = row["salary_to"] if "salary_to" in row.keys() else None
    cur = (row["salary_currency"] or "").upper()

    if cur in ("", "RUR", "RUB"):
        threshold = SALARY_MIN_RUR
    elif cur in ("USD", "EUR"):
        threshold = SALARY_MIN_USD
    else:
        threshold = None

    if threshold is not None:
        if _to is not None and _to > 0:          # верхний предел задан: "до Y" / "от X до Y"
            if _to <= threshold:
                return f"salary_to_{_to}{cur or 'RUR'}_at_or_below_{threshold}"
        elif _from is not None and _from > 0:    # только "от X"
            if _from < threshold:
                return f"salary_from_{_from}{cur or 'RUR'}_below_{threshold}"
    if exp in BAD_EXP:
        return f"experience_bad: {exp}"
    if exp is None and EXP_IN_DESC.search(desc):
        return "experience_bad_in_description"
    if any(m in title for m in JUNIOR_MARKERS):
        return "junior_title"
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    settings = load_settings()
    conn = sqlite3.connect(str(settings.database_path))
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT id, title, salary_from, salary_to, salary_currency, description,
               json_extract(evaluation_json, '$.experience_id') AS experience_id,
               evaluation_json, llm_reason
        FROM vacancies
        WHERE status = 'pending_approval'
        ORDER BY id
    """).fetchall()

    print(f"pending_approval всего: {len(rows)}")
    to_clean: list[tuple[sqlite3.Row, str]] = []
    for r in rows:
        reason = reason_for(r)
        if reason:
            to_clean.append((r, reason))

    print(f"под чистку: {len(to_clean)}")
    by_reason = Counter(reason.split(":")[0] for _, reason in to_clean)
    for reason, n in by_reason.most_common():
        print(f"  {reason}: {n}")

    if args.dry_run:
        print("\n--- DRY RUN, первые 10 ---")
        for r, reason in to_clean[:10]:
            print(f"  {r['id']} | {r['title'][:50]} | {reason}")
        print(f"\n(dry-run: БД не изменена)")
        return

    ts = now_iso()
    for r, reason in to_clean:
        ej = r["evaluation_json"] or "{}"
        try:
            data = json.loads(ej)
        except (json.JSONDecodeError, TypeError):
            data = {}
        data["prefilter_audit"] = {"reason": reason, "at": ts}
        conn.execute(
            "UPDATE vacancies SET status='irrelevant', llm_reason=?, "
            "evaluation_json=?, queued_at=NULL WHERE id=? AND status='pending_approval'",
            (f"prefilter_audit: {reason}", json.dumps(data, ensure_ascii=False), r["id"]),
        )
    conn.commit()

    print(f"\nOK: {len(to_clean)} → irrelevant")
    print("--- контроль: pending_approval осталось ---")
    (n,) = conn.execute("SELECT COUNT(*) FROM vacancies WHERE status='pending_approval'").fetchone()
    print(f"  pending_approval: {n}")


if __name__ == "__main__":
    main()
