import shutil, subprocess
from pathlib import Path
from datetime import datetime

p = Path("approval.py")
shutil.copy(p, str(p) + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M"))
c = p.read_text(encoding="utf-8")

# 1. import asyncio (если нет)
if "import asyncio" not in c:
    # Вставляем ПОСЛЕ "import logging", чтобы не нарушить from __future__
    c = c.replace("import logging\n", "import asyncio\nimport logging\n", 1)
    print("OK: import asyncio added (after __future__)")

# 2. __init__: добавить поля cooldown + lock
old = """        self.now_factory = now_factory or (lambda: datetime.now().astimezone())"""
new = """        self.now_factory = now_factory or (lambda: datetime.now().astimezone())
        self._apply_cooldown_seconds = 300
        self._last_apply_at = None
        self._apply_lock = asyncio.Lock()"""
if old in c:
    c = c.replace(old, new, 1)
    print("OK: __init__ fields added")
else:
    print("WARN: __init__ anchor not found")

# 3. approve_and_apply: lock + cooldown
old2 = """        permit = self.database.approve(
            job_id,
            telegram_user_id,
            self.settings.tg_user_id,
            self.now_factory(),
            self.settings.approval_ttl_minutes,
        )
        if permit is None:
            logger.warning("application_blocked job_id=%s reason=approval_invalid", job_id)
            return ApprovalResult(False, "Approval is missing, expired, or already used")
        logger.info("approval_received job_id=%s", job_id)"""
new2 = """        async with self._apply_lock:
            now = self.now_factory()
            if self._last_apply_at is not None:
                elapsed = (now - self._last_apply_at).total_seconds()
                if elapsed < self._apply_cooldown_seconds:
                    wait = int(self._apply_cooldown_seconds - elapsed) + 1
                    logger.warning("application_blocked job_id=%s reason=rate_limit wait=%ds", job_id, wait)
                    return ApprovalResult(False, f"Подождите {wait} сек (защита hh.ru от спама)")
            permit = self.database.approve(
                job_id,
                telegram_user_id,
                self.settings.tg_user_id,
                now,
                self.settings.approval_ttl_minutes,
            )
            if permit is None:
                logger.warning("application_blocked job_id=%s reason=approval_invalid", job_id)
                return ApprovalResult(False, "Approval is missing, expired, or already used")
            self._last_apply_at = now
        logger.info("approval_received job_id=%s", job_id)"""
if old2 in c:
    c = c.replace(old2, new2, 1)
    print("OK: rate-limit in approve_and_apply")
else:
    print("WARN: approve_and_apply anchor not found")

p.write_text(c, encoding="utf-8")
r = subprocess.run(["python3", "-m", "py_compile", "approval.py"], capture_output=True)
if r.returncode != 0:
    print(r.stderr.decode())
    raise SystemExit(1)
print("DONE: approval.py patched")
