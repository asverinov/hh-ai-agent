#!/usr/bin/env python3
"""migration_analysis.py — сводка по миграции AI9 (JSONL + DB)."""
import json, sqlite3
from collections import Counter
from pathlib import Path
from config import load_settings

JSONL = Path("reports/migration_results.jsonl")
OUT = Path("reports/migration_analysis_for_expert.md")

def main():
    if not JSONL.exists():
        print(f"НЕТ {JSONL}"); return
    records = []
    for line in JSONL.open(encoding="utf-8"):
        line = line.strip()
        if not line: continue
        try: records.append(json.loads(line))
        except: pass

    s1, s2, s2_hf, s2_reason = Counter(), Counter(), Counter(), Counter()
    ids = []
    for r in records:
        ids.append(str(r["id"]))
        st1 = r.get("stage1") or {}
        st2 = r.get("stage2") or {}
        s1[st1.get("suitable", "MISSING")] += 1
        s2[st2.get("suitable", "MISSING")] += 1
        s2_hf[st2.get("hard_filter_triggered", "MISSING")] += 1
        reason = (st2.get("hard_filter_reason") or "")[:70]
        s2_reason[reason] += 1

    conn = sqlite3.connect(str(load_settings().database_path))
    ph = ",".join(["?"] * len(ids))
    db_rows = conn.execute(f"""
        SELECT id, status, llm_decision FROM vacancies WHERE id IN ({ph})
    """, ids).fetchall()
    db_status = Counter(str(r[1]) for r in db_rows)
    db_decision = Counter((str(r[1]), r[2]) for r in db_rows)

    all_stage1 = conn.execute(
        "SELECT COUNT(*) FROM vacancies WHERE status='rejected_by_llm' AND evaluation_json LIKE '%\"stage\": 1%'"
    ).fetchone()[0]
    all_s2_reject = conn.execute(
        "SELECT COUNT(*) FROM vacancies WHERE status='rejected_by_llm' AND evaluation_json LIKE '%hard_filter_triggered%'"
    ).fetchone()[0]
    all_pending_hf = conn.execute(
        "SELECT COUNT(*) FROM vacancies WHERE status='pending_approval' AND evaluation_json LIKE '%hard_filter_triggered%' AND llm_decision=1"
    ).fetchone()[0]

    total_jsonl = len(records)
    stage2_reached_jsonl = sum(1 for r in records if (r.get("stage2") or {}).get("suitable") is not None)
    s2_true = s2.get(True, 0)
    s2_false = s2.get(False, 0)

    with OUT.open("w", encoding="utf-8") as f:
        f.write("# Анализ миграции AI9 — для Эксперта\n\n")
        f.write("## Источники\n\n")
        f.write(f"- `reports/migration_results.jsonl`: **{total_jsonl}** записей (все дошли до Stage 2)\n")
        f.write(f"- БД, `evaluation_json LIKE '%\"stage\": 1%'` (Stage 1 reject, в JSONL их нет): **{all_stage1}**\n")
        f.write(f"- БД, `rejected_by_llm` + `hard_filter_triggered` (Stage 2 reject): **{all_s2_reject}**\n")
        f.write(f"- БД, `pending_approval` + `hard_filter_triggered` + `llm_decision=1` (suitable): **{all_pending_hf}**\n\n")

        f.write("## Исходы (по JSONL — только Stage 2)\n\n")
        f.write(f"- stage2.suitable=true:  **{s2_true}**\n")
        f.write(f"- stage2.suitable=false: **{s2_false}**\n")
        f.write(f"- % true (от дошедших до Stage 2, JSONL): **{(s2_true/total_jsonl*100) if total_jsonl else 0:.1f}%**\n")
        f.write(f"- stage2.hard_filter_triggered: {dict(s2_hf)}\n\n")

        f.write("## Stage 1 / Stage 2 (по БД, все AI9-формата)\n\n")
        f.write(f"- Stage 1 reject: **{all_stage1}** ({all_stage1/(all_stage1+all_s2_reject+all_pending_hf)*100:.1f}% от 533)\n")
        f.write(f"- Stage 2 reject: **{all_s2_reject}**\n")
        f.write(f"- suitable (pending+hf): **{all_pending_hf}**\n")
        f.write(f"- Сумма: {all_stage1 + all_s2_reject + all_pending_hf} (ожидание: 533)\n\n")

        f.write("## Топ-15 hard_filter_reason (JSONL)\n\n")
        for r, c in s2_reason.most_common(15):
            f.write(f"- {c:>4}  `{r}`\n")
        f.write("\n")

        f.write("## Текущий статус в БД (для ID из JSONL)\n\n")
        for k, v in db_status.items():
            f.write(f"- {k}: {v}\n")
        f.write("\n")
        f.write("## (status, llm_decision)\n\n")
        for k, v in db_decision.items():
            f.write(f"- {k}: {v}\n")

    print(f"OK: {OUT}")
    print(f"JSONL total: {total_jsonl} (s2_true={s2_true}, s2_false={s2_false})")
    print(f"DB: stage1={all_stage1}, stage2_reject={all_s2_reject}, suitable={all_pending_hf} (sum={all_stage1+all_s2_reject+all_pending_hf})")
    print(f"JSONL→DB status: {dict(db_status)}")

if __name__ == "__main__":
    main()
