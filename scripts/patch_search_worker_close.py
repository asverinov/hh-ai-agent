import shutil, subprocess
from pathlib import Path
from datetime import datetime

p = Path("scripts/search_worker.py")
shutil.copy(p, str(p) + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M"))
c = p.read_text(encoding="utf-8")

if "await hh.close()" in c:
    print("SKIP: hh.close() уже есть")
    raise SystemExit(0)

# Заменяем блок main() — оборачиваем while в try/finally
old = '''    hh = HHClient(None, s, db, approval_guard=None)  # type: ignore[arg-type]

    while True:
        cycle_start = datetime.now()
        total_new = 0
        for code in RESUME_CODES:
            new, pages = await search_one_resume(hh, db, code, args.limit)
            total_new += new
            log(f"ИТОГ {code}: new={new} pages={pages}")
        elapsed = (datetime.now() - cycle_start).total_seconds()
        log(f"ЦИКЛ ЗАВЕРШЁН: new={total_new} за {elapsed:.0f} сек")
        if args.once:
            break
        log(f"Пауза {SLEEP_AFTER_CYCLE} сек до следующего цикла")
        await asyncio.sleep(SLEEP_AFTER_CYCLE)'''

new = '''    hh = HHClient(None, s, db, approval_guard=None)  # type: ignore[arg-type]
    try:
        while True:
            cycle_start = datetime.now()
            total_new = 0
            for code in RESUME_CODES:
                new, pages = await search_one_resume(hh, db, code, args.limit)
                total_new += new
                log(f"ИТОГ {code}: new={new} pages={pages}")
            elapsed = (datetime.now() - cycle_start).total_seconds()
            log(f"ЦИКЛ ЗАВЕРШЁН: new={total_new} за {elapsed:.0f} сек")
            if args.once:
                break
            log(f"Пауза {SLEEP_AFTER_CYCLE} сек до следующего цикла")
            await asyncio.sleep(SLEEP_AFTER_CYCLE)
    finally:
        try:
            await hh.close()
            log("HHClient закрыт")
        except Exception as e:
            log(f"close failed: {e}")'''

if old not in c:
    print("ERROR: блок main() не найден. Возможно, отличается.")
    print("Ищи в файле:")
    print("  grep -n 'HHClient(None' scripts/search_worker.py")
    print("  grep -n 'SLEEP_AFTER_CYCLE' scripts/search_worker.py")
    raise SystemExit(1)

c = c.replace(old, new, 1)
p.write_text(c, encoding="utf-8")
print("OK: try/finally с hh.close() добавлен")

r = subprocess.run(["python3", "-m", "py_compile", "scripts/search_worker.py"], capture_output=True)
if r.returncode != 0:
    print(r.stderr.decode())
    shutil.copy(str(p) + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M"), p)
    print("ROLLBACK")
    raise SystemExit(1)
print("OK: search_worker.py компилируется")
