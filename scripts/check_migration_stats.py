#!/usr/bin/env python3
"""check_migration_stats.py — AI10: правильный знаменатель (533/488, не 588)."""
import json
import sqlite3
from pathlib import Path
from config import load_settings

JSONL = Path("reports/migration_results.jsonl")

def main():
    conn = sqlite3.connect(str(load_settings().database_path))
    conn.row_factory = sqlite3.Row
    stage1 = conn.execute("""
        SELECT COUNT(*) FROM vacancies
        WHERE status='rejected_by_llm' AND evaluation_json LIKE '%"stage": 1%'
    """).fetchone()[0]
    s2_true = s2_false = s2_hf = total_s2 = 0
    if JSONL.exists():
        for line in JSONL.open(encoding="utf-8"):
            line = line.strip()
            if not line: continue
            try: r = json.loads(line)
            except: continue
            s2 = r.get("stage2") or {}
            if s2.get("suitable") is None: continue
            total_s2 += 1
            if s2.get("suitable") is True: s2_true += 1
            elif s2.get("suitable") is False: s2_false += 1
            if s2.get("hard_filter_triggered") is True: s2_hf += 1
    total = stage1 + total_s2
    if total == 0:
        print("Миграция ещё не обработала ни одной вакансии."); return
    pct_s1 = stage1 / total * 100
    pct_true_all = s2_true / total * 100
    pct_true_s2 = s2_true / total_s2 * 100 if total_s2 else 0
    pct_hf_s2 = s2_hf / total_s2 * 100 if total_s2 else 0
    print(f"=== СТАТИСТИКА МИГРАЦИИ (обработано: {total}) ===")
    print(f"Stage 1 reject: {stage1} ({pct_s1:.1f}%)")
    print(f"Stage 2 reached: {total_s2}")
    print(f"1. % suitable=true (от Stage 2): {pct_true_s2:.1f}%  [{s2_true}/{total_s2}]")
    print(f"   % suitable=true (от всех):     {pct_true_all:.1f}%  [{s2_true}/{total}]")
    print(f"2. % hard_filter_triggered (от Stage 2): {pct_hf_s2:.1f}%  [{s2_hf}/{total_s2}]")
    print(f"3. suitable=false: {s2_false}")
    print()
    print("=== СТОП-СИГНАЛЫ ===")
    if total < 50:
        print(f"⏳ Обработано мало ({total}), рано делать выводы.")
    else:
        if pct_true_s2 < 10: print("🔴 СТОП: suitable=true < 10%")
        if pct_hf_s2 > 85:   print("🔴 СТОП: hard_filter > 85%")
        if pct_true_s2 > 60: print("🟡 ВНИМАНИЕ: suitable=true > 60% (мягко)")
    if not (total >= 50 and (pct_true_s2 < 10 or pct_hf_s2 > 85)):
        print("🟢 Пока всё идёт по плану.")

if __name__ == "__main__":
    main()
