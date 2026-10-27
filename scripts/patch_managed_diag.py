import shutil, subprocess
from pathlib import Path
from datetime import datetime

def backup(p):
    shutil.copy(p, str(p) + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M"))

# === 1. MANAGED: диагностика + raw при ошибке ===
p = Path("llm/managed.py")
backup(p)
c = p.read_text(encoding="utf-8")

# 1a. warning при empty/truncated
old = '''                if not response.text.strip() or response.finish_reason == "length":
                    raise LLMInvalidResponseError()'''
new = '''                if not response.text.strip() or response.finish_reason == "length":
                    logger.warning(
                        "llm_empty_or_truncated provider=%s model=%s operation=%s "
                        "finish_reason=%r text_len=%s",
                        self.adapter.name, response.model, request.operation,
                        response.finish_reason, len(response.text or ""),
                    )
                    raise LLMInvalidResponseError()'''
if old in c:
    c = c.replace(old, new)
    print("OK: warning при empty/truncated")
else:
    print("WARN: empty/truncated блок не найден")

# 1b. сохранить raw_response при ошибке
old2 = '''                self.database.complete_llm_request(
                    row_id,
                    success=False,
                    finished_at=self._now(),
                    error_type=exc.category,
                )
                logger.warning('''
new2 = '''                _raw = ""
                try:
                    _raw = (response.text if response else "") or ""
                except Exception:
                    _raw = ""
                self.database.complete_llm_request(
                    row_id,
                    success=False,
                    finished_at=self._now(),
                    error_type=exc.category,
                    raw_response=_raw or None,
                )
                logger.warning('''
if old2 in c:
    c = c.replace(old2, new2)
    print("OK: raw_response сохраняется при ошибке")
else:
    print("WARN: except-блок не найден")

p.write_text(c, encoding="utf-8")

# === 2. AI_ANALYZER: увеличить max_output_tokens для cover letter ===
p2 = Path("ai_analyzer.py")
backup(p2)
c2 = p2.read_text(encoding="utf-8")
old3 = '''        request = self._request(
            system_instructions=system_text,
            payload={
                "candidate": candidate,
                "vacancy": vacancy,
                "cover_letter": {"style": cover.style, "language": cover.language},
            },
            operation="cover_letter",
            structured=False,
            vacancy_id=vacancy_id,
            model_override=model_override,
            session_id=session_id,
        )'''
new3 = '''        request = self._request(
            system_instructions=system_text,
            payload={
                "candidate": candidate,
                "vacancy": vacancy,
                "cover_letter": {"style": cover.style, "language": cover.language},
            },
            operation="cover_letter",
            structured=False,
            vacancy_id=vacancy_id,
            model_override=model_override,
            session_id=session_id,
        )
        # qwen3.7-flash может уйти в reasoning и упереться в 6000 токенов.
        # Даём 10000 на письмо — модель уложится в 1100 символов ответа.
        from dataclasses import replace as _replace
        request = _replace(request, max_output_tokens=10000)'''
if old3 in c2:
    c2 = c2.replace(old3, new3)
    print("OK: max_output_tokens=10000 для cover letter")
else:
    print("WARN: generate_cover_letter request блок не найден")

p2.write_text(c2, encoding="utf-8")

# Компиляция
for f in ["llm/managed.py", "ai_analyzer.py"]:
    r = subprocess.run(["python3", "-m", "py_compile", f], capture_output=True)
    if r.returncode != 0:
        print(f"ERROR {f}: {r.stderr.decode()}")
        raise SystemExit(1)
    print(f"OK: {f} компилируется")
