#!/usr/bin/env python3
"""send_test_apply.py — отправка одного отклика через агента (проверка теневой блокировки).

Логика: discover → request_approval → approve (permit) → submit_application.
Текст берётся из аргумента или переменной.
"""
import argparse
import asyncio
import hashlib
import sys
from datetime import datetime, UTC
from pathlib import Path

# sys.path: ~/hh-ai-agent, не /tmp
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from config import load_settings
from database import Database, VacancyStatus
from browser_backend import create_browser_backend
from approval import ApprovalGuard, ApplicationPermission
from hh_client import HHClient, VacancySummary


VACANCY_ID = "138709577"
VACANCY_URL = f"https://hh.ru/vacancy/{VACANCY_ID}"
VACANCY_TITLE = "Вакансия МТС (проверка отклика)"
COMPANY = "МТС"
COVER_LETTER = """Добрый день!

Управляю процессами ITIL/ITSM: выстраиваю управление инцидентами и проблемами, провожу анализ корневых причин (RCA), веду базу известных ошибок и координирую корректирующие действия с техническими командами и поставщиками. Анализирую массив инцидентов, выявляю тренды и инициирую проблемные записи.

В ГАС «Выборы» для ЦИК России управление проблемами было частью моей зоны ответственности: проводил RCA по критическим и повторяющимся инцидентам, внедрял постоянные решения и вел базу известных ошибок. За счет анализа первопричин и корректирующих мероприятий снизил число повторных инцидентов на 26%. Выстроил мониторинг (Zabbix, Grafana) и регулярную отчетность по статусу проблем для руководства и заказчика.

Владею ITIL (Problem, Incident, Change, SLM), Jira, Confluence, Grafana, Zabbix, продвинутым Excel. Умею работать со статистикой инцидентов, строить дашборды и аргументировать решения цифрами. Понимаю принципы высоконагруженных систем и облачных сервисов. Готов погрузиться в SimpleOne и развивать зрелость ITSM-процессов.

Буду рад обсудить, чем мой опыт может быть полезен для ваших проектов.

С уважением,
Александр Сверинов"""


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="не отправлять, только подготовить")
    args = parser.parse_args()

    settings = load_settings()
    db = Database(settings.database_path)

    # 0. Preflight
    print(f"app_mode={settings.app_mode}")
    print(f"enable_real_apply={settings.enable_real_apply}")
    print(f"tg_user_id={settings.tg_user_id}")
    print(f"required_portfolio_url={settings.profile.cover_letter.required_portfolio_url!r}")
    print(f"closing={settings.profile.cover_letter.closing!r}")

    existing = db.get(VACANCY_ID)
    if existing is not None:
        print(f"Уже в БД: status={existing.status}, letter_len={len(existing.cover_letter or '')}")
        if existing.status == VacancyStatus.PENDING_APPROVAL.value:
            print("  → уже pending_approval, пропускаем request_approval")
        elif existing.status in (VacancyStatus.APPLIED.value, VacancyStatus.APPLYING.value):
            print(f"  ERROR: уже в статусе {existing.status}, не отправляем повторно")
            return
        else:
            print(f"  WARN: статус {existing.status}, требуется ручная проверка")
    else:
        # 1. discover
        print(f"discover {VACANCY_ID}")
        desc_hash = hashlib.sha256(COVER_LETTER.encode()).hexdigest()
        ok = db.discover(
            job_id=VACANCY_ID,
            title=VACANCY_TITLE,
            company=COMPANY,
            company_url="",
            url=VACANCY_URL,
            description_hash=desc_hash,
            search_query="manual-test",
            discovered_at=datetime.now(UTC),
            description="(manual test — no description read)",
        )
        if not ok:
            print("  ERROR: discover вернул False (вакансия уже существует?)")
            return
        print("  OK: discovered")

    # 2. request_approval (только если ещё не pending_approval)
    existing = db.get(VACANCY_ID)
    if existing.status != VacancyStatus.PENDING_APPROVAL.value:
        print("request_approval")
        ok = db.request_approval(
            job_id=VACANCY_ID,
            cover_letter=COVER_LETTER,
            llm_decision=True,
            llm_reason="manual test — проверка теневой блокировки",
            confidence=0.9,
            fit_summary="Manual test",
            evaluation_json='{"best_resume": "RP"}',
            now=datetime.now(UTC),
        )
        if not ok:
            print("  ERROR: request_approval вернул False")
            return
        print("  OK: pending_approval")

    # 3. approve → permit
    print("approve")
    permit = db.approve(
        job_id=VACANCY_ID,
        telegram_user_id=settings.tg_user_id,
        expected_user_id=settings.tg_user_id,
        now=datetime.now(UTC),
        ttl_minutes=30,
    )
    if not permit:
        print("  ERROR: approve вернул None (не pending? wrong user?)")
        return
    print(f"  OK: permit={permit[:8]}...")

    if args.dry_run:
        print("\nDRY-RUN: останавливаемся перед submit_application")
        # откатываем в pending (чтобы можно было повторить)
        with db._connect() as conn:
            conn.execute(
                "UPDATE vacancies SET status=?, permit_hash=NULL, approved_at=NULL, approval_expires_at=NULL WHERE id=?",
                (VacancyStatus.PENDING_APPROVAL.value, VACANCY_ID),
            )
        print("  status откачен в pending_approval")
        return

    # 4. submit_application
    print("\nsubmit_application (открывает hh.ru через CloakBrowser)")
    backend = create_browser_backend(settings)
    try:
        ctx = await backend.start()
        guard = ApprovalGuard(settings, db)
        client = HHClient(ctx, settings, db, guard)
        if not await client.ensure_login():
            print("  ERROR: ensure_login=False")
            return

        permission = ApplicationPermission(
            job_id=VACANCY_ID,
            permit=permit,
            telegram_user_id=settings.tg_user_id,
        )
        result = await client.submit_application(permission)
        print(f"submit_application result: {result}")

        if result:
            print("OK: отклик отправлен")
        else:
            print("FAIL: отклик НЕ отправлен (см. лог выше — reason)")

        await client.close()
    finally:
        await backend.close()


if __name__ == "__main__":
    asyncio.run(main())
