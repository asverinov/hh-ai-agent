#!/usr/bin/env python3
"""browser_worker.py — единый браузерный процесс: apply (приоритет) + read_description.

Диспетчер: один CloakBrowser, один event loop.
- Если прошло >= APPLY_COOLDOWN_SEC с последнего apply и очередь apply не пуста — apply.
- Иначе, если прошла пауза с последнего read — read.
- Иначе — короткий сон до ближайшего события.

Паузы apply и read — это свойства задач, а не блокировки браузера:
во время 5-минутной паузы apply браузер крутит read (по одному чтению
каждые 10–40 секунд), а не спит.

Капча в apply → STOP (VNC). Капча в read → defer 60 мин + глобальная
пауза 5 мин; 2 подряд → STOP.
"""
import asyncio
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from approval import ApplicationPermission
from browser_backend import BrowserLaunchError, create_browser_backend
from config import load_settings
from database import Database, VacancyStatus
from hh_client import CaptchaDetectedError, HHClient, VacancyExpiredError, VacancySummary
from logging_setup import configure_logging

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


def _parse_dt(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _apply_one(hh: HHClient, db: Database, settings, row: dict, notify) -> str:
    """Вернуть: 'ok' | 'expired' | 'error' | 'captcha' | 'skip'."""
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
        _v = db.get(job_id)
        if _v is not None and _v.status.value == "skipped":
            await notify(f"⏭ Пропущено (анкета): {title}")
            return "skip"
        if _v is not None and "pre-response validation" in (_v.error_text or ""):
            logger.warning("pre_response_recheck job_id=%s — defer, не считаем за error", job_id)
            db.defer_apply_queue(job_id, 60)
            return "skip"
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

    from approval import ApprovalGuard
    backend = create_browser_backend(settings)
    context = await backend.start()
    guard = ApprovalGuard(settings, db)
    hh = HHClient(context, settings, db, guard)

    token = (settings.tg_bot_token or "").strip()
    chat_id = int(settings.tg_user_id or 0)
    async def notify(text): await tg_notify(token, chat_id, text)

    apply_streak_errors = 0
    apply_not_before: float = 0.0  # monotonic; apply blocked until then
    next_read_at: float | None = None  # monotonic; None → read сразу

    _reset = db.reset_all_applying()
    if _reset:
        logger.warning("reset_stale_applying: %d вакансий вернулись в pending", _reset)

    logger.info(
        "browser_worker started (dispatcher: apply priority, cooldown=%ds, read_pause=%ds-%ds)",
        APPLY_COOLDOWN_SEC, READ_SLEEP[0], READ_SLEEP[1],
    )
    try:
        while True:
            # ---------- Apply (приоритет) ----------
            apply_ready = time.monotonic() >= apply_not_before
            last_iso = db.get_last_apply_at()
            if last_iso:
                try:
                    elapsed = (_now() - _parse_dt(last_iso)).total_seconds()
                    if elapsed < APPLY_COOLDOWN_SEC:
                        apply_ready = False
                except Exception:
                    pass

            if apply_ready:
                row = db.dequeue_apply()
                if row:
                    job_id = str(row["id"])
                    logger.info("apply_start id=%s", job_id)
                    result = await _apply_one(hh, db, settings, row, notify)
                    if result == "captcha":
                        logger.error("STOP: captcha при apply")
                        break
                    if result == "ok":
                        apply_streak_errors = 0
                    else:
                        apply_streak_errors += 1
                        apply_not_before = time.monotonic() + APPLY_COOLDOWN_SEC
                        if apply_streak_errors >= 3:
                            logger.error("STOP: 3 подряд ошибки apply")
                            await notify("⚠️ 3 ошибки apply подряд. Останавливаюсь.")
                            break
                    continue

            # ---------- Read ----------
            if next_read_at is None or time.monotonic() >= next_read_at:
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
                        elif details.state in (PageState.VACANCY_REMOVED, PageState.VACANCY_HIDDEN):
                            db.mark_expired(str(rrow["id"]))
                            logger.info("read_expired id=%s state=%s", rrow["id"], details.state.value)
                        elif details.state == PageState.CAPTCHA_DETECTED:
                            logger.error(
                                "STOP: капча при read id=%s url=%s",
                                rrow["id"], summary.url,
                            )
                            await notify(
                                f"⚠️ Капча при read (id={rrow['id']}). Останавливаюсь, нужен VNC."
                            )
                            break
                        else:
                            db.reset_read_queue(str(rrow["id"]))
                            logger.warning("read_unknown id=%s state=%s", rrow["id"], details.state.value)
                    except Exception as exc:
                        logger.warning("read_error id=%s err=%s", rrow["id"], exc)
                        db.defer_read_queue(str(rrow["id"]), 30)
                    next_read_at = time.monotonic() + random.uniform(*READ_SLEEP)
                    continue

            # ---------- Idle: короткий сон до ближайшего события ----------
            sleep_for = float(IDLE_SLEEP)
            if next_read_at is not None:
                sleep_for = min(sleep_for, max(1.0, next_read_at - time.monotonic()))
            if last_iso:
                try:
                    apply_wait = APPLY_COOLDOWN_SEC - (_now() - _parse_dt(last_iso)).total_seconds()
                    if apply_wait > 0:
                        sleep_for = min(sleep_for, max(1.0, apply_wait))
                except Exception:
                    pass
            await asyncio.sleep(max(1.0, sleep_for))
    finally:
        try:
            await backend.close()
        except Exception:
            pass
        logger.info("browser_worker stopped")


if __name__ == "__main__":
    configure_logging(Path("browser_worker.log"))
    try:
        asyncio.run(main())
    except BrowserLaunchError as exc:
        logger.error("browser_launch_failed err=%s", exc)
        sys.exit(1)
