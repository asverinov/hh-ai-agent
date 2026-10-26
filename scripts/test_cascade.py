import sys
from config import load_settings
from ai_analyzer import VacancyAnalyzer

def main():
    print("⏳ Загрузка настроек...")
    try:
        settings = load_settings()
    except Exception as e:
        print(f"❌ Ошибка загрузки настроек: {e}")
        sys.exit(1)
        
    print(f"✅ Профиль загружен. Кандидат: {settings.profile.candidate.name}")
    
    # 1. Проверка резюме
    resumes = settings.profile.resumes
    print(f"\n📚 Загружено резюме: {len(resumes)}")
    for code, r in resumes.items():
        text_len = len(r.full_text) if r.full_text else 0
        print(f"  - {code}: {r.title} (Summary: {len(r.summary)} симв, Full_text: {text_len} симв)")
        if text_len == 0:
            print(f"    ⚠️ ВНИМАНИЕ: Файл data/resumes/resume_{code}.txt пуст или не найден!")

    # 2. Проверка Stage 0 правил
    rules = settings.profile.stage0_rules
    print(f"\n⚙️ Загружено правил Stage 0: {len(rules)}")
    for i, rule in enumerate(rules):
        print(f"  - Правило {i+1}: keywords={rule.keywords} -> candidates={rule.candidates}")

    # 3. Тест эвристики Stage 0
    print("\n🧠 Тест Stage 0 (эвристика):")
    # Создаем анализатор с заглушкой провайдера (нам нужен только метод select_resume_stage0)
    analyzer = VacancyAnalyzer(settings, provider=None) 
    
    test_cases = [
        ("Руководитель поддержки", "Требуется опыт ITIL, SLA, управление инцидентами 2-й линии."),
        ("Delivery Manager", "Управление портфелем проектов, развитие PMO и методологии."),
        ("Senior PM", "Управление проектами внедрения ERP."),
        ("Неизвестная вакансия", "Просто текст без ключевых слов.")
    ]
    
    for title, desc in test_cases:
        candidates = analyzer.select_resume_stage0(title, desc)
        print(f"  - '{title}': {candidates}")

if __name__ == "__main__":
    main()
