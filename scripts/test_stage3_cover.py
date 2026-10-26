import asyncio
import re
from pathlib import Path
from config import load_settings
from ai_analyzer import VacancyAnalyzer
from database import Database
from llm.providers.openai_compatible import OpenAICompatibleProvider
from llm.managed import ManagedLLMProvider

FORBIDDEN = [
    "с уважением", "буду рад обсудить", "ваша компания",
    "450 млн", "28+15", "30→280", "30 -> 280",
    "tco/roi", "p&l", "unit-экономика", "маржинальность",
    "провёл аудиты", "провел аудиты", "владею prince2",
]

async def main():
    settings = load_settings()
    db = Database(str(settings.database_path))
    db.init()

    api_key = (settings.llm.aitunnel_api_key or "").strip()
    base_url = (settings.llm.aitunnel_base_url or "https://api.aitunnel.ru/v1").strip()
    adapter = OpenAICompatibleProvider(base_url, api_key, json_mode=False, reasoning_enabled=None)
    provider = ManagedLLMProvider(adapter, db, max_retries=1, max_requests_per_day=1000)
    analyzer = VacancyAnalyzer(settings, provider)

    title = "Менеджер проектов по внедрению в Контур.Налоговый мониторинг"
    desc = """Контур.Налоговый мониторинг — сервис для онлайн-взаимодействия организаций с ФНС.
    Ищем менеджера проектов, который будет отвечать за внедрение сервисов отчётности в IT-инфраструктуру клиента.
    Опыт руководства IT-проектами. Практический опыт во внедрении, системном анализе, проектировании архитектуры.
    Навыки работы с данными: Excel, SQL, при необходимости — Python.
    Опыт переговоров с крупными корпоративными клиентами."""

    # Stage 0 + 1 (уже проверяли, но для полноты)
    candidates = analyzer.select_resume_stage0(title, desc)
    if len(candidates) > 1:
        best_resume, reason = await analyzer.select_resume_stage1(title, desc, candidates)
    else:
        best_resume, reason = candidates[0], "Stage 0"
    print(f"Stage 0/1: best_resume={best_resume}")

    resume_text = settings.profile.resumes[best_resume].full_text

    # Загружаем промпт v4.1
    cl_digest_path = Path("docs/datasets/ds_cover_letter_digest.md")
    cl_digest = cl_digest_path.read_text(encoding="utf-8") if cl_digest_path.exists() else None
    print(f"Cover letter digest: {'v4.1 (' + str(len(cl_digest)) + ' симв)' if cl_digest else 'НЕ НАЙДЕН'}")

    print("\n=== Генерация письма (qwen3.7-flash + v4.1) ===")
    letter_new = await analyzer.generate_cover_letter(
        title, desc, company_name="Контур",
        vacancy_id="test_cover_001",
        model_override=settings.llm.cover_letter_model,  # qwen3.7-flash
        system_override=cl_digest,
        session_id="test-cover-001",
        best_resume=best_resume,
        resume_text=resume_text,
    )

    print(f"\n{'='*70}")
    print("НОВОЕ ПИСЬМО (каскад: best_resume + full_text + v4.1):")
    print(f"{'='*70}")
    print(letter_new)
    print(f"\n{'='*70}")
    print(f"Длина: {len(letter_new)} симв")

    # Проверки
    print("\n=== ПРОВЕРКИ ===")
    low = letter_new.lower()

    # 1. Длина
    if 900 <= len(letter_new) <= 1300:
        print(f"✅ Длина {len(letter_new)} в норме (900-1300)")
    elif 700 <= len(letter_new) <= 1500:
        print(f"⚠️ Длина {len(letter_new)} пограничная")
    else:
        print(f"❌ Длина {len(letter_new)} вне нормы")

    # 2. Абзацы
    paras = [p for p in letter_new.split("\n\n") if p.strip()]
    print(f"{'✅' if len(paras) == 3 else '⚠️'} Абзацев: {len(paras)} (ожидалось 3)")

    # 3. Запрещённые фразы
    found = [f for f in FORBIDDEN if f in low]
    if found:
        print(f"❌ Найдены запрещённые фразы: {found}")
    else:
        print("✅ Запрещённых фраз нет")

    # 4. Домен (налоги → 1С/Банк России/финансы)
    domain_markers = ["1с", "банк росси", "финансов", "смэв", "soap", "налог"]
    hits = [m for m in domain_markers if m in low]
    print(f"{'✅' if hits else '⚠️'} Доменные маркеры (налоги): {hits or 'нет'}")

    # 5. Грейд: не должно быть 450 млн
    if "450" in letter_new:
        print("❌ Найден 450 млн (overclaiming для middle-вакансии)")
    else:
        print("✅ Маркеров Lead-грейда (450 млн) нет")

    # 6. Буква ё
    if "ё" in letter_new:
        print("⚠️ Найдена буква «ё» (просили заменять на «е»)")
    else:
        print("✅ Буквы «ё» нет")

    # 7. Литеральные \n
    if "\\n" in letter_new:
        print("❌ Литеральные \\n\\n в тексте")
    else:
        print("✅ Переносы реальные")

    # Для сравнения: старый способ (без resume_text)
    print("\n=== СТАРЫЙ СПОСОБ (без resume_text, через _candidate()) ===")
    letter_old = await analyzer.generate_cover_letter(
        title, desc, company_name="Контур",
        vacancy_id="test_cover_old_001",
        model_override=settings.llm.cover_letter_model,
        system_override=cl_digest,
        session_id="test-cover-old-001",
    )
    print(letter_old)
    print(f"\nДлина: {len(letter_old)} симв")

if __name__ == "__main__":
    asyncio.run(main())
