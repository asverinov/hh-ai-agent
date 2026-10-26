import subprocess
import shutil
from pathlib import Path

def backup(p):
    shutil.copy(p, p + ".bak-cascade")

def rollback():
    for p in ["config.py", "ai_analyzer.py", "main.py"]:
        if Path(p + ".bak-cascade").exists():
            shutil.copy(p + ".bak-cascade", p)
    print("❌ ОШИБКА: Изменения откачены.")

def check():
    r = subprocess.run(["python3", "-m", "py_compile", "config.py", "ai_analyzer.py", "main.py"], capture_output=True)
    if r.returncode != 0:
        print(r.stderr.decode())
        return False
    return True

# 1. PATCH CONFIG.PY
def patch_config():
    p = Path("config.py")
    c = p.read_text()
    
    # Add dataclasses
    dc = '''@dataclass(frozen=True)
class ResumeProfile:
    title: str
    summary: str
    full_text: str
    project_keys: tuple[str, ...]
    tech_focus: tuple[str, ...]
    domains: tuple[str, ...]
    grade_markers: tuple[str, ...]
    forbidden_facts: tuple[str, ...]

@dataclass(frozen=True)
class Stage0Rule:
    keywords: tuple[str, ...]
    candidates: tuple[str, ...]

@dataclass(frozen=True)
class Profile:
'''
    if "class ResumeProfile" not in c:
        c = c.replace("@dataclass(frozen=True)\nclass Profile:", dc)
    
    # Add to Profile
    if "resumes: dict[str, ResumeProfile]" not in c:
        c = c.replace("    excluded_keywords: tuple[str, ...]\n", "    excluded_keywords: tuple[str, ...]\n    resumes: dict[str, ResumeProfile] = field(default_factory=dict)\n    stage0_rules: tuple[Stage0Rule, ...] = ()\n")
        
    # Add parsing (BEFORE def section to avoid try-block indentation errors)
    anchor_parse = "    def section(name: str) -> Mapping[str, object]:"
    parse = '''    # --- Parse resumes and stage0_rules ---
    parsed_resumes = {}
    raw_resumes = raw_profile.get("resumes", {})
    if isinstance(raw_resumes, dict):
        for r_code, r_data in raw_resumes.items():
            if not isinstance(r_data, dict): continue
            ft_path = Path(f"data/resumes/resume_{r_code}.txt")
            ft = ft_path.read_text(encoding="utf-8") if ft_path.exists() else ""
            parsed_resumes[r_code] = ResumeProfile(
                title=str(r_data.get("title", "")), summary=str(r_data.get("summary", "")),
                full_text=ft, project_keys=tuple(r_data.get("project_keys", [])),
                tech_focus=tuple(r_data.get("tech_focus", [])), domains=tuple(r_data.get("domains", [])),
                grade_markers=tuple(r_data.get("grade_markers", [])), forbidden_facts=tuple(r_data.get("forbidden_facts", [])),
            )
    parsed_rules = []
    raw_rules = raw_profile.get("stage0_rules", [])
    if isinstance(raw_rules, list):
        for rule in raw_rules:
            if not isinstance(rule, dict): continue
            if "default" in rule:
                parsed_rules.append(Stage0Rule(keywords=(), candidates=tuple(rule["default"])))
            else:
                parsed_rules.append(Stage0Rule(keywords=tuple(rule.get("keywords", [])), candidates=tuple(rule.get("candidates", []))))
    # --------------------------------------

    def section(name: str) -> Mapping[str, object]:'''
    
    if "parsed_resumes" not in c:
        c = c.replace(anchor_parse, parse)
        
    # Fallback in except block
    anchor_except = "        cover_letter = CoverLetterProfile(\"ru\", 1800, \"professional\")"
    fallback = """        cover_letter = CoverLetterProfile("ru", 1800, "professional")
        parsed_resumes = {}
        parsed_rules = []"""
    if "parsed_resumes = {}" not in c:
        c = c.replace(anchor_except, fallback)
        
    # Pass to Profile instantiation
    anchor_profile = "            candidate=candidate,\n            hh=hh,\n            cover_letter=cover_letter,\n        )"
    new_profile = "            candidate=candidate,\n            hh=hh,\n            cover_letter=cover_letter,\n            resumes=parsed_resumes,\n            stage0_rules=tuple(parsed_rules),\n        )"
    if "resumes=parsed_resumes" not in c:
        c = c.replace(anchor_profile, new_profile)
        
    p.write_text(c)

# 2. PATCH AI_ANALYZER.PY
def patch_analyzer():
    p = Path("ai_analyzer.py")
    c = p.read_text()
    
    # Add methods
    methods = '''    def select_resume_stage0(self, title: str, description: str) -> list[str]:
        text = (title + " " + description).lower()
        candidates = set()
        for rule in self.settings.profile.stage0_rules:
            if any(kw.lower() in text for kw in rule.keywords):
                candidates.update(rule.candidates)
        return list(candidates) if candidates else ["RP"]

    async def select_resume_stage1(self, title: str, description: str, candidates: list[str]) -> tuple[str, str]:
        summaries = {c: self.settings.profile.resumes[c].summary for c in candidates if c in self.settings.profile.resumes}
        prompt = f"Выбери лучшее резюме для вакансии.\\nВакансия: {title}\\n{description[:2000]}\\n\\nСаммари:\\n" + "\\n".join(f"{k}: {v}" for k, v in summaries.items())
        prompt += '\\n\\nОтветь JSON: {"best_resume": "Код", "rationale": "1-2 предложения"}'
        req = self._request(system_instructions="Ты HR-эксперт. Выбери резюме.", payload={"prompt": prompt}, operation="stage1_select", structured=True)
        try:
            resp, structured = await self.provider.generate_structured(req, dict)
            if structured and "best_resume" in structured:
                return structured["best_resume"], structured.get("rationale", "Stage 1 LLM")
        except Exception:
            pass
        return candidates[0], "Fallback to Stage 0"

    async def assess(
'''
    if "select_resume_stage0" not in c:
        c = c.replace("    async def assess(\n", methods)

    # Modify assess signature
    if "resume_text: str | None = None" not in c:
        c = c.replace("        system_override: str | None = None,\n    ) -> SuitabilityResult:", "        system_override: str | None = None,\n        best_resume: str | None = None,\n        resume_text: str | None = None,\n    ) -> SuitabilityResult:")
    
    # Modify assess payload
    old_payload = "        payload = {\"candidate\": self._candidate_for_analysis(), \"vacancy\": {\"title\": vacancy_title, \"description\": vacancy_description}}"
    new_payload = '''        cand_data = self._candidate_for_analysis()
        if resume_text:
            cand_data = {"best_resume": best_resume, "resume_text": resume_text}
        payload = {"candidate": cand_data, "vacancy": {"title": vacancy_title, "description": vacancy_description}}'''
    if old_payload in c:
        c = c.replace(old_payload, new_payload)

    # Modify generate_cover_letter signature
    if "resume_text: str | None = None,\n    ) -> str:" not in c:
        c = c.replace("        session_id: str | None = None,\n    ) -> str:", "        session_id: str | None = None,\n        best_resume: str | None = None,\n        resume_text: str | None = None,\n    ) -> str:")
        
    # Modify generate_cover_letter payload
    old_cl = '        candidate = self._candidate()\n        candidate.pop("additional_information", None)'
    new_cl = '''        candidate = self._candidate()
        candidate.pop("additional_information", None)
        if resume_text:
            candidate = {"best_resume": best_resume, "resume_text": resume_text}'''
    if old_cl in c:
        c = c.replace(old_cl, new_cl)

    p.write_text(c)

# 3. PATCH MAIN.PY
def patch_main():
    p = Path("main.py")
    c = p.read_text()
    
    # Inject Stage 0 + 1 before assess
    stage_logic = '''    candidates = analyzer.select_resume_stage0(summary.title, details.description)
    if len(candidates) > 1:
        best_resume, reason = await analyzer.select_resume_stage1(summary.title, details.description, candidates)
    else:
        best_resume = candidates[0]
        reason = "Stage 0 heuristic"
    resume_text = settings.profile.resumes.get(best_resume).full_text if best_resume in settings.profile.resumes else ""

    try:
        if _context_analyzer is not None and _context_digest:
            suitability = await _context_analyzer.assess(
                summary.title, details.description, vacancy_id=summary.id,
                model_override=settings.llm.assess_model,
                session_id=_context_session_id,
                system_override=_context_digest,
                best_resume=best_resume, resume_text=resume_text,
            )
        else:
            suitability = await analyzer.assess(
                summary.title, details.description, vacancy_id=summary.id,
                best_resume=best_resume, resume_text=resume_text,
            )'''
    old_assess = '''    try:
        if _context_analyzer is not None and _context_digest:
            suitability = await _context_analyzer.assess(
                summary.title, details.description, vacancy_id=summary.id,
                model_override=settings.llm.assess_model,
                session_id=_context_session_id,
                system_override=_context_digest,
            )
        else:
            suitability = await analyzer.assess(
                summary.title, details.description, vacancy_id=summary.id
            )'''
    if old_assess in c:
        c = c.replace(old_assess, stage_logic)

    # Add best_resume_reason to evaluation data
    if '"best_resume_reason": reason,' not in c:
        c = c.replace('"best_resume": suitability.best_resume,', '"best_resume": suitability.best_resume or best_resume,\n        "best_resume_reason": reason,')

    # Modify generate_cover_letter call
    old_cl = '''        letter = await _cl_analyzer.generate_cover_letter(
            summary.title, details.description, company_name=details.company,
            vacancy_id=summary.id, **_cl_kwargs,
        )'''
    new_cl = '''        letter = await _cl_analyzer.generate_cover_letter(
            summary.title, details.description, company_name=details.company,
            vacancy_id=summary.id, best_resume=best_resume, resume_text=resume_text, **_cl_kwargs,
        )'''
    if old_cl in c:
        c = c.replace(old_cl, new_cl)

    p.write_text(c)

if __name__ == "__main__":
    print("⏳ Бэкап файлов...")
    for f in ["config.py", "ai_analyzer.py", "main.py"]:
        backup(f)
    
    try:
        print("🛠️ Патчим config.py...")
        patch_config()
        print("🛠️ Патчим ai_analyzer.py...")
        patch_analyzer()
        print("🛠️ Патчим main.py...")
        patch_main()
        
        print("🔍 Проверка синтаксиса...")
        if not check():
            rollback()
            exit(1)
            
        print("✅ Все файлы успешно обновлены и скомпилированы!")
        print("Теперь архитектура Stage 0 -> 1 -> 2 встроена в пайплайн.")
    except Exception as e:
        print(f"Exception: {e}")
        rollback()
