"""Smoke-тесты фиксов AI9.

Закрепляют то, что сделано в сессии AI9:
1. validate_stage2_result: verdict_reject через startswith (не in) — ложный reject исправлен.
2. check_prefilter: короткие аббревиатуры (РКО, PKI) через word boundary.
3. SuitabilityResult: overqualified/overqualified_reason сохраняются через _normalize.
4. PrefilterRules: загрузка из prefilter_rules.yaml + fallback на profile.yaml.
5. Database.init() на чистой БД: все колонки на месте (read_error_count, cached_tokens, queued_at).

Не покрывает: reconcile_vacancies, control_run, LLM-стадии (отдельная задача).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

# scripts/ — не пакет, добавляем в sys.path для импорта assess_worker.check_prefilter
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from ai_analyzer import Requirement, SuitabilityResult
from assess_pipeline import validate_stage2_result
from config import (
    ContextualKeyword,
    EmploymentTypeFilter,
    PrefilterRules,
    _fallback_prefilter,
    _load_prefilter_rules,
)
from database import Database


# ---------- 1. verdict_reject через startswith ----------


def _make_result(verdict: str, suitable: bool = True) -> SuitabilityResult:
    return SuitabilityResult(
        suitable=suitable,
        best_resume="RP",
        requirements=[
            Requirement(level=2, label="X", text="t", match=0.8, evidence="", is_required=False)
        ],
        verdict=verdict,
        confidence=0.8,
    )


@pytest.mark.parametrize(
    "verdict, expected_suitable",
    [
        # «Подаваться… отказ по overqualified» — НЕ reject (фикс AI9)
        ("Подаваться. Возможен отказ по overqualified.", True),
        # «Подаваться… не подходит по домену» — НЕ reject (фикс AI9)
        ("Подаваться. Домен не подходит, но смежный.", True),
        # Начинается с «Не подаваться» — reject
        ("Не подаваться. Ядро не покрыто.", False),
        # Начинается с «Отказ» — reject
        ("Отказ. Домен не покрыт.", False),
        # Начинается с «Не подходит» — reject
        ("Не подходит по грейду.", False),
    ],
)
def test_verdict_reject_uses_startswith(verdict: str, expected_suitable: bool) -> None:
    """AI9-fix: verdict_reject только по началу verdict, не по вхождению.

    Старое поведение (in) давало ложный reject: «Подаваться… возможен отказ» → reject.
    """
    result = _make_result(verdict, suitable=True)
    out = validate_stage2_result(result)
    assert out.suitable is expected_suitable, (
        f"verdict={verdict!r}: suitable={out.suitable}, expected={expected_suitable}"
    )


# ---------- 2. check_prefilter: короткие аббревиатуры ----------


def _prefilter_settings(**overrides) -> SimpleNamespace:
    """Минимальный settings.profile.prefilter для check_prefilter."""
    pf = PrefilterRules(
        junior_title_markers=("junior", "стажёр"),
        experience_allowed=("between3And6", "moreThan6"),
        salary_hard_min=250_000,
        excluded_positions=("Разработчик",),
        excluded_companies=("СИБИНТЕК",),
        excluded_keywords=("РКО", "PKI", "эквайринг", "вентиляц"),
        excluded_keywords_contextual=(),
        employment_type_filter=None,
    )
    return SimpleNamespace(profile=SimpleNamespace(prefilter=pf))


@pytest.mark.parametrize(
    "desc, expected_reject",
    [
        # РКО как отдельное слово — reject
        ("Требуется опыт РКО, расчётно-кассовое обслуживание.", "keyword: РКО"),
        # РКО в скобках — reject
        ("Знание банковских продуктов (РКО, эквайринг).", "keyword: РКО"),
        # «банковского» — НЕ РКО (word boundary)
        ("Наши заказчики — крупные компании из банковского сектора.", ""),
        # «руководство» — НЕ РКО
        ("Руководство проектами и командами разработки.", ""),
        # «маркетплейс» — НЕ РКО
        ("Работа с маркетплейсами и партнёрствами.", ""),
        # реальный эквайринг — reject (длинное слово, substring)
        ("Опыт эквайринга и платёжных систем.", "keyword: эквайринг"),
        # реальный PKI — reject (короткая аббревиатура, word boundary)
        ("Знание PKI обязательно.", "keyword: PKI"),
        # вентиляция — reject (substring)
        ("Проектирование систем вентиляции.", "keyword: вентиляц"),
    ],
)
def test_check_prefilter_word_boundary(desc: str, expected_reject: str) -> None:
    """AI9-fix: короткие аббревиатуры (РКО, PKI) — word boundary, не substring.

    Старое поведение (in) ловило «банковского», «руководство», «маркетплейс».
    """
    from assess_worker import check_prefilter

    reject, _et_special, _et_note = check_prefilter(desc, _prefilter_settings())
    if expected_reject:
        assert reject == expected_reject, f"desc={desc!r}: reject={reject!r}, expected={expected_reject!r}"
    else:
        assert reject == "", f"desc={desc!r}: ложный reject={reject!r}"


# ---------- 3. SuitabilityResult: overqualified через _normalize ----------


def test_overqualified_preserved_through_normalize() -> None:
    """AI9: overqualified/overqualified_reason в root_keys, иначе _normalize выбросит."""
    r = SuitabilityResult.model_validate(
        {
            "suitable": True,
            "verdict": "Подаваться с оговоркой",
            "confidence": 0.7,
            "overqualified": True,
            "overqualified_reason": "роль team lead L2, кандидат senior",
        }
    )
    assert r.overqualified is True
    assert "team lead" in r.overqualified_reason
    assert r.suitable is True  # overqualified НЕ влияет на suitable


def test_overqualified_defaults_to_false() -> None:
    """Без overqualified в JSON — default False, reason пустой."""
    r = SuitabilityResult(suitable=True, verdict="ok", confidence=0.8)
    assert r.overqualified is False
    assert r.overqualified_reason == ""


def test_overqualified_not_touched_by_validate_stage2() -> None:
    """AI9: validate_stage2_result НЕ трогает overqualified (вне requirements[])."""
    r = SuitabilityResult.model_validate(
        {
            "suitable": True,
            "verdict": "Подаваться",
            "confidence": 0.7,
            "overqualified": True,
            "overqualified_reason": "мидловая роль",
            "requirements": [
                {"level": 2, "label": "X", "text": "t", "match": 0.8, "evidence": "", "is_required": False}
            ],
        }
    )
    out = validate_stage2_result(r)
    assert out.overqualified is True
    assert out.overqualified_reason == "мидловая роль"


# ---------- 4. PrefilterRules: загрузка из yaml + fallback ----------


def _candidate_hh():
    """Минимальные CandidateProfile/HHProfile для fallback."""
    from config import CandidateProfile, HHProfile

    candidate = CandidateProfile(
        name="Test", location="Msk", desired_positions=("PM",),
        experience_summary="x", education="", technologies=(), projects=(),
        github_url="", salary_expectation="", work_format=(),
        excluded_positions=("Разработчик",), additional_information="",
        excluded_companies=("СИБИНТЕК",), excluded_keywords=("РКО",),
    )
    hh = HHProfile(
        resume_name="PM", search_queries=("PM",), areas=(),
        experience_filters=("от 3 до 6 лет", "более 6 лет"),
    )
    return candidate, hh


def test_prefilter_loads_from_yaml(tmp_path: Path) -> None:
    """AI9: prefilter_rules.yaml читается, значения из yaml, не из profile.yaml."""
    data = {
        "junior_title_markers": ["junior", "trainee"],
        "experience_allowed": ["between3And6", "moreThan6"],
        "salary_hard_min": 250000,
        "excluded_positions": ["DevOps"],
        "excluded_companies": ["The_covert"],
        "excluded_keywords": ["РКО", "PKI"],
        "excluded_keywords_contextual": [
            {"keyword": "IPMA", "context": "обязательная сертификация"}
        ],
        "employment_type_flags": {
            "patterns": ["ГПХ", "самозанят"],
            "exceptions": ["ТК РФ"],
            "note": "Проверить формат оформления.",
        },
    }
    path = tmp_path / "prefilter_rules.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    candidate, hh = _candidate_hh()
    errors: list[str] = []
    pf = _load_prefilter_rules(path, candidate, hh, errors)

    assert errors == []
    assert pf.junior_title_markers == ("junior", "trainee")
    assert pf.salary_hard_min == 250000
    assert pf.excluded_positions == ("DevOps",)
    assert pf.excluded_companies == ("The_covert",)
    assert pf.excluded_keywords == ("РКО", "PKI")
    assert len(pf.excluded_keywords_contextual) == 1
    assert pf.excluded_keywords_contextual[0].keyword == "IPMA"
    assert pf.employment_type_filter is not None
    assert pf.employment_type_filter.patterns == ("ГПХ", "самозанят")
    assert pf.employment_type_filter.exceptions == ("ТК РФ",)


def test_prefilter_fallback_when_yaml_missing(tmp_path: Path) -> None:
    """AI9: если prefilter_rules.yaml нет — fallback на profile.yaml (candidate/hh)."""
    missing = tmp_path / "nonexistent.yaml"
    candidate, hh = _candidate_hh()
    errors: list[str] = []
    pf = _load_prefilter_rules(missing, candidate, hh, errors)

    assert errors == []
    # fallback: excluded_positions из candidate
    assert pf.excluded_positions == ("Разработчик",)
    assert pf.excluded_companies == ("СИБИНТЕК",)
    assert pf.excluded_keywords == ("РКО",)
    # experience_allowed — нормализован из hh.experience_filters
    assert set(pf.experience_allowed) == {"between3And6", "moreThan6"}
    # employment filter отсутствует в fallback
    assert pf.employment_type_filter is None


# ---------- 5. Database.init(): все колонки на чистой БД ----------


def test_database_init_on_clean_db(tmp_path: Path) -> None:
    """AI9-fix: init() на чистой БД не падает (read_error_count до CREATE, cached_tokens).

    Было: read_error_count ALTER до CREATE TABLE vacancies → OperationalError.
    Было: cached_tokens не в CREATE TABLE llm_requests → нет колонки.
    """
    db = Database(tmp_path / "test.db")
    db.init()

    with db._connect() as c:
        vac_cols = {r[1] for r in c.execute("PRAGMA table_info(vacancies)")}
        llm_cols = {r[1] for r in c.execute("PRAGMA table_info(llm_requests)")}

    required_vac = {
        "read_error_count", "queued_at", "queue_priority",
        "evaluation_json", "description", "salary_from", "salary_to",
    }
    required_llm = {"cached_tokens", "vacancy_id", "raw_response", "prompt_version"}

    assert required_vac <= vac_cols, f"missing vac: {required_vac - vac_cols}"
    assert required_llm <= llm_cols, f"missing llm: {required_llm - llm_cols}"


def test_database_init_includes_irrelevant_status(tmp_path: Path) -> None:
    """AI9: CHECK (status IN ...) содержит 'irrelevant' (reconcile)."""
    db = Database(tmp_path / "test.db")
    db.init()

    with db._connect() as c:
        row = c.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='vacancies'"
        ).fetchone()

    assert row is not None
    assert "'irrelevant'" in (row[0] or "")
