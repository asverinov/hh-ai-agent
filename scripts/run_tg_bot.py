#!/usr/bin/env python3
"""run_tg_bot.py — отдельный entrypoint для TelegramService (без main.py).

НЕ делает apply — только enqueue_apply. Apply делает browser_worker.

Запуск:
  PYTHONPATH=. python3 -u scripts/run_tg_bot.py
"""
import asyncio
import logging
import sys
from pathlib import Path

from approval import ApprovalService
from config import load_settings
from database import Database
from logging_setup import configure_logging
from tg_bot import AgentControl, TelegramService

logger = logging.getLogger(__name__)


class _NoopSender:
    """Заглушка PhysicalApplicationSender: tg_bot не делает apply, только enqueue."""

    async def submit_application(self, permission) -> bool:
        logger.warning("noop_sender_called job_id=%s — apply должен делать browser_worker",
                       getattr(permission, "job_id", "?"))
        return False


async def main() -> None:
    settings = load_settings()
    db = Database(str(settings.database_path))
    db.init()

    control = AgentControl()
    approval_service = ApprovalService(settings, db, _NoopSender())
    telegram = TelegramService(settings, db, approval_service, control)

    # Bootstrap-сообщение: полная статистика по статусам
    try:
        stats = db.stats()
        pending_count = db.count_pending()
        await telegram.notify_bootstrap(stats, pending_count)
    except Exception as exc:
        logger.warning("bootstrap_failed err=%s", exc)

    logger.info("tg_bot_polling_start")
    try:
        await telegram.start_polling()
    finally:
        await telegram.stop()


if __name__ == "__main__":
    configure_logging(Path("tg_bot.log"))
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
