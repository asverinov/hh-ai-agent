#!/usr/bin/env python3
"""backfill_descriptions.py — догрузка description для pending без описания.

Для вакансий со status='pending_approval' и description='' открывает
страницу через CloakBrowser, парсит описание, сохраняет в БД.

Не запускает LLM. Только CloakBrowser + БД.

CLI:
    python3 scripts/backfill_descriptions.py --dry-run
    python3 scripts/backfill_descriptions.py --all
    python3 scripts/backfill_descriptions.py --ids 137767768,...
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

from config import load_settings                                    # noqa: E402
from database import Database                                      # noqa: E402
from browser_backend import create_browser_backend                # noqa: E402
from approval import ApprovalGuard                                 # noqa: E402
from hh_client import HHClient, VacancySummary, PageState          # noqa: E402


def fetch_pending_without_desc(
    db_path: Path, ids: list[str] | None = None,
) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        where = ["status = 'pending_approval'", "description = ''"]
        params: list = []
        if ids:
            placeholders = ", ".join("?" for _ in ids)
            where.append(f"id IN ({placeholders})")
            params.extend(ids)
        query = (
            "SELECT id, title, url, search_query, description_hash "
            "FROM vacancies WHERE " + " AND ".join(where) +
            " ORDER BY discovered_at"
        )
        return [dict(r) for r in conn.execute(query, params).fetchall()]


async def main(args: argparse.Namespace) -> int:
    settings = load_settings(Path(args.env_file))
    db_path = Path(settings.database_path)
    db = Database(db_path)
    db.init()

    ids = [x.strip() for x in args.ids.split(",")] if args.ids else None
    vacancies = fetch_pending_without_desc(db_path, ids)

    if not vacancies:
        print("INFO: нет pending без description")
        return 0

    print(f"INFO: вакансий для догрузки: {len(vacancies)}")
    for v in vacancies:
        print(f"  {v['id']}  {v['title'][:60]}")

    if args.dry_run:
        print("\nDRY-RUN: ничего не сделано")
        return 0

    backend = create_browser_backend(settings)
    context = None
    updated = failed = captcha = 0
    try:
        context = await backend.start()
        guard = ApprovalGuard(settings, db)
        client = HHClient(context, settings, db, guard)

        if not await client.ensure_login():
            print("ERROR: hh.ru login required", file=sys.stderr)
            return 2

        for v in vacancies:
            vid = v["id"]
            summary = VacancySummary(
                vid, v["title"], v["url"], v["search_query"] or "",
            )
            try:
                details = await client.read_vacancy(summary, None)
            except Exception as exc:
                failed += 1
                print(f"  [err ] {vid} {type(exc).__name__}: {exc}")
                continue

            if details.state is PageState.CAPTCHA_DETECTED:
                captcha += 1
                print(f"  [capt] {vid} — капча, пропуск")
                continue

            if details.state is not PageState.VACANCY_LOADED:
                failed += 1
                print(f"  [skip] {vid} state={details.state.value}")
                continue

            desc = details.description or ""
            if not desc.strip():
                failed += 1
                print(f"  [empty] {vid} description пустой")
                continue

            new_hash = hashlib.sha256(desc.encode()).hexdigest()
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE vacancies SET description = ?, description_hash = ? "
                    "WHERE id = ?",
                    (desc, new_hash, vid),
                )
                conn.commit()

            updated += 1
            print(f"  [ ok ] {vid} desc_len={len(desc)}")

    finally:
        if context is not None:
            try:
                await backend.close()
            except Exception:
                pass

    print(f"\n=== DONE ===")
    print(f"  updated={updated}  failed={failed}  captcha={captcha}")
    return 0


def cli() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env-file", default=".env")
    p.add_argument("--ids", default=None, help="через запятую")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--all", action="store_true", help="все pending без description")
    args = p.parse_args()
    # --all — это default в скрипте (все pending без desc)
    return asyncio.run(main(args))


if __name__ == "__main__":
    sys.exit(cli())
