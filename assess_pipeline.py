import logging
from typing import Literal
from pydantic import BaseModel, Field

from ai_analyzer import VacancyAnalyzer, SuitabilityResult
from llm.types import LLMRequest

logger = logging.getLogger(__name__)

class ScreenResult(BaseModel):
    """Результат Stage 1 (скрининг)"""
    suitable: bool
    best_resume: str | None = None
    confidence: float = 0.0
    reason: str = ""

async def assess_stage1(
    analyzer: VacancyAnalyzer,
    title: str,
    description: str,
    summaries: dict[str, str],  # Все 4: {RP: "...", RSP: "...", ROP: "...", RPO: "..."}
    digest_text: str,
    model_override: str = "qwen3.7-flash",
    session_id: str = "assess1-screen",
    vacancy_id: str | None = None,
) -> ScreenResult:
    """
    Stage 1: Скрининг.
    Вход: FULL description + ВСЕ 4 summary.
    Модель: Qwen + cache_control.
    """
    system_instructions = digest_text  # TODO: Заменить на assess1.md от Эксперта
    payload = {
        "vacancy": {"title": title, "description": description}, # FULL
        "candidates": summaries, # Все 4
    }

    req = analyzer._request(
        system_instructions=system_instructions,
        payload=payload,
        operation="stage1_select",
        structured=True,
        model_override=model_override,
        session_id=session_id,
        vacancy_id=vacancy_id,
        cache_control=True,  # Включаем explicit кеш для Qwen
    )

    # Пока используем заглушку, так как промпта assess1.md нет
    # resp = await analyzer.provider.generate_structured(req, ScreenResult)
    # return resp.data
    
    # ЗАГЛУШКА: возвращаем dummy-результат, чтобы можно было протестировать пайплайн
    logger.warning("Используется заглушка для assess_stage1! Нужен промпт assess1.md.")
    return ScreenResult(
        suitable=True,
        best_resume="RP",
        confidence=0.9,
        reason="Заглушка: вакансия подходит."
    )

async def assess_stage2(
    analyzer: VacancyAnalyzer,
    title: str,
    description: str,
    best_resume: str,
    resume_text: str,
    alternatives: dict[str, dict],  # {RSP: {title, summary_short}, ...}
    digest_text: str,
    model_override: str = "mimo-v2.6-flash",
    session_id: str = "assess2-eval",
    vacancy_id: str | None = None,
) -> SuitabilityResult:
    """
    Stage 2: Оценка.
    Вход: resume_text выбранного + FULL description + alternatives.
    Модель: MiMo (implicit cache).
    """
    system_instructions = digest_text + "\n\n---\n\n## РЕЗЮМЕ КАНДИДАТА\n\n" + resume_text
    payload = {
        "vacancy": {"title": title, "description": description}, # FULL
        "alternatives": alternatives, # Title + summary_short
    }

    req = analyzer._request(
        system_instructions=system_instructions,
        payload=payload,
        operation="vacancy_analysis",
        structured=True,
        model_override=model_override,
        session_id=session_id,
        vacancy_id=vacancy_id,
        cache_control=False, # MiMo ломается от cache_control
    )

    # Пока используем заглушку
    # resp = await analyzer.provider.generate_structured(req, SuitabilityResult)
    # return resp.data
    
    logger.warning("Используется заглушка для assess_stage2! Нужен промпт assess2.md.")
    return SuitabilityResult(
        suitable=True,
        requirements=[],
        verdict="Заглушка",
        confidence=0.9,
        best_resume=best_resume
    )
