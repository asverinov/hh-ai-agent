import json
import sqlite3
from pathlib import Path
from config import load_settings

def main():
    db_path = str(load_settings().database_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT id, title, llm_decision, llm_reason, evaluation_json
        FROM vacancies 
        WHERE status IN ('pending_approval', 'rejected_by_llm') AND llm_reason != ''
    """).fetchall()

    true_sample = []
    false_sample = []
    post_process_cases = []

    for r in rows:
        if not r['evaluation_json']: continue
        ej = json.loads(r['evaluation_json'])
        
        reason = ej.get('hard_filter_reason', '')
        if 'post-process: verdict' in reason:
            post_process_cases.append(r['id'])

        if r['llm_decision'] == 1 and len(true_sample) < 20:
            true_sample.append(r)
        elif r['llm_decision'] == 0 and 'is_required match=' in reason and len(false_sample) < 10:
            false_sample.append(r)

    out = Path("reports/migration_audit_for_expert.md")
    with out.open("w", encoding="utf-8") as f:
        f.write("# Аудит миграции (сэмплы)\n\n")
        
        f.write("## 1. Sample 20 suitable=true\n\n")
        for r in true_sample:
            ej = json.loads(r['evaluation_json'])
            f.write(f"### {r['id']} | {r['title']}\n")
            f.write(f"- best_resume: {ej.get('best_resume')}\n")
            f.write(f"- verdict: {ej.get('verdict')}\n")
            reqs = ej.get('requirements', [])
            top_reqs = [x for x in reqs if x.get('level') == 1][:3]
            f.write("- top-3 L1 reqs:\n")
            for tr in top_reqs:
                f.write(f"  - {tr.get('text')[:50]} -> {tr.get('match')}\n")
            f.write("\n")

        f.write("## 2. Sample 10 suitable=false (через is_required)\n\n")
        for r in false_sample:
            ej = json.loads(r['evaluation_json'])
            f.write(f"### {r['id']} | {r['title']}\n")
            f.write(f"- reason: {ej.get('hard_filter_reason')}\n")
            f.write(f"- verdict: {ej.get('verdict')}\n\n")

        f.write("## 3. Все случаи post-process: verdict\n\n")
        f.write(f"Всего: {len(post_process_cases)}\n\n")
        for pid in post_process_cases:
            f.write(f"- {pid}\n")

    print(f"✅ Аудит сохранен: {out}")

if __name__ == "__main__":
    main()
