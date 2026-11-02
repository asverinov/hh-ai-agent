import json
import sqlite3
from pathlib import Path
from config import load_settings

# Список ID из второго пилота
PILOT_IDS = [
    '137312361', '138841461', '137440301', '138881135', '137764804', 
    '126534185', '138785135', '138144751', '138144753', '138144750',
    '133311122', '138881236', '138625378', '138841163', '138389905'
]

def main():
    settings = load_settings()
    db_path = str(settings.database_path)
    
    # Сохраняем отчет прямо в папку reports/
    out_file = Path("reports/pilot_report_v2_for_expert.md")
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    with out_file.open("w", encoding="utf-8") as f:
        f.write("# ПИЛОТ v2: Отчет по 15 вакансиям\n\n")
        f.write("Проверка работы `validate_stage2_result` и обновленного `assess2.md`.\n\n---\n\n")
        
        for vid in PILOT_IDS:
            row = conn.execute("SELECT id, title, status, llm_reason, confidence, evaluation_json FROM vacancies WHERE id=?", (vid,)).fetchone()
            if not row: continue
                
            f.write(f"## {row['title']}\nID: {vid}\n\n")
            
            if row['status'] == 'rejected_by_llm' and (row['llm_reason'] or '').startswith("Stage 1:"):
                f.write(f"**Stage 1 (Qwen):** `suitable=False` (уверенность {row['confidence']})\n")
                f.write(f"- Причина: {row['llm_reason'].replace('Stage 1: ', '')}\n\n")
                f.write("**Stage 2:** Не запускался.\n\n")
            else:
                ej = json.loads(row['evaluation_json']) if row['evaluation_json'] else {}
                f.write(f"**Stage 1:** `suitable=True` -> пропущен в Stage 2\n\n")
                f.write(f"**Stage 2 (MiMo):**\n")
                
                hf_flag = "🔥 **HARD REJECT**" if ej.get('hard_filter_triggered') else ""
                f.write(f"- Вердикт: `suitable={ej.get('suitable', False)}` {hf_flag} (уверенность {row['confidence']})\n")
                if ej.get('hard_filter_reason'):
                    f.write(f"- Причина фильтра: `{ej['hard_filter_reason']}`\n")
                f.write(f"- Резюме: {row['llm_reason']}\n\n")
                
                f.write("**Требования (match):**\n")
                for req in ej.get("requirements", []):
                    match_pct = int(req.get('match', 0) * 100)
                    plus_flag = " (плюс)" if req.get('is_plus') else ""
                    f.write(f"- [L{req.get('level')}] {req.get('text')} — **{match_pct}%**{plus_flag}\n")
                f.write("\n")
                
            f.write("---\n\n")
            
    conn.close()
    print(f"✅ Отчет сохранен: {out_file}")
    print(f"   Размер: {out_file.stat().st_size} байт")

if __name__ == "__main__":
    main()
