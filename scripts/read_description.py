"""
read_description: берёт discovered с description='' и queued_at IS NULL,
читает через CloakBrowser, сохраняет description + company + company_url.
Не делает assess. Не трогает apply — арбитраж через flock + .browser-apply-pending.

Запуск:
  python3 scripts/read_description.py --limit 3 --dry-run
  python3 scripts/read_description.py --limit 3
  nohup env PYTHONPATH=. python3 scripts/read_description.py > /tmp/read.log 2>&1 &
"""
import argparse
import asyncio
import fcntl
import os
import random
import sys
from datetime import datetime
from pathlib import Path

from config import load_settings
from database import Database
from approval import ApprovalGuard
from browser_backend import create_browser_backend
from hh_client import HHClient, PageState, VacancySummary

LOG = Path(f"read_description_{datetime.now().strftime('%Y%m%d_%H%M')}.log")
ARBITER_LOCK = Path(".browser-arbiter.lock")
APPLY_PENDING = Path(".browser-apply-pending")
BROWSER_PROFILE = Path(".browser-profile")
SLEEP_BETWEEN = (3, 30)
CAPTCHA_STOP_AFTER = 2
CAPTCHA_PAUSE_SEC = 300
SLEEP_APPLY_WAIT = 5


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def cleanup_stale_chromium() -> None:
    """Удалить SingletonLock, если процесса Chromium нет (после kill -9)."""
    lock = BROWSER_PROFILE / "SingletonLock"
    if not lock.exists():
        return
    try:
        target = os.readlink(lock)
        pid = int(target.rsplit("-", 1)[-1])
    except (OSError, ValueError):
        # не смогли прочитать — удаляем на всякий случай (лучше, чем висеть)
        log(f"cleanup: не удалось разобрать {lock}, удаляю")
        try:
            lock.unlink()
        except OSError as e:
            log(f"cleanup: unlink failed: {e}")
        return
    try:
        os.kill(pid, 0)
        log(f"cleanup: Chromium pid={pid} жив, lock не трогаю")
    except ProcessLookupError:
        log(f"cleanup: Chromium pid={pid} мёртв, удаляю lock")
        try:
            lock.unlink()
        except OSError as e:
            log(f"cleanup: unlink failed: {e}")


async def arbiter_acquire() -> int:
    """Эксклюзивный flock на .browser-arbiter.lock (blocking, в отдельном потоке)."""
    fd = os.open(str(ARBITER_LOCK), os.O_CREAT | os.O_RDWR, 0o644)
    await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_EX)
    return fd


def arbiter_release(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


async def wait_apply_idle() -> None:
    """Ждать, пока apply не отпустит браузер (.browser-apply-pending)."""
    while APPLY_PENDING.exists():
        log("apply pending, ждём 5 сек")
        await asyncio.sleep(SLEEP_APPLY_WAIT)


def mark_expired(db: Database, vid: str, state: PageState) -> None:
    """Прямой UPDATE, как в fetch_descr_and_regen.py:182-187 (transition не гарантирует 'expired')."""
    with db._connect() as connection:
        connection.execute(
            "UPDATE vacancies SET status='expired', llm_reason=? WHERE id=? AND status='discovered'",
            (f"read_description: {state.value}", vid),
        )


async def read_one(hh: HHClient, db: Database, row: dict, dry_run: bool) -> str:
    """Возвращает статус: ok | expired | skip | retry | fail."""
    vid = row["id"]
    summary = VacancySummary(id=vid, title=row["title"], url=row["url"], search_query="")
    try:
        details = await hh.read_vacancy(summary, None)
    except Exception as e:
        log(f"FAIL read_vacancy {vid}: {type(e).__name__}: {str(e)[:150]}")
        if not dry_run:
            db.reset_read_queue(vid)
        return "retry"

    state = details.state
    desc = (details.description or "").strip()

    if state in (PageState.VACANCY_REMOVED, PageState.VACANCY_HIDDEN, PageState.ACCESS_DENIED):
        if not dry_run:
            mark_expired(db, vid, state)
        log(f"EXPIRED {vid} state={state.value}")
        return "expired"

    if state is PageState.CAPTCHA_DETECTED:
        log(f"CAPTCHA {vid} state={state.value} desc_len={len(desc)}")
        if not dry_run:
            db.defer_read_queue(vid, 60)
        return "captcha"

    if state is not PageState.VACANCY_LOADED:
        log(f"RETRY {vid} state={state.value} desc_len={len(desc)}")
        if not dry_run:
            db.reset_read_queue(vid)
        return "retry"

    if not desc:
        log(f"RETRY {vid} empty description")
        if not dry_run:
            db.reset_read_queue(vid)
        return "retry"

    if dry_run:
        log(f"DRY {vid} desc_len={len(desc)} company={details.company!r}")
        return "ok"

    ok = db.update_description(
        vid, desc, details.company or "", getattr(details, "company_url", "") or "",
    )
    if not ok:
        log(f"FAIL update_description {vid} (row not found)")
        return "fail"
    log(f"OK {vid} desc_len={len(desc)} company={details.company!r}")
    return "ok"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0=бесконечно, N=обработать N и выйти")
    ap.add_argument("--once", action="store_true", help="один проход по очереди и выход")
    ap.add_argument("--dry-run", action="store_true", help="не писать в БД")
    args = ap.parse_args()

    settings = load_settings()
    db = Database(str(settings.database_path))
    db.init()

    cleanup_stale_chromium()

    backend = create_browser_backend(settings)
    context = await backend.start()
    hh = None
    fd = None
    try:
        guard = ApprovalGuard(settings, db)
        hh = HHClient(context, settings, db, guard)
        if not await hh.ensure_login():
            log("FATAL: hh.ru login required — залогинься в VNC, ENTER")
            sys.exit(1)

        processed = 0
        captcha_streak = 0
        stats = {"ok": 0, "expired": 0, "retry": 0, "skip": 0, "fail": 0, "captcha": 0}
        while True:
            await wait_apply_idle()
            row = db.dequeue_read()
            if row is None:
                if args.once:
                    log("очередь пуста, --once → выход")
                    break
                log("очередь пуста, пауза 60 сек")
                await asyncio.sleep(60)
                continue

            fd = await arbiter_acquire()
            try:
                # повторная проверка после получения lock (по плану, раздел 3)
                if APPLY_PENDING.exists():
                    if not args.dry_run:
                        db.reset_read_queue(row["id"])
                    log(f"apply started meanwhile, {row['id']} вернули в очередь")
                    continue
                res = await read_one(hh, db, row, args.dry_run)
                stats[res] = stats.get(res, 0) + 1
                processed += 1
                if res == "captcha":
                    captcha_streak += 1
                    log(f"captcha_streak={captcha_streak}")
                    if captcha_streak >= CAPTCHA_STOP_AFTER:
                        log(f"STOP: {CAPTCHA_STOP_AFTER} капчи подряд — нужно вмешательство (VNC)")
                        break
                    log(f"пауза {CAPTCHA_PAUSE_SEC} сек после капчи")
                    await asyncio.sleep(CAPTCHA_PAUSE_SEC)
                else:
                    if res == "ok":
                        captcha_streak = 0
                log(f"progress: {processed} {stats}")
            finally:
                arbiter_release(fd)
                fd = None

            if args.limit and processed >= args.limit:
                log(f"--limit {args.limit} достигнут, выход")
                break
            if args.once:
                # один проход: после первой же успешной/неуспешной обработки
                if row is None:
                    break
            await asyncio.sleep(random.uniform(*SLEEP_BETWEEN))

        log(f"ГОТОВО: {stats}")
    finally:
        if fd is not None:
            arbiter_release(fd)
        if hh is not None:
            try:
                await hh.close()
            except Exception:
                pass
        try:
            await backend.close()
        except Exception:
            try:
                await context.close()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
