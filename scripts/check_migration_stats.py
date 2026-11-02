import json
import sqlite3
from config import load_settings

def main():
    db_path = str(load_settings().database_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Фильтруем ТОЛЬКО новые результаты миграции:
    # у них в evaluation_json есть поле hard_filter_triggered (старый пайплайн его не писал)
    rows = conn.execute("""
        SELECT status, llm_decision, llm_reason, evaluation_json
        FROM vacancies 
        WHERE status IN ('pending_approval', 'rejected_by_llm')
          AND evaluation_json LIKE '%hard_filter_triggered%'
    """).fetchall()

    total = len(rows)
    if total == 0:
        print("Миграция еще не обработала ни одной вакансии (или нет новых evaluation_json).")
        return

    suitable_true = 0
    hard_filter_triggered = 0
    req_filter = 0
    verdict_filter = 0
    confidence_none = 0
    mismatch = 0
    stage1_rejects = 0

    for r in rows:
        if r['llm_decision'] == 1:
            suitable_true += 1
        
        # Stage 1 reject: есть llm_reason начинающийся с Stage 1, но нет evaluation_json с hard_filter
        reason_text = r['llm_reason'] or ''
        ej = json.loads(r['evaluation_json']) if r['evaluation_json'] else {}
        
        # Stage 1 rejects не имеют полного evaluation_json — они просто получили llm_reason
        if not ej.get('requirements'):
            stage1_rejects += 1
            continue
        
        if ej.get('hard_filter_triggered'):
            hard_filter_triggered += 1
            reason = ej.get('hard_filter_reason', '')
            if 'is_required match=' in reason:
                req_filter += 1
            elif 'post-process: verdict' in reason:
                verdict_filter += 1
        
        if ej.get('confidence') is None:
            confidence_none += 1
            
        verdict_lower = (ej.get('verdict') or '').lower()
        if ej.get('suitable') and 'не подаваться' in verdict_lower:
            mismatch += 1

    pct_true = (suitable_true / total) * 100
    pct_hf = (hard_filter_triggered / total) * 100
    pct_req = (req_filter / hard_filter_triggered * 100) if hard_filter_triggered else 0
    pct_stage1 = (stage1_rejects / total) * 100 if total else 0

    print(f"=== СТАТИСТИКА МИГРАЦИИ (обработано: {total}) ===")
    print(f"Stage 1 reject (не запускали Stage 2): {stage1_rejects} ({pct_stage1:.1f}%)")
    print(f"Дошли до Stage 2: {total - stage1_rejects}")
    print(f"1. % suitable=true (от всех): {pct_true:.1f}% (Ожидание: 15-25% от дошедших)")
    print(f"2. % hard_filter_triggered: {pct_hf:.1f}% (Ожидание: 60-75% от дошедших)")
    print(f"3. Доля is_required в фильтрах: {pct_req:.1f}% (Ожидание: ≥80%)")
    print(f"   - По is_required: {req_filter}")
    print(f"   - По вердикту (post-process): {verdict_filter}")
    print(f"4. confidence: None: {confidence_none} (Ожидание: 0)")
    print(f"5. suitable=true + 'не подаваться': {mismatch} (Ожидание: 0)")

    print("\n=== СТОП-СИГНАЛЫ ===")
    if total < 50:
        print(f"⏳ Обработано мало ({total}), рано делать выводы. Ждём ~100+.")
    else:
        if pct_true < 10: print("🔴 СТОП: suitable=true < 10% (фильтр слишком строгий)")
        if pct_hf > 85: print("🔴 СТОП: hard_filter > 85% (переусердствовали)")
        if verdict_filter > hard_filter_triggered * 0.5 and hard_filter_triggered > 0:
            print("🔴 СТОП: post-process по вердикту > 50% (v1.4 не работает)")
        if confidence_none > 0: print("🟡 ВНИМАНИЕ: есть confidence: None")
        if mismatch > 0: print("🔴 СТОП: suitable=true + 'не подаваться'")
    
    if not any([total >= 50 and pct_true < 10, total >= 50 and pct_hf > 85, mismatch > 0]):
        print("🟢 Пока всё идет по плану.")

if __name__ == "__main__":
    main()
