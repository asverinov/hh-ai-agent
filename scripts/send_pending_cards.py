#!/usr/bin/env python3
"""send_pending_cards.py — разово отправить карточки pending_approval в TG.

Чтобы не заспамить, по умолчанию limit=10. После просмотра — ещё 10 и т.д.

Запуск:
  PYTHONPATH=. python3 scripts/send_pending_cards.py --limit 10
  PYTHONPATH=. python3 scripts/send_pending_cards.py --limit 10 --offset 10
"""
import argparse
import asyncio
import logging
from pathlib import Path

from config import load_settings
from database import Database
from logging_setup import configure_logging
from tg_bot import AgentControl, TelegramService
from approval import ApprovalService

logger = logging.getLogger(__name__)


class _NoopSender:
    async def submit_application(self, permission) -> bool:
        return False


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--offset", type=int, default=0)
    args = ap.parse_args()

    settings = load_settings()
    db = Database(str(settings.database_path))
    db.init()

    control = AgentControl()
    approval_service = ApprovalService(settings, db, _NoopSender())
    telegram = TelegramService(settings, db, approval_service, control)

    with db._connect() as c:
        c.row_factory = __import__("sqlite3").Row
        rows = c.execute("""
            SELECT * FROM vacancies
            WHERE status='pending_approval' AND cover_letter != ''
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """, (args.limit, args.offset)).fetchall()

    logger.info("send_pending_cards n=%d offset=%d", len(rows), args.offset)
    sent = 0
    for row in rows:
        vacancy = db._vacancy(row)
        try:
            await telegram.send_preview(vacancy, include_actions=True)
            sent += 1
            await asyncio.sleep(1.0)  # rate limit TG
        except Exception as exc:
            logger.warning("send_preview_failed id=%s err=%s", row["id"], exc)

    logger.info("sent %d/%d cards", sent, len(rows))
    await telegram.stop()


if __name__ == "__main__":
    configure_logging(Path("send_pending_cards.log"))
    asyncio.run(main())
