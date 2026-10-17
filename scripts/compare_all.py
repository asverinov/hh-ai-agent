#!/usr/bin/env python3
"""compare_all.py — сведение оценок всех моделей по калибровочному набору.

Источники:
  1. 4 GigaChat модели из таблицы evaluations (is_calibration=1)
  2. DeepSeek (prompt-формат) из calibration_reference_prompt_*.txt
  3. DeepSeek (правила с Фибоначчи) из calibration_reference_rules_*.txt

Вывод:
  - единая матрица id × модель (True/False)
  - consensus (сколько из 6 согласны с большинством)
  - pairwise agreement (15 пар)
  - список спорных
  - CSV для дальнейшего анализа
"""

from __future__ import annotations

import csv
import re
import sqlite3
import sys
from collections import Counter
from itertools import combinations
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "agent.db"
REF_PROMPT = REPO / "docs/datasets/calibration_reference_prompt_20261017.txt"
REF_RULES = REPO / "docs/datasets/calibration_reference_rules_20261017.txt"
MANUAL_LABELS = REPO / "docs/datasets/manual_labels_20261017.txt"
OUT_CSV = REPO / "docs/datasets/calibration_full_matrix.csv"

GIGA_MODELS = ["GigaChat-2-Pro", "GigaChat-2-Max", "GigaChat-3-Pro", "GigaChat-3-Ultra"]
AITUNNEL_MODELS = ["deepseek-v4.1-flash", "deepseek-v3.2", "qwen3.7-flash"]
DB_MODELS = GIGA_MODELS + AITUNNEL_MODELS
ALL_COLS = DB_MODELS + ["DS-prompt", "DS-rules", "Manual"]


def load_db_evaluations() -> dict[str, dict]:
    """{vacancy_id: {model: bool}} — все модели из evaluations (is_calibration=1)."""
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT vacancy_id, model, suitable
            FROM evaluations WHERE is_calibration = 1
        """).fetchall()
    data: dict[str, dict] = {}
    for r in rows:
        vid = r["vacancy_id"]
        if vid not in data:
            data[vid] = {}
        data[vid][r["model"]] = bool(r["suitable"])
    return data


def load_ds_prompt(path: Path) -> dict[str, bool]:
    """Парсит @@@REFERENCE_BEGIN@@@ <id> / SUITABLE: true|false."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    result: dict[str, bool] = {}
    for block in re.split(r"@@@REFERENCE_BEGIN@@@\s+", text)[1:]:
        lines = block.split("\n", 1)
        vid = lines[0].strip().split()[0]
        m = re.search(r"^SUITABLE:\s*(true|false)", block, re.MULTILINE | re.IGNORECASE)
        if m:
            result[vid] = m.group(1).lower() == "true"
    return result


def load_ds_rules(path: Path) -> dict[str, bool]:
    """Парсит markdown: '1. <id> — ...' + следующую 'Вердикт: ...'."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    result: dict[str, bool] = {}

    current_id: str | None = None
    for line in text.split("\n"):
        # Заголовок вакансии: "1. 136204304 — ВТБ..."
        m = re.match(r"^\s*\d+\.\s*(\d{6,})\s*—", line)
        if m:
            current_id = m.group(1)
            continue
        # Вердикт
        m = re.match(r"^\s*Вердикт:\s*(.+)$", line)
        if m and current_id:
            verdict = m.group(1).lower().strip()
            if "не подаваться" in verdict:
                result[current_id] = False
            elif "подаваться" in verdict:  # включает "подаваться с оговорками"
                result[current_id] = True
            current_id = None  # сбрасываем
    return result


def load_manual_labels(path: Path) -> dict[str, str]:
    """{vacancy_id: 'approve'|'reject'|'borderline'}."""
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 2:
            vid, verdict = parts[0], parts[1].lower()
            if verdict in ("approve", "reject", "borderline"):
                result[vid] = verdict
    return result


def fmt(v: bool | None) -> str:
    if v is None:
        return "  —  "
    return "✅" if v else "❌"


def main() -> int:
    db = load_db_evaluations()
    ds_prompt = load_ds_prompt(REF_PROMPT)
    ds_rules = load_ds_rules(REF_RULES)
    manual = load_manual_labels(MANUAL_LABELS)

    print(f"INFO: DB вакансий с оценками: {len(db)}")
    for m in DB_MODELS:
        n = sum(1 for v in db.values() if m in v)
        print(f"  {m}: {n}")
    print(f"INFO: DS-prompt вакансий: {len(ds_prompt)}")
    print(f"INFO: DS-rules вакансий: {len(ds_rules)}")
    print(f"INFO: Manual labels: {len(manual)}")
    if manual:
        from collections import Counter
        print(f"  распределение: {dict(Counter(manual.values()))}")

    all_ids = sorted(set(db) | set(ds_prompt) | set(ds_rules))
    if not all_ids:
        print("ERROR: пусто", file=sys.stderr)
        return 1

    # Матрица
    matrix: dict[str, dict[str, bool | None]] = {}
    for vid in all_ids:
        row: dict[str, bool | None] = {}
        for m in DB_MODELS:
            row[m] = db.get(vid, {}).get(m)
        row["DS-prompt"] = ds_prompt.get(vid)
        row["DS-rules"] = ds_rules.get(vid)
        mv = manual.get(vid)
        if mv == "approve":
            row["Manual"] = True
        elif mv == "reject":
            row["Manual"] = False
        elif mv == "borderline":
            # borderline считаем как «нет» для production, но отдельно логируем
            row["Manual"] = None
        else:
            row["Manual"] = None
        matrix[vid] = row

    # Заголовки
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        titles = {
            r["id"]: r["title"][:42]
            for r in conn.execute("SELECT id, title FROM vacancies WHERE id IN (%s)"
                                  % ",".join("?" * len(all_ids)), all_ids)
        }

    # Печать
    header = f"{'id':<12} {'title':<42}"
    for m in ALL_COLS:
        header += f" {m.replace('GigaChat-', '').replace('DS-', 'DS'):>8}"
    header += "  consensus"
    print()
    print(header)
    print("-" * len(header))

    stats_agree = Counter()  # сколько раз модели согласны (N/6)
    dissent_ids: list[str] = []

    for vid in all_ids:
        row = matrix[vid]
        cells = [row[m] for m in ALL_COLS]
        present = [x for x in cells if x is not None]
        line = f"{vid:<12} {titles.get(vid, ''):<42}"
        for v in cells:
            line += f" {fmt(v):>8}"

        if present:
            cnt = Counter(present)
            majority, n_maj = cnt.most_common(1)[0]
            n_total = len(present)
            line += f"  {n_maj}/{n_total}"
            stats_agree[n_maj] += 1
            if n_maj < n_total:
                dissent_ids.append(vid)
        else:
            line += "  0/0"

        print(line)

    # Сводка консенсуса
    print()
    print("=== Консенсус ===")
    total_sources = len(ALL_COLS)
    for k in sorted(stats_agree.keys(), reverse=True):
        print(f"  {k}/{total_sources} согласны: {stats_agree[k]} вакансий")

    # Pairwise agreement
    print()
    print("=== Pairwise agreement (по всем общим вакансиям) ===")
    pairs: list[tuple[str, str, float, int]] = []
    for a, b in combinations(ALL_COLS, 2):
        total = 0
        agree = 0
        for vid in all_ids:
            va = matrix[vid].get(a)
            vb = matrix[vid].get(b)
            if va is None or vb is None:
                continue
            total += 1
            if va == vb:
                agree += 1
        if total:
            pct = 100.0 * agree / total
            pairs.append((a, b, pct, total))
    pairs.sort(key=lambda x: -x[2])
    for a, b, pct, n in pairs:
        print(f"  {a:<18} vs {b:<18}  {pct:5.1f}% ({n} общих)")

    # Спорные вакансии
    if dissent_ids:
        print()
        print(f"=== Спорные вакансии ({len(dissent_ids)}) ===")
        for vid in dissent_ids:
            row = matrix[vid]
            cells_str = " ".join(f"{m.split('-')[-1]}={fmt(row[m])}" for m in ALL_COLS if row[m] is not None)
            print(f"  {vid}  {titles.get(vid, '')[:50]:<50}  {cells_str}")

    # CSV
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["vacancy_id", "title"] + ALL_COLS + ["consensus_n", "consensus_total"])
        for vid in all_ids:
            row = matrix[vid]
            cells = [row[m] for m in ALL_COLS]
            present = [x for x in cells if x is not None]
            if present:
                cnt = Counter(present)
                _, n_maj = cnt.most_common(1)[0]
                n_total = len(present)
            else:
                n_maj, n_total = 0, 0
            writer.writerow(
                [vid, titles.get(vid, "")]
                + [("" if v is None else ("1" if v else "0")) for v in cells]
                + [n_maj, n_total]
            )
    print()
    print(f"OK: CSV → {OUT_CSV}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
