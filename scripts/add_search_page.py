import shutil, subprocess
from pathlib import Path
from datetime import datetime

p = Path("hh_client.py")
shutil.copy(p, str(p) + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M"))
c = p.read_text(encoding="utf-8")

if "async def search_page" in c:
    print("SKIP: search_page уже есть")
    raise SystemExit(0)

# Найти конец search_vacancies_by_resume (перед _read_active_resume или другим def)
# Вставим новый метод после search_vacancies_by_resume
anchor = "    def _read_active_resume(self)"
new_method = '''    async def search_page(
        self, resume_id: str, page: int = 0, per_page: int = 100
    ) -> dict[str, Any]:
        """Один API-запрос similar_vacancies (без фильтров, без seen).

        Возвращает сырой JSON: {items, pages, found, page, per_page}.
        """
        if not await self._refresh_token_if_needed():
            raise RuntimeError("auth_error")
        session = await self._get_http_session()
        url = f"https://api.hh.ru/resumes/{resume_id}/similar_vacancies"
        params = {"page": page, "per_page": per_page}
        headers = {"Authorization": f"Bearer {self._access_token}"}
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status == 403:
                text = await resp.text()
                raise RuntimeError(f"forbidden: {text[:200]}")
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"http {resp.status}: {text[:200]}")
            return await resp.json()

    def _read_active_resume(self)'''

if anchor not in c:
    print("ERROR: anchor '_read_active_resume' не найден")
    raise SystemExit(1)

c = c.replace(anchor, new_method, 1)
p.write_text(c, encoding="utf-8")
print("OK: search_page добавлен")

r = subprocess.run(["python3", "-m", "py_compile", "hh_client.py"], capture_output=True)
if r.returncode != 0:
    print(r.stderr.decode())
    shutil.copy(str(p) + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M"), p)
    raise SystemExit(1)
print("OK: hh_client.py компилируется")
