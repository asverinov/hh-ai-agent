import asyncio
from pathlib import Path
from config import load_settings
from ai_analyzer import VacancyAnalyzer
from database import Database
from llm.providers.openai_compatible import OpenAICompatibleProvider
from llm.managed import ManagedLLMProvider

async def main():
    print("⏳ Загрузка настроек...")
    settings = load_settings()
    db = Database(str(settings.database_path))
    db.init()

    # Создаем AITUNNEL провайдер ЯВНО (как в main.py)
    api_key = (settings.llm.aitunnel_api_key or "").strip()
    base_url = (settings.llm.aitunnel_base_url or "https://api.aitunnel.ru/v1").strip()
    if not api_key:
        print("❌ AITUNNEL_API_KEY не задан в .env")
        return
    print(f"✅ AITUNNEL подключен: {base_url}")

    adapter = OpenAICompatibleProvider(base_url, api_key, json_mode=False, reasoning_enabled=None)
    provider = ManagedLLMProvider(adapter, db, max_retries=1, max_requests_per_day=1000)
    analyzer = VacancyAnalyzer(settings, provider)

    # Тестовая вакансия
    title = "Менеджер проектов по внедрению в Контур.Налоговый мониторинг"
    desc = """Контур.Налоговый мониторинг — сервис для онлайн-взаимодействия организаций с ФНС.
    Ищем менеджера проектов, который будет отвечать за внедрение сервисов отчётности в IT-инфраструктуру клиента.
    Опыт руководства IT-проектами. Практический опыт во внедрении, системном анализе, проектировании архитектуры.
    Навыки работы с данными: Excel, SQL, при необходимости — Python.
    Опыт переговоров с крупными корпоративными клиентами."""

    print("\n=== 1. Проверка дайджестов ===")
    for p in ["docs/datasets/ds_context_digest_v3.md", "docs/datasets/ds_context_digest.md"]:
        f = Path(p)
        if f.exists():
            print(f"  {p}: {f.stat().st_size} байт ({len(f.read_text())} симв)")
        else:
            print(f"  {p}: НЕ НАЙДЕН")

    print("\n=== 2. Stage 0 ===")
    candidates = analyzer.select_resume_stage0(title, desc)
    print(f"  Кандидаты: {candidates}")

    print("\n=== 3. Stage 1 (LLM) ===")
    if len(candidates) > 1:
        best_resume, reason = await analyzer.select_resume_stage1(title, desc, candidates)
    else:
        best_resume = candidates[0]
        reason = "Stage 0 heuristic (только 1 кандидат)"
    print(f"  Выбрано: {best_resume}")
    print(f"  Причина: {reason}")

    resume_text = settings.profile.resumes.get(best_resume).full_text
    print(f"  Длина full_text: {len(resume_text)} симв")

    print("\n=== 4. Stage 2 (Оценка) ===")
    # Используем сжатый дайджест v3 (новый)
    digest_path = Path("docs/datasets/ds_context_digest_v3.md")
    digest = digest_path.read_text(encoding="utf-8") if digest_path.exists() else None
    print(f"  Дайджест: {'v3 (' + str(len(digest)) + ' симв)' if digest else 'НЕ НАЙДЕН'}")

    try:
        suitability = await analyzer.assess(
            title, desc,
            vacancy_id="test_cascade_001",
            model_override=settings.llm.assess_model,
            session_id="test-cascade-001",
            system_override=digest,
            best_resume=best_resume,
            resume_text=resume_text,
        )
        print("\n=== РЕЗУЛЬТАТ ===")
        print(f"  suitable: {suitability.suitable}")
        print(f"  confidence: {suitability.confidence}")
        print(f"  best_resume (LLM): {suitability.best_resume}")
        print(f"  verdict: {suitability.verdict[:300]}")
        print(f"  requirements: {len(suitability.requirements)}")
        for r in suitability.requirements[:3]:
            print(f"    L{r.level} [{r.match:.2f}] {r.text[:60]}")
    except Exception as e:
        print(f"  ❌ Ошибка LLM: {type(e).__name__}: {e}")

if __name__ == "__main__":
    asyncio.run(main())
