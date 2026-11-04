#!/usr/bin/env python3
"""audit_migration.py — сэмплы для Эксперта (JSONL + DB Stage 1)."""
import json, sqlite3
from pathlib import Path
from config import load_settings

JSONL = Path("reports/migration_results.jsonl")
OUT = Path("reports/migration_audit_for_expert.md")

def short_reqs(stage2, top=3):
    reqs = (stage2 or {}).get("requirements") or []
    lines = []
    for r in [x for x in reqs if x.get("level") == 1][:top]:
        lines.append(f"  - L1 `{(r.get('text') or '')[:70]}` -> match={r.get('match')} is_required={r.get('is_required')}")
    return "\n".join(lines)

def main():
    if not JSONL.exists():
        print(f"НЕТ {JSONL}"); return
    records = []
    for line in JSONL.open(encoding="utf-8"):
        line = line.strip()
        if not line: continue
        try: records.append(json.loads(line))
        except: pass

    true_sample = [r for r in records if (r.get("stage2") or {}).get("suitable") is True][:20]
    false_sample = [r for r in records if (r.get("stage2") or {}).get("suitable") is False][:10]
    post_cases = [r for r in records if "post-process" in ((r.get("stage2") or {}).get("hard_filter_reason") or "")]

    conn = sqlite3.connect(str(load_settings().database_path))
    conn.row_factory = sqlite3.Row
    stage1_rows = conn.execute("""
        SELECT id, title, llm_reason, evaluation_json FROM vacancies
        WHERE status='rejected_by_llm' AND evaluation_json LIKE '%"stage": 1%'
        ORDER BY id DESC LIMIT 10
    """).fetchall()

    with OUT.open("w", encoding="utf-8") as f:
        f.write("# Аудит миграции AI9 — сэмплы\n\n")
        f.write(f"- JSONL: **{len(records)}** записей\n")
        f.write(f"- true_sample: **{len(true_sample)}**\n")
        f.write(f"- false_sample: **{len(false_sample)}**\n")
        f.write(f"- post-process: **{len(post_cases)}**\n")
        f.write(f"- Stage 1 reject sample: **{len(stage1_rows)}**\n\n")

        f.write(f"## 1. suitable=true (20 из JSONL)\n\n")
        for r in true_sample:
            s2 = r.get("stage2") or {}
            f.write(f"### {r['id']} | {r.get('title','')}\n")
            f.write(f"- url: {r.get('url','')}\n")
            f.write(f"- best_resume: {s2.get('best_resume')}\n")
            f.write(f"- confidence: {s2.get('confidence')}\n")
            f.write(f"- verdict: {(s2.get('verdict') or '')[:300]}\n")
            f.write("- top-3 L1 reqs:\n")
            f.write(short_reqs(s2) + "\n\n")

        f.write(f"## 2. suitable=false с hard_filter (10 из JSONL)\n\n")
        for r in false_sample:
            s2 = r.get("stage2") or {}
            f.write(f"### {r['id']} | {r.get('title','')}\n")
            f.write(f"- url: {r.get('url','')}\n")
            f.write(f"- best_resume: {s2.get('best_resume')}\n")
            f.write(f"- hard_filter_reason: {s2.get('hard_filter_reason')}\n")
            f.write(f"- verdict: {(s2.get('verdict') or '')[:300]}\n")
            f.write("- top-3 L1 reqs:\n")
            f.write(short_reqs(s2) + "\n\n")

        f.write(f"## 3. Post-process cases (verdict)\n\n")
        f.write(f"Всего: **{len(post_cases)}**\n\n")
        for r in post_cases:
            s2 = r.get("stage2") or {}
            f.write(f"- {r['id']} | {r.get('title','')} | reason: {s2.get('hard_filter_reason')}\n")

        f.write(f"\n## 4. Stage 1 reject (10 из БД)\n\n")
        for row in stage1_rows:
            ej = json.loads(row["evaluation_json"]) if row["evaluation_json"] else {}
            f.write(f"### {row['id']} | {row['title']}\n")
            f.write(f"- llm_reason: {(row['llm_reason'] or '')[:300]}\n")
            f.write(f"- stage1.best_resume: {ej.get('best_resume')}\n")
            f.write(f"- stage1.confidence: {ej.get('confidence')}\n")
            f.write(f"- stage1.verdict: {(ej.get('verdict') or '')[:300]}\n\n")

    print(f"OK: {OUT}")
    print(f"true={len(true_sample)}, false={len(false_sample)}, post={len(post_cases)}, stage1={len(stage1_rows)}")

if __name__ == "__main__":
    main()
