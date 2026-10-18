#!/usr/bin/env python3
"""test_cover_letter_models.py — сравнение моделей на генерации сопроводов.

12 вакансий × 5 моделей = 60 генераций. НЕ пишет в БД. Сохраняет в md-файл
для оценки экспертной сессией.

Модели:
- AITUNNEL: deepseek-v3.2, deepseek-v4.1-flash, qwen3.7-flash
- GigaChat: GigaChat-2-Max, GigaChat-3-Ultra

CLI:
    python3 scripts/test_cover_letter_models.py --dry-run
    python3 scripts/test_cover_letter_models.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

from config import load_settings                                    # noqa: E402
from database import Database                                      # noqa: E402
from ai_analyzer import VacancyAnalyzer, AnalysisError             # noqa: E402
from llm.managed import ManagedLLMProvider                         # noqa: E402
from llm.factory import create_llm_provider                        # noqa: E402
from llm.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402

VACANCY_IDS = [
    "138442950", "137729841", "138556390", "138419202",
    "137587980", "137582581", "138204825", "138072797",
    "137353733", "138173704", "138530680", "138558358",
]

# V31_MODE: только 2 модели для ретеста v3.1
AITUNNEL_MODELS = [
    ("qwen3.7-flash", "test-cl-v31-qwen"),
]

GIGACHAT_MODELS = [
    ("GigaChat-3-Ultra", "test-cl-v31-gc-ultra"),
]

DEFAULT_DIGEST = "docs/datasets/ds_cover_letter_digest.md"
DEFAULT_OUTPUT = "docs/datasets/cover_letter_models_test_v31_20261018.md"


def fetch_vacancies(db_path: Path) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        placeholders = ", ".join("?" for _ in VACANCY_IDS)
        query = (
            "SELECT id, title, description, company, url, "
            "       cover_letter, evaluation_json "
            f"FROM vacancies WHERE id IN ({placeholders})"
        )
        rows = conn.execute(query, VACANCY_IDS).fetchall()
    return [dict(r) for r in rows]


def create_aitunnel_provider(settings, db):
    api_key = (settings.llm.aitunnel_api_key or "").strip()
    base_url = (settings.llm.aitunnel_base_url or "https://api.aitunnel.ru/v1").strip()
    if not api_key:
        return None
    adapter = OpenAICompatibleProvider(base_url, api_key, json_mode=False)
    return ManagedLLMProvider(
        adapter, db,
        max_retries=settings.llm.max_retries,
        max_requests_per_day=1000,
    )


def render_vacancy_block(
    v: dict,
    results: dict[str, dict],
) -> str:
    """Формирует markdown-блок по одной вакансии."""
    lines: list[str] = []
    lines.append(f"\n## {v['id']} — {v['title']} ({v['company'] or '—'})")
    lines.append(f"**URL:** {v['url']}")
    lines.append(f"**Description length:** {len(v.get('description') or '')} символов")
    lines.append("")

    # Требования из evaluation_json
    if v.get("evaluation_json"):
        try:
            ev = json.loads(v["evaluation_json"])
            reqs = ev.get("requirements") or []
            if reqs:
                lines.append("**Требования (из evaluation_json):**")
                for r in reqs[:8]:
                    lines.append(
                        f"- [L{r.get('level','?')}] {r.get('text','')[:120]} "
                        f"(match={r.get('match','?')})"
                    )
                lines.append("")
        except Exception:
            pass

    # Baseline
    baseline = (v.get("cover_letter") or "").strip()
    if baseline:
        lines.append(f"**Baseline (текущее письмо в БД, len={len(baseline)}):**")
        lines.append("```")
        lines.append(baseline)
        lines.append("```")
        lines.append("")

    # Модели
    for model, res in results.items():
        if res.get("error"):
            lines.append(f"### {model} — ❌ ERROR: {res['error']}")
            lines.append("")
            continue
        letter = res.get("letter") or ""
        lines.append(f"### {model} (len={len(letter)})")
        if res.get("fallback"):
            lines.append("**⚠️ FALLBACK: письмо получено после retry (первый ответ отбракован)**")
        if res.get("raw") and res.get("raw") != letter:
            lines.append(f"**RAW (первый ответ, длина {len(res['raw'])}):**")
            lines.append("```")
            lines.append(res["raw"][:800])
            lines.append("```")
        lines.append("```")
        lines.append(letter if letter else "(пусто)")
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


async def run(args: argparse.Namespace) -> int:
    settings = load_settings(Path(args.env_file))
    db_path = Path(settings.database_path)
    db = Database(db_path)
    db.init()

    digest_path = Path(args.digest)
    if not digest_path.exists():
        print(f"ERROR: {digest_path} не найден", file=sys.stderr)
        return 2
    system_override = digest_path.read_text(encoding="utf-8")
    print(f"INFO: digest {digest_path} ({len(system_override)} символов)")

    vacancies = fetch_vacancies(db_path)
    print(f"INFO: вакансий: {len(vacancies)}")
    total = len(vacancies) * (len(AITUNNEL_MODELS) + len(GIGACHAT_MODELS))
    print(f"INFO: моделей: {len(AITUNNEL_MODELS) + len(GIGACHAT_MODELS)}, всего вызовов: {total}")

    if args.dry_run:
        for v in vacancies:
            print(f"  {v['id']}  {v['title'][:50]}  (desc={len(v.get('description') or '')})")
        print("\nDRY-RUN: ничего не сгенерировано")
        return 0

    # Провайдеры
    aitunnel_provider = create_aitunnel_provider(settings, db)
    if aitunnel_provider is None:
        print("ERROR: AITUNNEL provider не создан", file=sys.stderr)
        return 3
    gc_provider = create_llm_provider(settings, db)

    aitunnel_analyzer = VacancyAnalyzer(settings, aitunnel_provider)
    gc_analyzer = VacancyAnalyzer(settings, gc_provider)

    blocks: list[str] = [
        "# Тест моделей для генерации сопроводов",
        f"\n**Дата:** 18.10.2026",
        f"\n**Вакансий:** {len(vacancies)}",
        f"\n**Модели:** {', '.join(m for m, _ in AITUNNEL_MODELS + GIGACHAT_MODELS)}",
        f"\n**Дайджест:** `{digest_path}` ({len(system_override)} символов)",
        "\n**Задача для эксперта:** оцени каждое письмо по шкале 1-5, "
        "сводная таблица модель × вакансия → оценка, средняя по модели, "
        "рекомендация: какая модель лучше для сопроводов.",
        "\n---",
    ]

    t0 = time.monotonic()
    done = errors = 0

    try:
        for v in vacancies:
            print(f"\n=== {v['id']} {v['title'][:50]} ===")
            vid = v["id"]
            results: dict[str, dict] = {}

            for model, session_id in AITUNNEL_MODELS:
                print(f"  [{model}] ...", end=" ", flush=True)
                try:
                    letter = await aitunnel_analyzer.generate_cover_letter(
                        v["title"], v["description"],
                        company_name=v.get("company") or "",
                        vacancy_id=vid,
                        model_override=model,
                        system_override=system_override,
                        session_id=session_id,
                    )
                    results[model] = {"letter": letter, "error": None}
                    done += 1
                    print(f"OK len={len(letter)}")
                except Exception as exc:
                    results[model] = {"letter": "", "error": f"{type(exc).__name__}: {exc}"}
                    errors += 1
                    print(f"ERR: {exc}")

            for model, session_id in GIGACHAT_MODELS:
                print(f"  [{model}] ...", end=" ", flush=True)
                try:
                    letter = await gc_analyzer.generate_cover_letter(
                        v["title"], v["description"],
                        company_name=v.get("company") or "",
                        vacancy_id=vid,
                        model_override=model,
                        system_override=system_override,
                        session_id=session_id,
                    )
                    results[model] = {"letter": letter, "error": None}
                    done += 1
                    print(f"OK len={len(letter)}")
                except Exception as exc:
                    results[model] = {"letter": "", "error": f"{type(exc).__name__}: {exc}"}
                    errors += 1
                    print(f"ERR: {exc}")

            blocks.append(render_vacancy_block(v, results))

    finally:
        try:
            await aitunnel_provider.close()
        except Exception:
            pass
        try:
            await gc_provider.close()
        except Exception:
            pass

    out_path = Path(args.output)
    out_path.write_text("\n".join(blocks), encoding="utf-8")
    dt = time.monotonic() - t0

    print(f"\n=== DONE ===")
    print(f"  success={done}  errors={errors}")
    print(f"  elapsed={dt:.1f}s  avg={dt/max(done,1):.1f}s per call")
    print(f"  → {out_path} ({out_path.stat().st_size} байт)")
    return 0


def cli() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env-file", default=".env")
    p.add_argument("--digest", default=DEFAULT_DIGEST)
    p.add_argument("--output", default=DEFAULT_OUTPUT)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(cli())
