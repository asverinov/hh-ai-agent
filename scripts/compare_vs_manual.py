#!/usr/bin/env python3
"""compare_vs_manual.py — сравнение источников с ручной разметкой.

Считает precision/recall/F1 и accuracy каждого источника против Manual.
Учитывает только вакансии, где Manual = approve или reject (borderline
исключается — для них другая задача).

Manual — «золотой стандарт» (твоя ручная разметка).
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from compare_all import (  # noqa: E402
    ALL_COLS,
    MANUAL_LABELS,
    REF_PROMPT,
    REF_RULES,
    load_db_evaluations,
    load_ds_prompt,
    load_ds_rules,
    load_manual_labels,
)


def main() -> int:
    db = load_db_evaluations()
    ds_prompt = load_ds_prompt(REF_PROMPT)
    ds_rules = load_ds_rules(REF_RULES)
    manual = load_manual_labels(MANUAL_LABELS)

    # Только «чистые» — approve/reject (без borderline)
    clean_ids = sorted(vid for vid, v in manual.items() if v in ("approve", "reject"))
    if not clean_ids:
        print("ERROR: нет ручной разметки approve/reject", file=sys.stderr)
        return 1

    print(f"=== Manual: {len(clean_ids)} вакансий (approve/reject) ===")
    print(f"    {dict(Counter(manual[v] for v in clean_ids))}")
    print()

    # Матрица источников
    sources: dict[str, dict[str, bool | None]] = {}
    for m in ALL_COLS:
        sources[m] = {}
        for vid in clean_ids:
            if m == "Manual":
                sources[m][vid] = manual[vid] == "approve"
            elif m == "DS-prompt":
                sources[m][vid] = ds_prompt.get(vid)
            elif m == "DS-rules":
                sources[m][vid] = ds_rules.get(vid)
            else:
                sources[m][vid] = db.get(vid, {}).get(m)

    # Метрики
    manual_true = {vid: (manual[vid] == "approve") for vid in clean_ids}
    n_pos = sum(manual_true.values())
    n_neg = len(clean_ids) - n_pos

    print(f"=== Метрики против Manual (n={len(clean_ids)}, pos={n_pos}, neg={n_neg}) ===\n")
    print(f"{'source':<22} {'TP':>3} {'FP':>3} {'TN':>3} {'FN':>3} {'P':>7} {'R':>7} {'F1':>7} {'Acc':>7}")
    print("-" * 80)

    results = []
    for m in ALL_COLS:
        if m == "Manual":
            continue
        tp = fp = tn = fn = 0
        for vid in clean_ids:
            pred = sources[m][vid]
            gold = manual_true[vid]
            if pred is None:
                continue
            if pred and gold:
                tp += 1
            elif pred and not gold:
                fp += 1
            elif not pred and not gold:
                tn += 1
            else:
                fn += 1
        n = tp + fp + tn + fn
        if n == 0:
            continue
        p = tp / (tp + fp) if (tp + fp) else None
        r = tp / (tp + fn) if (tp + fn) else None
        f1 = 2 * p * r / (p + r) if (p and r) else None
        acc = (tp + tn) / n
        results.append((m, tp, fp, tn, fn, p, r, f1, acc, n))

    # Сортируем по F1 (None — в конец)
    results.sort(key=lambda x: (-(x[7] or 0), -x[8]))

    def f(x, fmt=".2f"):
        return f"{x:{fmt}}" if x is not None else "  n/a"

    for (m, tp, fp, tn, fn, p, r, f1, acc, n) in results:
        print(f"{m:<22} {tp:>3} {fp:>3} {tn:>3} {fn:>3} "
              f"{f(p):>7} {f(r):>7} {f(f1):>7} {f(acc):>7}")

    print()
    print("=== Легенда ===")
    print("  TP/FP/TN/FN — confusion matrix по 8 вакансиям")
    print("  P  = TP/(TP+FP)")
    print("  R  = TP/(TP+FN)")
    print("  F1 = 2PR/(P+R)")
    print("  Acc = (TP+TN)/N")

    # Детально по ошибкам
    print()
    print("=== Ошибки по источникам ===")
    for (m, *_rest) in results:
        errors = []
        for vid in clean_ids:
            pred = sources[m][vid]
            gold = manual_true[vid]
            if pred is None or pred == gold:
                continue
            err = "FP" if (pred and not gold) else "FN"
            errors.append(f"{vid}({err})")
        if errors:
            print(f"  {m:<22} {', '.join(errors)}")
        else:
            print(f"  {m:<22} — без ошибок ✅")

    return 0


if __name__ == "__main__":
    sys.exit(main())
