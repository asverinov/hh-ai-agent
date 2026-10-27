import sqlite3
import shutil
from pathlib import Path
from datetime import datetime

db_path = Path("agent.db")
backup = db_path.parent / f"agent.db.bak-migration-{datetime.now().strftime('%Y%m%d-%H%M')}"
shutil.copy(db_path, backup)
print(f"OK: бэкап {backup}")

conn = sqlite3.connect(str(db_path))
cur = conn.cursor()

# 1. Проверка: поля уже есть?
cur.execute("PRAGMA table_info(vacancies)")
cols = {row[1] for row in cur.fetchall()}
print(f"OK: существующие колонки: {len(cols)}")

# 2. Добавить queued_at
if "queued_at" not in cols:
    cur.execute("ALTER TABLE vacancies ADD COLUMN queued_at TEXT")
    print("OK: добавлено queued_at")
else:
    print("SKIP: queued_at уже есть")

# 3. Добавить queue_priority
if "queue_priority" not in cols:
    cur.execute("ALTER TABLE vacancies ADD COLUMN queue_priority INTEGER DEFAULT 0")
    print("OK: добавлено queue_priority")
else:
    print("SKIP: queue_priority уже есть")

# 4. Индексы для быстрой выборки очередей
cur.execute("CREATE INDEX IF NOT EXISTS idx_queued_read ON vacancies(status, queued_at, description)")
cur.execute("CREATE INDEX IF NOT EXISTS idx_queued_apply ON vacancies(queue_priority, queued_at, status)")
print("OK: индексы созданы")

conn.commit()

# 5. Проверка
cur.execute("PRAGMA table_info(vacancies)")
new_cols = [row[1] for row in cur.fetchall()]
print(f"OK: колонки после миграции: {new_cols}")
cur.execute("PRAGMA integrity_check")
print(f"OK: integrity_check = {cur.fetchone()[0]}")

conn.close()
