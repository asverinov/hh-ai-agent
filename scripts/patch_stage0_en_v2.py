import yaml, shutil
from pathlib import Path
from datetime import datetime

p = Path("profile.yaml")
shutil.copy(p, str(p) + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M"))
data = yaml.safe_load(p.read_text(encoding="utf-8"))

# 1. Правило PM: только однозначные (project manager, delivery manager, project lead)
en_pm_only = ["project manager", "delivery manager", "project lead"]
for rule in data["stage0_rules"]:
    kw = rule.get("keywords", [])
    if "менеджер проектов" in kw:
        # Убираем спорные, если мой прошлый патч случайно применился
        for bad in ["program manager", "program lead", "engineering manager", "team lead", "tech lead"]:
            if bad in kw:
                kw.remove(bad)
        for w in en_pm_only:
            if w not in kw:
                kw.append(w)
        print(f"  ✅ PM-правило (только однозначные): {kw}")
        break

# 2. Program Manager → RP + RPO (спорно, портфель)
if not any("program manager" in r.get("keywords", []) for r in data["stage0_rules"]):
    data["stage0_rules"].insert(0, {
        "keywords": ["program manager", "program lead"],
        "candidates": ["RP", "RPO"],
    })
    print("  ✅ Добавлено: program manager -> RP + RPO")

# 3. Engineering Manager → RSP + RP (people manager + PM)
if not any("engineering manager" in r.get("keywords", []) for r in data["stage0_rules"]):
    data["stage0_rules"].insert(0, {
        "keywords": ["engineering manager", "team lead", "tech lead"],
        "candidates": ["RSP", "RP"],
    })
    print("  ✅ Добавлено: engineering manager -> RSP + RP")

# 4. Валидация
p.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False), encoding="utf-8")
yaml.safe_load(p.read_text(encoding="utf-8"))
print("  ✅ profile.yaml валиден")
