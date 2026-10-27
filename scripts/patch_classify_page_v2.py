import shutil, subprocess
from pathlib import Path
from datetime import datetime

p = Path("hh_client.py")
shutil.copy(p, str(p) + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M"))
c = p.read_text(encoding="utf-8")

# 1. import re
if not any(line.strip() == "import re" for line in c.split("\n")[:20]):
    c = c.replace("import logging\n", "import logging\nimport re\n", 1)
    print("OK: import re")

# 2. Заменить блок is_visible + fallback
old = '''        for state, selectors in checks:
            for selector in selectors:
                if await page.locator(selector).is_visible():
                    return state

        if hasattr(page, "wait_for_timeout"):
            for fallback_selector, state in (
                ('[data-qa="vacancy-description"]', PageState.VACANCY_LOADED),
                ('.vacancy-description', PageState.VACANCY_LOADED),
                ('[data-qa="vacancy-removed"]', PageState.VACANCY_REMOVED),
                ('[data-qa="captcha"]', PageState.CAPTCHA_DETECTED),
            ):
                try:
                    loc = page.locator(fallback_selector).first
                    await loc.wait_for(state="visible", timeout=2000)
                    return state
                except Exception:
                    pass

        return PageState.PAGE_STRUCTURE_CHANGED'''

new = '''        for state, selectors in checks:
            for selector in selectors:
                try:
                    if await page.locator(selector).count() > 0:
                        return state
                except Exception:
                    continue

        # Fallback 1: inner_text (ловит архив/скрытые, игнорирует \\xa0 и разбивку)
        try:
            body_text = await page.locator("body").inner_text(timeout=5000)
        except Exception:
            body_text = ""
        if body_text:
            if re.search(
                r"ваканси[яю]\\s+в\\s+архиве|в\\s+архиве\\s+с\\s+\\d|не\\s+принимает\\s+отклик|работодатель[,\\s]+вероятно[,\\s]+уже\\s+нашел",
                body_text, re.IGNORECASE,
            ):
                return PageState.VACANCY_REMOVED
            if re.search(r"вам\\s+недоступна\\s+эта\\s+ваканси", body_text, re.IGNORECASE):
                return PageState.VACANCY_HIDDEN

        # Fallback 2: короткий wait_for по ключевым селекторам
        if hasattr(page, "wait_for_timeout"):
            for fallback_selector, state in (
                ('[data-qa="vacancy-description"]', PageState.VACANCY_LOADED),
                ('.vacancy-description', PageState.VACANCY_LOADED),
                ('[data-qa="vacancy-removed"]', PageState.VACANCY_REMOVED),
                ('[data-qa="vacancy-archived"]', PageState.VACANCY_REMOVED),
                ('[data-qa="captcha"]', PageState.CAPTCHA_DETECTED),
            ):
                try:
                    loc = page.locator(fallback_selector).first
                    await loc.wait_for(state="visible", timeout=2000)
                    return state
                except Exception:
                    pass

        return PageState.PAGE_STRUCTURE_CHANGED'''

if old not in c:
    print("ERROR: блок не найден. Возможно уже пропатчено или отличается.")
    raise SystemExit(1)

c = c.replace(old, new, 1)
p.write_text(c, encoding="utf-8")
print("OK: classify_page пропатчен")

r = subprocess.run(["python3", "-m", "py_compile", "hh_client.py"], capture_output=True)
if r.returncode != 0:
    print(r.stderr.decode())
    shutil.copy(str(p) + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M"), p)
    raise SystemExit(1)
print("OK: hh_client.py компилируется")
