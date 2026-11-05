"""reconcile.py — сверка БД с API (similar_vacancies).

Вызывается:
  - search_worker.py — после каждого цикла (seen уже собран).
  - scripts/reconcile_vacancies.py — вручную.

NOT_TOUCH: applied/applying/apply_failed/skipped/approved/rejected_by_llm/rejected_by_filter.
TO_TOUCH: discovered/pending_approval/expired/irrelevant/analysis_failed.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone

from database import Database, VacancyStatus


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def merge_evaluation_json(existing: str, new_fields: dict) -> str:
    try:
        data = json.loads(existing) if existing else {}
    except (json.JSONDecodeError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.update(new_fields)
    return json.dumps(data, ensure_ascii=False)


TO_TOUCH = (
    VacancyStatus.DISCOVERED.value,
    VacancyStatus.PENDING_APPROVAL.value,
    VacancyStatus.EXPIRED.value,
    VacancyStatus.IRRELEVANT.value,
    VacancyStatus.ANALYSIS_FAILED.value,
)


def _revive_status(row: sqlite3.Row) -> str | None:
    """Определить статус при возврате. None = не возвращать (оставить как есть)."""
    ej_str = row["evaluation_json"] or ""
    try:
        ej = json.loads(ej_str) if ej_str else {}
    except (json.JSONDecodeError, TypeError):
        ej = {}
    if not isinstance(ej, dict):
        ej = {}

    # явный prefilter_audit (salary/exp/junior) — не возвращать
    if ej.get("prefilter_audit"):
        return None

    desc = row["description"] or ""
    if not desc:
        return VacancyStatus.DISCOVERED.value  # read_description

    if not ej_str:
        return VacancyStatus.DISCOVERED.value  # assess_worker

    llm_dec = ej.get("llm_decision")
    if llm_dec is None:
        llm_dec = row["llm_decision"]

    if llm_dec == 0:
        return None  # отвергнута LLM — не возвращать

    cover = row["cover_letter"] or ""
    if not cover:
        return VacancyStatus.DISCOVERED.value  # cover_letter_worker

    return VacancyStatus.PENDING_APPROVAL.value  # карточка уже есть


def apply_reconcile(
    db: Database, seen: dict, allowed_exp: set, dry_run: bool = False,
) -> dict:
    """Применить reconcile: все TO_TOUCH статусы vs seen.

    seen: {vid: {"experience_id": str|None, "resume_code": str}}
    Возвращает {"stats": {...}, "touched": [(vid, from_status, action), ...]}.
    """
    stats: Counter = Counter()
    touched: list[tuple[str, str, str]] = []

    with db._connect() as conn:
        conn.row_factory = sqlite3.Row
        ph = ",".join("?" * len(TO_TOUCH))
        rows = conn.execute(
            f"""SELECT id, title, status, evaluation_json, description,
                       cover_letter, llm_decision
                FROM vacancies WHERE status IN ({ph})""",
            TO_TOUCH,
        ).fetchall()

    for row in rows:
        vid = str(row["id"])
        status = row["status"]
        entry = seen.get(vid)

        if entry is None:
            # нет в API → irrelevant (только для "активных"; expired/irrelevant уже там)
            if status in (
                VacancyStatus.DISCOVERED.value,
                VacancyStatus.PENDING_APPROVAL.value,
                VacancyStatus.ANALYSIS_FAILED.value,
            ):
                stats["irrelevant"] += 1
                touched.append((vid, status, "irrelevant"))
                if not dry_run:
                    db.mark_irrelevant(
                        vid, reason="reconcile: not_in_similar",
                        evaluation_json=merge_evaluation_json(
                            row["evaluation_json"] or "",
                            {"reconcile": "not_in_similar", "reconciled_at": now_iso()},
                        ),
                    )
            else:
                stats["no_change_not_in_api"] += 1
            continue

        exp_id = entry.get("experience_id")

        if exp_id and allowed_exp and exp_id not in allowed_exp:
            # плохой опыт → rejected_by_filter
            stats["reject_exp"] += 1
            touched.append((vid, status, "reject_exp"))
            if not dry_run:
                db.reject_by_filter(
                    vid, reason=f"experience: {exp_id}",
                    evaluation_json=merge_evaluation_json(
                        row["evaluation_json"] or "",
                        {"experience_id": exp_id, "reconcile": "reject_experience",
                         "reconciled_at": now_iso()},
                    ),
                )
            continue

        # в API, опыт ок или неизвестен
        if status in (VacancyStatus.DISCOVERED.value, VacancyStatus.PENDING_APPROVAL.value):
            stats["backfill"] += 1
            touched.append((vid, status, "backfill"))
            if not dry_run:
                db.set_evaluation_json(
                    vid,
                    merge_evaluation_json(
                        row["evaluation_json"] or "",
                        {"experience_id": exp_id, "reconcile": "backfill",
                         "reconciled_at": now_iso()},
                    ),
                )
            continue

        # expired/irrelevant/analysis_failed — revive
        new_status = _revive_status(row)
        if new_status is None:
            stats["no_revive"] += 1
            touched.append((vid, status, "no_revive"))
            continue
        stats[f"revive_to_{new_status}"] += 1
        touched.append((vid, status, f"revive_to_{new_status}"))
        if not dry_run:
            db.revive_vacancy(
                vid, new_status=new_status, experience_id=exp_id,
                reconcile_reason=f"revived_from_{status}",
            )

    return {"stats": dict(stats), "touched": touched}
