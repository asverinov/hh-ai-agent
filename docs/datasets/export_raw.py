#!/usr/bin/env python3
"""Экспорт вакансий из БД в текстовый файл с блочным форматом.

Формат: см. docs/datasets/README.md
Идемпотентно: вакансии, уже выгруженные в файл, повторно не пишутся.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BEGIN = "@@@VACANCY_BEGIN@@@"
END = "@@@VACANCY_END@@@"

DEFAULT_DB = Path("agent.db")
DEFAULT_OUT_DIR = Path("docs/datasets")


def find_existing_ids(out_file: Path) -> set[str]:
    """Читает файл и возвращает id вакансий, уже записанных туда."""
    if not out_file.exists():
        return set()
    ids: set[str] = set()
    with out_file.open(encoding="utf-8") as f:
        for line in f:
            if line.startswith(BEGIN):
                parts = line.strip().split(maxsplit=1)
                if len(parts) == 2:
                    ids.add(parts[1])
    return ids


def fetch_vacancies(db: Path) -> list[dict]:
    """Возвращает все вакансии с непустым description и evaluation_json."""
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                v.id, v.title, v.url, v.company, v.status,
                v.description, v.evaluation_json, v.fit_summary,
                v.llm_decision, v.llm_reason, v.confidence,
                v.salary_from, v.salary_to, v.salary_currency,
                v.discovered_at,
                lr.model, lr.provider, lr.prompt_version, lr.raw_response,
                lr.latency_ms, lr.input_tokens, lr.output_tokens
            FROM vacancies v
            LEFT JOIN llm_requests lr
                ON lr.vacancy_id = v.id
               AND lr.operation = 'vacancy_analysis'
               AND lr.success = 1
            WHERE v.description != ''
              AND v.evaluation_json != ''
            ORDER BY v.discovered_at
            """
        ).fetchall()
    return [dict(r) for r in rows]


def render_block(v: dict) -> str:
    salary = ""
    if v.get("salary_from") or v.get("salary_to"):
        s_from = v.get("salary_from") or "?"
        s_to = v.get("salary_to") or "?"
        cur = v.get("salary_currency") or ""
        salary = f"{s_from}–{s_to} {cur}".strip()

    llm_dec = v.get("llm_decision")
    llm_dec_str = {0: "reject", 1: "approve", None: "n/a"}.get(llm_dec, str(llm_dec))

    parts = [
        f"{BEGIN} {v['id']}",
        f"TITLE: {v.get('title','')}",
        f"COMPANY: {v.get('company','')}",
        f"URL: {v.get('url','')}",
        f"STATUS: {v.get('status','')}",
        f"SALARY: {salary}",
        f"DISCOVERED_AT: {v.get('discovered_at','')}",
        "",
        f"PROMPT_VERSION: {v.get('prompt_version') or ''}",
        f"MODEL: {v.get('model') or ''}",
        f"PROVIDER: {v.get('provider') or ''}",
        f"LATENCY_MS: {v.get('latency_ms') or ''}",
        f"TOKENS_IN: {v.get('input_tokens') or ''}",
        f"TOKENS_OUT: {v.get('output_tokens') or ''}",
        "",
        "--- AGENT_VERDICT ---",
        f"llm_decision: {llm_dec_str}",
        f"llm_reason: {v.get('llm_reason','')}",
        f"confidence: {v.get('confidence') if v.get('confidence') is not None else ''}",
        f"fit_summary: {v.get('fit_summary','')}",
        "",
        "--- AGENT_EVALUATION_JSON ---",
        v.get("evaluation_json") or "",
        "",
        "--- DESCRIPTION ---",
        v.get("description") or "",
        "",
        "--- LLM_RAW_RESPONSE ---",
        v.get("raw_response") or "",
        "",
        f"{END} {v['id']}",
        "",
    ]
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--out-file",
        type=Path,
        default=None,
        help="Явное имя файла (по умолчанию raw_YYYYMMDD.txt)",
    )
    args = parser.parse_args()

    if not args.db.exists():
        print(f"ERROR: БД {args.db} не найдена", file=sys.stderr)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_file = args.out_file or args.out_dir / f"raw_{datetime.now():%Y%m%d}.txt"

    existing_ids = find_existing_ids(out_file)
    print(f"INFO: в файле {out_file.name} уже {len(existing_ids)} вакансий")

    rows = fetch_vacancies(args.db)
    print(f"INFO: в БД {len(rows)} вакансий с description + evaluation_json")

    to_add = [r for r in rows if r["id"] not in existing_ids]
    print(f"INFO: к добавлению {len(to_add)}")

    if not to_add:
        print("OK: нечего добавлять, файл актуален")
        return 0

    with out_file.open("a", encoding="utf-8") as f:
        for v in to_add:
            f.write(render_block(v))
            print(f"  + {v['id']}  {v.get('title','')[:60]}")

    print(f"\nOK: добавлено {len(to_add)}, всего в файле {len(existing_ids)+len(to_add)}")
    print(f"     {out_file}")
    print(f"     размер: {out_file.stat().st_size} байт")
    return 0


if __name__ == "__main__":
    sys.exit(main())
