#!/usr/bin/env python3
"""evaluate_models.py — прогон вакансий из БД через несколько GigaChat-моделей.

Прямой вызов analyzer.assess() с model_override. Без hh.ru, без CloakBrowser,
без TG. Только LLM и запись в evaluations.

CLI:
    python3 scripts/evaluate_models.py --calibration
    python3 scripts/evaluate_models.py --all
    python3 scripts/evaluate_models.py --ids 138357711,138528225
    python3 scripts/evaluate_models.py --calibration --models GigaChat-2-Pro,GigaChat-2-Max
    python3 scripts/evaluate_models.py --calibration --dry-run
    python3 scripts/evaluate_models.py --calibration --force   # перезапись
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

# Форсим построчный вывод, чтобы progress был виден в pipe/tee
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import os                                                          # noqa: E402

from config import load_settings                                    # noqa: E402
from database import Database                                      # noqa: E402
from ai_analyzer import VacancyAnalyzer, AnalysisError             # noqa: E402
from main import create_llm_provider                               # noqa: E402
from llm.managed import ManagedLLMProvider                         # noqa: E402
from llm.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402
from dataclasses import replace  # noqa: E402

DEFAULT_MODELS_GIGACHAT = [
    "GigaChat-2-Pro",
    "GigaChat-2-Max",
    "GigaChat-3-Pro",
    "GigaChat-3-Ultra",
]

DEFAULT_MODELS_AITUNNEL = [
    "deepseek-v4.1-flash",
    "deepseek-v3.2",
    "qwen3.7-flash",
]


def create_aitunnel_provider(settings, db):
    """Создаёт ManagedLLMProvider поверх AITUNNEL (OpenAI-совместимый)."""
    api_key = os.getenv("AITUNNEL_API_KEY", "").strip()
    base_url = os.getenv("AITUNNEL_BASE_URL", "https://api.aitunnel.ru/v1").strip()
    if not api_key:
        print("ERROR: AITUNNEL_API_KEY не найден в окружении. "
              "Проверь .env и что он загружен (load_settings подтянет).",
              file=sys.stderr)
        return None
    adapter = OpenAICompatibleProvider(
        base_url,
        api_key,
        json_mode=True,
        reasoning_enabled=None,
    )
    return ManagedLLMProvider(
        adapter,
        db,
        max_retries=settings.llm.max_retries,
        max_requests_per_day=1000,
    )


def load_calibration_ids(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [v["id"] for v in data["positive"]] + [v["id"] for v in data["negative"]]


def fetch_vacancies(db_path: Path, ids: list[str] | None) -> list[dict]:
    """Возвращает [{id, title, description}] для вакансий с непустым описанием."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if ids:
            placeholders = ", ".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT id, title, description FROM vacancies "
                f"WHERE description != '' AND id IN ({placeholders})",
                ids,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, title, description FROM vacancies "
                "WHERE description != '' ORDER BY discovered_at"
            ).fetchall()
    return [dict(r) for r in rows]


def get_llm_request_meta(db_path: Path, vacancy_id: str, model: str) -> dict | None:
    """Последний успешный vacancy_analysis для (vacancy_id, model)."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT id, provider, model, prompt_version
            FROM llm_requests
            WHERE vacancy_id = ?
              AND operation = 'vacancy_analysis'
              AND success = 1
              AND model = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (vacancy_id, model),
        ).fetchone()
    return dict(row) if row else None


def already_evaluated(db_path: Path, vacancy_id: str, model: str) -> bool:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM evaluations WHERE vacancy_id = ? AND model = ? LIMIT 1",
            (vacancy_id, model),
        ).fetchone()
    return row is not None


async def run(args: argparse.Namespace) -> int:
    settings = load_settings(Path(args.env_file))
    if args.max_tokens:
        settings = replace(settings, llm=replace(settings.llm, max_output_tokens=args.max_tokens))
        print(f"INFO: LLM_MAX_OUTPUT_TOKENS override → {args.max_tokens}")
    db_path = Path(settings.database_path)
    db = Database(db_path)
    db.init()

    # --- выборка вакансий ---
    calibration_ids: set[str] = set()
    if args.calibration:
        cal_path = REPO / "docs/datasets/calibration_set.json"
        if not cal_path.exists():
            print(f"ERROR: не найден {cal_path}", file=sys.stderr)
            return 2
        ids = load_calibration_ids(cal_path)
        calibration_ids = set(ids)
        vacancies = fetch_vacancies(db_path, ids)
    elif args.ids:
        ids = [x.strip() for x in args.ids.split(",") if x.strip()]
        vacancies = fetch_vacancies(db_path, ids)
    elif args.all:
        vacancies = fetch_vacancies(db_path, None)
    else:
        print("ERROR: укажи --calibration / --all / --ids", file=sys.stderr)
        return 2

    if not vacancies:
        print("INFO: нечего прогонять — нет вакансий с описанием")
        return 0

    if args.models:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    else:
        models = (DEFAULT_MODELS_AITUNNEL if args.provider == "aitunnel"
                  else DEFAULT_MODELS_GIGACHAT)
    total = len(vacancies) * len(models)

    print(f"INFO: вакансий={len(vacancies)}  моделей={len(models)}  вызовов={total}")
    print(f"INFO: models={models}")
    if calibration_ids:
        print(f"INFO: калибровочный набор, is_calibration=1 для {len(calibration_ids)} вакансий")

    if args.dry_run:
        for v in vacancies:
            already = {m: already_evaluated(db_path, v["id"], m) for m in models}
            flag = " ".join(f"{m}:{'Y' if already[m] else '-'}" for m in models)
            print(f"  {v['id']}  {v['title'][:55]:<55}  {flag}")
        print("\nDRY-RUN: ничего не вызвано, ничего не записано")
        return 0

    # --- session_id (для кэша) ---
    session_id = args.session_id
    if not session_id:
        from datetime import datetime
        session_id = f"{args.provider}-calibration-{datetime.now():%Y%m%d-%H%M%S}"
    print(f"INFO: session_id={session_id}")
    print(f"INFO: cache {'включён' if args.provider == 'aitunnel' else 'не используется (GigaChat)'}")

    # --- system_override (обогащённый промпт) ---
    system_override: str | None = None
    if args.system_from_file:
        if not args.system_from_file.exists():
            print(f"ERROR: {args.system_from_file} не найден", file=sys.stderr)
            return 2
        system_override = args.system_from_file.read_text(encoding="utf-8")
        print(f"INFO: system_override из {args.system_from_file} "
              f"({len(system_override)} символов)")

    # --- инициализация провайдера и анализатора ---
    if args.provider == "aitunnel":
        provider = create_aitunnel_provider(settings, db)
        if provider is None:
            return 3
        print(f"INFO: provider=AITUNNEL (OpenAI-совместимый, base_url из AITUNNEL_BASE_URL)")
    else:
        provider = create_llm_provider(settings, db)
        print(f"INFO: provider={settings.llm.provider} (штатный)")
    analyzer = VacancyAnalyzer(settings, provider)

    done = skipped = errors = 0
    t0 = time.monotonic()

    try:
        for model in models:
            print(f"\n=== MODEL: {model} ===")
            for v in vacancies:
                vid = v["id"]
                if not args.force and already_evaluated(db_path, vid, model):
                    skipped += 1
                    print(f"  [skip] {vid} (already)")
                    continue

                # Retry на transient_server / rate_limit
                result = None
                last_exc = None
                for attempt in range(1, 4):  # 3 попытки
                    try:
                        result = await analyzer.assess(
                            v["title"], v["description"],
                            vacancy_id=vid, model_override=model,
                            session_id=session_id,
                            system_override=system_override,
                        )
                        break
                    except AnalysisError as exc:
                        last_exc = exc
                        if exc.error_type in ("transient_server", "rate_limit"):
                            wait = args.delay * attempt * 3  # 9 / 18 / 27 сек
                            print(f"  [retry] {vid} {exc.error_type}, wait {wait:.0f}s (attempt {attempt}/3)")
                            await asyncio.sleep(wait)
                            continue
                        # другая ошибка — не retry-абельная
                        errors += 1
                        print(f"  [err ] {vid} {exc.error_type}")
                        break
                    except Exception as exc:
                        errors += 1
                        print(f"  [err ] {vid} unexpected: {type(exc).__name__}: {exc}")
                        break

                if result is None:
                    if last_exc and last_exc.error_type in ("transient_server", "rate_limit"):
                        errors += 1
                        print(f"  [fail] {vid} {last_exc.error_type} after 3 attempts")
                    continue

                # Пауза после успешного вызова
                await asyncio.sleep(args.delay)

                meta = get_llm_request_meta(db_path, vid, model)
                if meta is None:
                    print(f"  [warn] {vid}: llm_request meta not found, skip write")
                    errors += 1
                    continue

                eval_data = {
                    "verdict": result.verdict,
                    "requirements": [
                        {"level": r.level, "label": r.label, "text": r.text,
                         "match": r.match, "evidence": r.evidence}
                        for r in result.requirements
                    ],
                    "confidence": result.confidence,
                }
                db.record_evaluation(
                    vacancy_id=vid,
                    llm_request_id=meta["id"],
                    provider=meta["provider"],
                    model=meta["model"],
                    prompt_version=meta["prompt_version"],
                    suitable=result.suitable,
                    confidence=result.confidence,
                    verdict=result.verdict,
                    requirements_json=json.dumps(eval_data, ensure_ascii=False),
                    is_calibration=1 if vid in calibration_ids else 0,
                )
                done += 1
                conf = result.confidence if result.confidence is not None else 0.0
                print(f"  [ ok ] {vid} suitable={int(result.suitable)} conf={conf:.3f}")
    finally:
        await provider.close()

    dt = time.monotonic() - t0
    print(f"\n=== DONE ===")
    print(f"  success={done}  skipped={skipped}  errors={errors}")
    print(f"  elapsed={dt:.1f}s  avg={dt/max(done,1):.1f}s per call")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env-file", default=".env")
    p.add_argument("--calibration", action="store_true",
                   help="только 20 вакансий из calibration_set.json")
    p.add_argument("--all", action="store_true",
                   help="все вакансии с непустым description")
    p.add_argument("--ids", default=None,
                   help="список vacancy_id через запятую")
    p.add_argument("--models", default=None,
                   help="через запятую (default зависит от --provider)")
    p.add_argument("--provider", default="gigachat",
                   choices=["gigachat", "aitunnel"],
                   help="gigachat (штатный) или aitunnel (OpenAI-совместимый)")
    p.add_argument("--force", action="store_true",
                   help="перезаписать уже существующие записи")
    p.add_argument("--delay", type=float, default=3.0,
                   help="пауза между вызовами LLM в секундах (default: 3.0)")
    p.add_argument("--max-tokens", type=int, default=None,
                   help="override LLM_MAX_OUTPUT_TOKENS (для reasoning-моделей)")
    p.add_argument("--session-id", default=None,
                   help="session_id для кэша (DeepSeek/AITUNNEL). Default: auto из provider+timestamp")
    p.add_argument("--system-from-file", type=Path, default=None,
                   help="путь к файлу с system prompt (заменяет rules 1-24, для AITUNNEL)")
    p.add_argument("--dry-run", action="store_true",
                   help="только показать, что будет обработано")
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
