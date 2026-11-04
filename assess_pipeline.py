import logging
from pathlib import Path
from pydantic import BaseModel, Field, AliasChoices

from ai_analyzer import VacancyAnalyzer, SuitabilityResult

logger = logging.getLogger(__name__)

class ScreenResult(BaseModel):
    """Результат Stage 1 (скрининг)"""
    suitable: bool
    best_resume: str | None = None
    confidence: float | None = 0.0
    reason: str = Field(default="", validation_alias=AliasChoices("reason", "verdict"))

def extract_hints(title: str, desc: str) -> list[dict]:
    import yaml
    rules_path = Path("stage1_hints.yaml")
    if not rules_path.exists():
        return []
    data = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    hints_list = data.get("hint", [])
    text = (title + " " + desc).lower()
    matched_hints = []
    for h in hints_list:
        keywords = h.get("keywords", [])
        matched_kws = []
        for kw in keywords:
            kw_lower = kw.lower()
            parts = kw_lower.split()
            if len(parts) > 1:
                if all(part[:5] in text for part in parts if len(part) >= 4):
                    matched_kws.append(kw)
            else:
                if len(kw_lower) >= 4 and kw_lower[:5] in text:
                    matched_kws.append(kw)
        if matched_kws:
            matched_hints.append({
                "matched_keywords": matched_kws,
                "likely_profile": h.get("likely_profile", [])
            })
    return matched_hints

def validate_stage2_result(result: SuitabilityResult) -> SuitabilityResult:
    """Post-processing предохранитель от логических ошибок LLM."""
    update_data = {}
    req_changed = False
    new_reqs = []
    hard_reject_reason = ""
    
    # 1. Сначала проверяем is_required (и код-страховку). Это имеет высший приоритет.
    for req in result.requirements:
        is_req = getattr(req, "is_required", False)
        
        # КОД-СТРАХОВКА: level<=2 и match < 0.5 -> принудительно is_required=True
        if req.level in (1, 2) and req.match < 0.5 and not is_req:
            is_req = True
            req = req.model_copy(update={"is_required": True})
            req_changed = True
            logger.warning(f"FALLBACK level1-2_match_below_05 for: {req.text[:50]}")
            
        new_reqs.append(req)
        
        if is_req and req.match < 0.7 and not hard_reject_reason:
            hard_reject_reason = f"is_required match={req.match} ({req.text[:50]})"
    
    # 2. Проверяем вердикт (если is_required не сработал)
    # AI9-fix: только НАЧАЛО verdict, не вхождение. Иначе «Подаваться... возможен отказ
    # по overqualified» → false reject.
    verdict_lower = (result.verdict or "").strip().lower()
    negative_prefixes = ("не подаваться", "не подходит", "отказ", "не рекоменд")
    verdict_reject = verdict_lower.startswith(negative_prefixes)
    
    # 3. Формируем итоговые флаги (is_required перебивает вердикт)
    if hard_reject_reason:
        update_data["suitable"] = False
        update_data["hard_filter_triggered"] = True
        update_data["hard_filter_reason"] = hard_reject_reason
        logger.warning(f"HARD REJECT (is_required): {hard_reject_reason}")
    elif verdict_reject:
        if result.suitable:
            update_data["suitable"] = False
            logger.warning(f"HARD REJECT (verdict): suitable was True, forced to False")
        if not getattr(result, "hard_filter_triggered", False):
            update_data["hard_filter_triggered"] = True
            update_data["hard_filter_reason"] = "post-process: verdict says 'не подаваться'"
            logger.warning(f"HARD REJECT (verdict): {update_data['hard_filter_reason']}")

    # AI10-fix: модель может вернуть suitable=True + hard_filter_triggered=True.
    # Если hard_filter стоит (свой или модельный) — suitable обязан быть False.
    _hf = update_data.get("hard_filter_triggered") or getattr(result, "hard_filter_triggered", False)
    if _hf and result.suitable:
        update_data["suitable"] = False
        if "hard_filter_reason" not in update_data:
            update_data["hard_filter_reason"] = (
                getattr(result, "hard_filter_reason", "") or "model hf=True, suitable forced False"
            )
        logger.warning("HARD REJECT (hf=True): suitable was True, forced to False")

    if req_changed:
        update_data["requirements"] = new_reqs
        
    if update_data:
        return result.model_copy(update=update_data)
    return result


async def assess_stage1(
    analyzer: VacancyAnalyzer,
    title: str,
    description: str,
    summaries: dict[str, str],
    hints: list[dict] | None = None,
    digest_path: str = "docs/datasets/assess1.md",
    model_override: str = "qwen3.7-flash",
    session_id: str = "assess1-screen-v1",
    vacancy_id: str | None = None,
) -> ScreenResult:
    p = Path(digest_path)
    if not p.exists():
        raise FileNotFoundError(f"Не найден промпт: {digest_path}")
    system_instructions = p.read_text(encoding="utf-8")

    payload = {
        "vacancy": {"title": title, "description": description},
        "candidates": summaries,
    }
    if hints:
        payload["hints"] = hints

    req = analyzer._request(
        system_instructions=system_instructions,
        payload=payload,
        operation="stage1_select",
        structured=True,
        model_override=model_override,
        session_id=session_id,
        vacancy_id=vacancy_id,
        cache_control=True,
    )

    resp, parsed = await analyzer.provider.generate_structured(req, ScreenResult)
    
    if parsed.confidence is None:
        logger.warning(f"Stage 1 confidence is None for {vacancy_id}. Raw: {resp.text[:200]}")
        parsed = parsed.model_copy(update={"confidence": 0.0})
        
    return parsed

async def assess_stage2(
    analyzer: VacancyAnalyzer,
    title: str,
    description: str,
    best_resume: str,
    resume_text: str,
    alternatives: dict[str, dict],
    digest_path: str = "docs/datasets/assess2.md",
    model_override: str = "mimo-v2.6-flash",
    session_id: str = "assess2-eval-v1",
    vacancy_id: str | None = None,
) -> SuitabilityResult:
    p = Path(digest_path)
    if not p.exists():
        raise FileNotFoundError(f"Не найден промпт: {digest_path}")
    
    base_digest = p.read_text(encoding="utf-8")
    system_instructions = base_digest + "\n\n---\n\n## РЕЗЮМЕ КАНДИДАТА\n\n" + resume_text

    payload = {
        "vacancy": {"title": title, "description": description},
        "alternatives": alternatives,
    }

    req = analyzer._request(
        system_instructions=system_instructions,
        payload=payload,
        operation="vacancy_analysis",
        structured=True,
        model_override=model_override,
        session_id=session_id,
        vacancy_id=vacancy_id,
        cache_control=False,
    )

    resp, parsed = await analyzer.provider.generate_structured(req, SuitabilityResult)
    parsed = validate_stage2_result(parsed)
    return parsed
