#!/usr/bin/env python3
"""generate_cover_letters.py — генерация сопроводов через AITUNNEL.

Использует ds_cover_letter_digest.md как system_override + session_id для кэша.
Без CloakBrowser. Без hh.ru. Только LLM + запись в БД.

CLI:
    python3 scripts/generate_cover_letters.py --calibration
    python3 scripts/generate_cover_letters.py --ids 137767768,138550519
    python3 scripts/generate_cover_letters.py --all
    python3 scripts/generate_cover_letters.py --calibration --models deepseek-v3.2
    python3 scripts/generate_cover_letters.py --calibration --dry-run
    python3 scripts/generate_cover_letters.py --calibration --show
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

# Форсим построчный вывод
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

from config import load_settings                                    # noqa: E402
from database import Database                                      # noqa: E402
from ai_analyzer import VacancyAnalyzer, AnalysisError             # noqa: E402
from llm.managed import ManagedLLMProvider                         # noqa: E402
from llm.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402

DEFAULT_MODEL = "deepseek-v3.2"
DEFAULT_DIGEST = "docs/datasets/ds_cover_letter_digest.md"


def load_calibration_ids(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [v["id"] for v in data["positive"]] + [v["id"] for v in data["negative"]]


def fetch_vacancies(db_path: Path, ids: list[str] | None) -> list[dict]:
    """Только вакансии с непустым description. Только suitable=true (approved)."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        where = [
            "description != ''",
            "(status IN ('pending_approval', 'applied', 'approved') "
            " OR llm_decision = 1)",
        ]
        params: list = []
        if ids:
            placeholders = ", ".join("?" for _ in ids)
            where.append(f"id IN ({placeholders})")
            params.extend(ids)
        query = (
            "SELECT id, title, description, company, url "
            "FROM vacancies WHERE " + " AND ".join(where) +
            " ORDER BY discovered_at DESC"
        )
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def create_aitunnel_provider(settings, db):
    api_key = os.getenv("AITUNNEL_API_KEY", "").strip()
    base_url = os.getenv("AITUNNEL_BASE_URL", "https://api.aitunnel.ru/v1").strip()
    if not api_key:
        print("ERROR: AITUNNEL_API_KEY не найден в окружении", file=sys.stderr)
        return None
    adapter = OpenAICompatibleProvider(base_url, api_key, json_mode=False)
    return ManagedLLMProvider(
        adapter, db,
        max_retries=settings.llm.max_retries,
        max_requests_per_day=1000,
    )


async def run(args: argparse.Namespace) -> int:
    settings = load_settings(Path(args.env_file))
    db_path = Path(settings.database_path)
    db = Database(db_path)
    db.init()

    # system_override из дайджеста
    digest_path = Path(args.digest)
    if not digest_path.exists():
        print(f"ERROR: {digest_path} не найден", file=sys.stderr)
        return 2
    system_override = digest_path.read_text(encoding="utf-8")
    print(f"INFO: system_override из {digest_path} ({len(system_override)} символов)")

    # session_id для кэша
    session_id = args.session_id
    if not session_id:
        from datetime import datetime
        session_id = f"cover-letter-{datetime.now():%Y%m%d-%H%M%S}"
    print(f"INFO: session_id={session_id}")

    # Модель
    model = args.model
    print(f"INFO: model={model}")

    # Выборка вакансий
    if args.calibration:
        cal_path = REPO / "docs/datasets/calibration_set.json"
        ids = load_calibration_ids(cal_path)
        vacancies = fetch_vacancies(db_path, ids)
    elif args.ids:
        ids = [x.strip() for x in args.ids.split(",") if x.strip()]
        vacancies = fetch_vacancies(db_path, ids)
    elif args.all:
        vacancies = fetch_vacancies(db_path, None)
    else:
        print("ERROR: укажи --calibration / --ids / --all", file=sys.stderr)
        return 2

    if not vacancies:
        print("INFO: нет подходящих вакансий (нужно: description + suitable=true)")
        return 0

    print(f"INFO: вакансий для генерации: {len(vacancies)}")

    if args.dry_run:
        for v in vacancies:
            print(f"  {v['id']}  {v['title'][:60]}")
        print("\nDRY-RUN: ничего не сгенерировано")
        return 0

    # Провайдер
    provider = create_aitunnel_provider(settings, db)
    if provider is None:
        return 3
    analyzer = VacancyAnalyzer(settings, provider)

    done = errors = 0
    t0 = time.monotonic()

    try:
        for v in vacancies:
            vid = v["id"]
            try:
                letter = await analyzer.generate_cover_letter(
                    v["title"], v["description"],
                    company_name=v.get("company") or "",
                    vacancy_id=vid,
                    model_override=model,
                    system_override=system_override,
                    session_id=session_id,
                )
            except AnalysisError as exc:
                errors += 1
                print(f"  [err ] {vid} {exc.error_type}")
                continue
            except Exception as exc:
                errors += 1
                print(f"  [err ] {vid} {type(exc).__name__}: {exc}")
                continue

            if not letter.strip():
                errors += 1
                print(f"  [empty] {vid}")
                continue

            # Сохраняем в БД
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE vacancies SET cover_letter = ? WHERE id = ?",
                    (letter, vid),
                )
                conn.commit()

            done += 1
            print(f"  [ ok ] {vid} len={len(letter)}")
            if args.show:
                print(f"      ─── {v['title'][:60]}")
                for line in letter.split("\n")[:8]:
                    print(f"      {line[:110]}")
                print()

    finally:
        await provider.close()

    dt = time.monotonic() - t0
    print(f"\n=== DONE ===")
    print(f"  success={done}  errors={errors}")
    print(f"  elapsed={dt:.1f}s  avg={dt/max(done,1):.1f}s per call")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env-file", default=".env")
    p.add_argument("--calibration", action="store_true",
                   help="20 вакансий из calibration_set.json")
    p.add_argument("--ids", default=None, help="список vacancy_id через запятую")
    p.add_argument("--all", action="store_true",
                   help="все вакансии с description + suitable=true")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help=f"модель AITUNNEL (default: {DEFAULT_MODEL})")
    p.add_argument("--digest", default=DEFAULT_DIGEST,
                   help=f"путь к дайджесту (default: {DEFAULT_DIGEST})")
    p.add_argument("--session-id", default=None,
                   help="session_id для кэша (default: auto)")
    p.add_argument("--dry-run", action="store_true",
                   help="показать план без генерации")
    p.add_argument("--show", action="store_true",
                   help="печатать сгенерированные сопроводы")
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
