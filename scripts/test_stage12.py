import sys
import asyncio
from pathlib import Path
from config import load_settings
from ai_analyzer import VacancyAnalyzer
from database import Database
from llm.factory import create_llm_provider

async def main():
    print("⏳ Загрузка настроек и провайдера...")
    settings = load_settings()
    db = Database(str(settings.database_path))
    db.init()
    provider = create_llm_provider(settings, db)
    analyzer = VacancyAnalyzer(settings, provider)

    # Тестовая вакансия: Контур.Налоговый мониторинг (ID 138204825)
    title = "Менеджер проектов по внедрению в Контур.Налоговый мониторинг"
    desc = """Контур.Налоговый мониторинг — сервис для онлайн-взаимодействия организаций с ФНС.
    Ищем менеджера проектов, который будет отвечать за внедрение сервисов отчётности в IT-инфраструктуру клиента.
    Опыт руководства IT-проектами. Практический опыт во внедрении, системном анализе, проектировании архитектуры.
    Навыки работы с данными: Excel, сводные таблицы, SQL, при необходимости — Python.
    Опыт переговоров с крупными корпоративными клиентами."""

    print("\n1. Stage 0 (Эвристика)...")
    candidates = analyzer.select_resume_stage0(title, desc)
    print(f"   Кандидаты: {candidates}")

    if len(candidates) > 1:
        print("\n2. Stage 1 (LLM Выбор)...")
        best_resume, reason = await analyzer.select_resume_stage1(title, desc, candidates)
        print(f"   Выбрано: {best_resume}")
        print(f"   Обоснование: {reason}")
    else:
        best_resume = candidates[0]
        reason = "Stage 0 heuristic"
        print(f"\n2. Stage 1 пропущен. Выбрано: {best_resume}")

    resume_text = settings.profile.resumes.get(best_resume).full_text if best_resume in settings.profile.resumes else ""
    print(f"   Длина текста резюме: {len(resume_text)} симв.")

    print("\n3. Stage 2 (Оценка соответствия)...")
    digest_path = Path("docs/datasets/ds_context_digest_v3.md")
    if not digest_path.exists():
        digest_path = Path("docs/datasets/ds_context_digest.md")
    
    digest = digest_path.read_text(encoding="utf-8") if digest_path.exists() else ""
    print(f"   Дайджест загружен: {len(digest)} симв.")

    suitability = await analyzer.assess(
        title, desc, 
        vacancy_id="test_123", 
        model_override=settings.llm.assess_model,
        session_id="test_session_001",
        system_override=digest if digest else None,
        best_resume=best_resume,
        resume_text=resume_text
    )

    print("\n=== РЕЗУЛЬТАТ ОЦЕНКИ ===")
    print(f"Подходит: {suitability.suitable}")
    print(f"Уверенность: {suitability.confidence}")
    print(f"Вердикт: {suitability.verdict}")
    print(f"Требований: {len(suitability.requirements)}")
    for r in suitability.requirements[:3]:
        print(f"  - L{r.level} {r.label}: {r.text[:50]}... (match={r.match})")

if __name__ == "__main__":
    asyncio.run(main())
