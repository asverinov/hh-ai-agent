#!/usr/bin/env python3
"""browser_worker.py — единый браузерный процесс: apply (приоритет) + read_description.

Один CloakBrowser. Apply первым (queue_priority>=1), иначе read.
Капча в apply → STOP (VNC). В read → defer 60 мин + пауза 5 мин.
"""
import asyncio
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from approval import ApplicationPermission
from browser_backend import BrowserLaunchError, create_browser_backend
from config import load_settings
from database import Database, VacancyStatus
from hh_client import CaptchaDetectedError, HHClient, VacancyExpiredError, VacancySummary
from logging_setup import setup_logging

import logging
logger = logging.getLogger(__name__)

APPLY_COOLDOWN_SEC = 300
READ_SLEEP = (10, 40)
IDLE_SLEEP = 30


async def tg_notify(token: str, chat_id: int, text: str) -> None:
    if not token or not chat_id:
        return
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=aiohttp.ClientTimeout(total=10),
            )
    except Exception as exc:
        logger.warning("tg_notify_failed err=%s", exc)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


async def _apply_one(hh: HHClient, db: Database, settings, row: dict, notify) -> str:
    """Вернуть: 'ok' | 'expired' | 'error' | 'captcha'."""
    job_id = str(row["id"])
    title = row.get("title") or job_id
    permit = db.approve(job_id, settings.tg_user_id, settings.tg_user_id, _now(), 30)
    if not permit:
        logger.warning("approve_failed job_id=%s", job_id)
        db.defer_apply_queue(job_id, 1)
        return "error"
    permission = ApplicationPermission(job_id, permit, settings.tg_user_id)
    try:
        ok = await hh.submit_application(permission)
        if ok:
            await notify(f"✓ Отклик отправлен: {title}")
            return "ok"
        await notify(f"✗ Не удалось: {title}")
        db.defer_apply_queue(job_id, 5)
        return "error"
    except CaptchaDetectedError:
        logger.error("CAPTCHA при apply job_id=%s — STOP", job_id)
        await notify(f"⚠️ Капча на hh.ru (apply: {title}). Останавливаюсь, нужен VNC.")
        return "captcha"
    except VacancyExpiredError:
        db.mark_expired(job_id)
        await notify(f"⌛ Вакансия закрыта: {title}")
        return "expired"
    except Exception as exc:
        logger.warning("apply_error job_id=%s err=%s", job_id, exc)
        await notify(f"✗ Ошибка ({type(exc).__name__}): {title}")
        db.defer_apply_queue(job_id, 5)
        return "error"


async def main() -> None:
    settings = load_settings()
    db = Database(str(settings.database_path))
    db.init()

    backend = create_browser_backend(settings)
    context = await backend.start()
    hh = HHClient(settings, context=context)

    token = (settings.tg_bot_token or "").strip()
    chat_id = int(settings.tg_user_id or 0)
    async def notify(text): await tg_notify(token, chat_id, text)

    apply_streak_errors = 0
    read_streak_captcha = 0

    logger.info("browser_worker started")
    try:
        while True:
            # 1. Apply (приоритет)
            last = db.get_last_apply_at()
            if last:
                try:
                    last_dt = datetime.fromisoformat(last)
                    elapsed = (_now() - last_dt).total_seconds()
                    if elapsed < APPLY_COOLDOWN_SEC:
                        wait = int(APPLY_COOLDOWN_SEC - elapsed) + 1
                        logger.info("apply_cooldown wait=%ds", wait)
                        await asyncio.sleep(wait)
                        continue
                except Exception:
                    pass

            row = db.dequeue_apply()
            if row:
                result = await _apply_one(hh, db, settings, row, notify)
                if result == "captcha":
                    logger.error("STOP: captcha при apply")
                    break
                if result == "ok":
                    apply_streak_errors = 0
                    await asyncio.sleep(APPLY_COOLDOWN_SEC)
                else:
                    apply_streak_errors += 1
                    if apply_streak_errors >= 3:
                        logger.error("STOP: 3 подряд ошибки apply")
                        await notify("⚠️ 3 ошибки apply подряд. Останавливаюсь.")
                        break
                    await asyncio.sleep(30)
                continue

            # 2. Read description
            rrow = db.dequeue_read()
            if rrow:
                summary = VacancySummary(
                    id=str(rrow["id"]),
                    title=rrow.get("title") or "",
                    url=rrow.get("url") or f"https://hh.ru/vacancy/{rrow['id']}",
                    search_query="",
                )
                try:
                    details = await hh.read_vacancy(summary, None)
                    from hh_client import PageState
                    if details.state == PageState.VACANCY_LOADED:
                        db.update_description(
                            str(rrow["id"]), details.description or "",
                            details.company or "", details.company_url or "",
                        )
                        logger.info("read_ok id=%s len=%d", rrow["id"], len(details.description or ""))
                        read_streak_captcha = 0
                    elif details.state in (PageState.VACANCY_REMOVED, PageState.VACANCY_HIDDEN):
                        db.mark_expired(str(rrow["id"]))
                        logger.info("read_expired id=%s state=%s", rrow["id"], details.state.value)
                    elif details.state == PageState.CAPTCHA_DETECTED:
                        read_streak_captcha += 1
                        db.defer_read_queue(str(rrow["id"]), 60)
                        logger.warning("read_captcha id=%s streak=%d", rrow["id"], read_streak_captcha)
                        if read_streak_captcha >= 2:
                            logger.error("STOP: 2 капчи подряд в read")
                            await notify("⚠️ 2 капчи подряд в read. Останавливаюсь, нужен VNC.")
                            break
                        await asyncio.sleep(300)
                    else:
                        db.reset_read_queue(str(rrow["id"]))
                        logger.warning("read_unknown id=%s state=%s", rrow["id"], details.state.value)
                except Exception as exc:
                    logger.warning("read_error id=%s err=%s", rrow["id"], exc)
                    db.defer_read_queue(str(rrow["id"]), 30)
                await asyncio.sleep(random.uniform(*READ_SLEEP))
                continue

            # 3. Пусто
            await asyncio.sleep(IDLE_SLEEP)
    finally:
        try:
            await backend.close()
        except Exception:
            pass
        logger.info("browser_worker stopped")


if __name__ == "__main__":
    setup_logging()
    try:
        asyncio.run(main())
    except BrowserLaunchError as exc:
        logger.error("browser_launch_failed err=%s", exc)
        sys.exit(1)
