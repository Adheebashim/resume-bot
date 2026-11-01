"""
Resume tailoring logic - job parsing, company research, fit decision, and Word template tailoring.
"""

import io
import json
from pathlib import Path

from docx import Document
from ddgs import DDGS

from config.settings import TEMPLATES, ROLE_LABELS
from modules.llm_provider import ask, parse_json, _clean


def read_docx_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Resume template not found: {path}. Add the three Word files to the templates/ folder.")
    document = Document(str(path))
    return "\n".join(p.text for p in document.paragraphs if p.text.strip())


def get_default_resume_text() -> str:
    # Compatibility fallback for old commands; Data Analyst is the neutral default.
    try:
        return read_docx_text(TEMPLATES["data_analyst"])
    except Exception:
        legacy = Path("resume.txt")
        return legacy.read_text(encoding="utf-8") if legacy.exists() else ""


RESUME = get_default_resume_text()


def extract(jd: str) -> dict:
    """Extract company, role, and the best-fitting template role from a job description."""
    prompt = (
        'Return ONLY JSON: {"company":"","role":"","template_role":"data_engineer|ai_engineer|data_analyst"}\n'
        "Pick template_role by actual duties, not title: data_engineer = ETL/pipelines/warehouses/"
        "orchestration; ai_engineer = LLMs/RAG/agents/ML applications; data_analyst = SQL/BI/"
        "reporting/insights.\n\nJOB:\n" + _clean(jd, 2500)
    )
    try:
        result = parse_json(ask(prompt, task="light", json_mode=True)[0])
    except Exception:
        result = {}
    result.setdefault("company", "")
    result.setdefault("role", "")
    selected = str(result.get("template_role", "")).strip().lower()
    if selected not in TEMPLATES:
        text = (str(result.get("role", "")) + "\n" + jd).lower()
        if any(k in text for k in ("generative ai", "agentic ai", "llm", "rag", "machine learning engineer", "ai engineer", "artificial intelligence")):
            selected = "ai_engineer"
        elif any(k in text for k in ("data engineer", "etl", "data pipeline", "data warehouse", "dbt", "airflow", "spark", "databricks")):
            selected = "data_engineer"
        else:
            selected = "data_analyst"
    result["template_role"] = selected
    return result


def research(company: str) -> str:
    """Tiny company snapshot (3 short results) - enough for emphasis, cheap in tokens."""
    if not company:
        return ""
    try:
        results = DDGS().text(f"{company} company about products", max_results=3)
        return "\n".join(
            f"- {(item.get('title') or '')[:60]}: {(item.get('body') or '')[:140]}"
            for item in results
        )
    except Exception:
        return ""


def decide(jd: str, company_ctx: str, resume_text: str) -> dict:
    """Stand-alone fit score (only used for uploaded-PDF resumes; templates score inside tailor_docx)."""
    prompt = (
        'Return ONLY JSON {"fit_score":0}: realistic integer 0-100 fit of this resume to the job '
        "(skills, experience, projects, qualifications). Do not inflate.\n\n"
        f"RESUME:\n{_clean(resume_text, 3500)}\n\nJOB:\n{_clean(jd, 3500)}"
    )
    try:
        result = parse_json(ask(prompt, task="light", json_mode=True)[0])
        result["fit_score"] = max(0, min(100, int(float(result.get("fit_score", 0)))))
        return result
    except Exception:
        return {"fit_score": 0}


def tailor(jd: str, company_ctx: str, decision: dict, resume_text: str):
    """Tailor an uploaded/custom resume (plain text -> plain text) for the PDF renderer."""
    prompt = (
        "Tailor this resume to the job. Use ONLY facts in the resume; never invent skills, "
        "employers, dates, credentials or numbers. You may reorder, rephrase and emphasise; "
        "mirror job keywords only when truthful. Plain text, no Markdown, about one page.\n\n"
        f"RESUME:\n{_clean(resume_text, 6000)}\n\n"
        + (f"COMPANY:\n{company_ctx}\n\n" if company_ctx else "")
        + f"JOB:\n{_clean(jd, 5000)}"
    )
    return ask(prompt, task="heavy")


# ---------------------------------------------------------------------
# Word template tailoring
# ---------------------------------------------------------------------
def _set_paragraph_text(paragraph, value: str) -> None:
    """Replace text while retaining the paragraph's existing style and layout."""
    runs = paragraph.runs
    if runs:
        runs[0].text = value
        for run in runs[1:]:
            run.text = ""
    else:
        paragraph.add_run(value)


def _template_sections(document):
    paragraphs = document.paragraphs
    texts = [p.text.strip() for p in paragraphs]
    summary_idx = next((i for i, t in enumerate(texts) if t.upper() == "SUMMARY"), None)
    experience_idx = next((i for i, t in enumerate(texts) if t.upper() == "EXPERIENCE"), None)
    project_idx = next((i for i, t in enumerate(texts) if t.upper() in {"PROJECTS", "AI PROJECTS"}), None)
    skills_idx = next((i for i, t in enumerate(texts) if t.upper() == "SKILLS"), None)
    if None in (summary_idx, experience_idx, project_idx, skills_idx):
        raise ValueError("The selected Word template is missing a required section heading.")
    summary = summary_idx + 1
    nielsen_idx = next((i for i, t in enumerate(texts) if i > experience_idx and "NielsenIQ" in t), None)
    vyza_idx = next((i for i, t in enumerate(texts) if i > experience_idx and "Vyza Solutions" in t), None)
    if nielsen_idx is None or vyza_idx is None or not (nielsen_idx < vyza_idx < project_idx):
        raise ValueError("Could not identify the experience sections in the selected Word template.")
    nielsen_bullets = [i for i in range(nielsen_idx + 1, vyza_idx) if texts[i]]
    intern_bullets = [i for i in range(vyza_idx + 1, project_idx) if texts[i]]
    project_bullets = [i for i in range(project_idx + 1, skills_idx) if texts[i] and " | " not in texts[i] and "  |  " not in texts[i]]
    education_idx = next((i for i, t in enumerate(texts) if i > skills_idx and t.upper() == "EDUCATION"), len(texts))
    skills_lines = [i for i in range(skills_idx + 1, education_idx) if texts[i]]
    return {"summary": summary, "nielsen": nielsen_bullets, "intern": intern_bullets, "projects": project_bullets, "skills": skills_lines}


def tailor_docx(template_role: str, jd: str, company_ctx: str, decision: dict):
    """One LLM call: tailors the template AND scores fit. Returns (docx_bytes, fit_score_0_100)."""
    document = Document(str(TEMPLATES[template_role]))
    sections = _template_sections(document)
    paragraphs = document.paragraphs
    source_summary = paragraphs[sections["summary"]].text
    source_nielsen = [paragraphs[i].text for i in sections["nielsen"]]
    source_intern = [paragraphs[i].text for i in sections["intern"]]
    source_projects = [paragraphs[i].text for i in sections["projects"]]
    skills_text = "\n".join(paragraphs[i].text.replace("\t", ": ") for i in sections["skills"])
    dumps = lambda v: json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    prompt = (
        'Tailor this resume content to the job. Return ONLY JSON: {"fit_score":0,"summary":"",'
        f'"nielsen_bullets":[{len(source_nielsen)} strings],"intern_bullets":[{len(source_intern)} strings],'
        f'"project_bullets":[{len(source_projects)} strings]}}\n'
        "fit_score = realistic integer 0-100 fit of the ORIGINAL resume (incl. skills) to the job; don't inflate.\n"
        "Rules: use only facts in the source; never invent skills, tools, employers, dates, credentials or "
        "metrics. Rephrase/reorder for relevance, mirror job keywords only when truthful. Each bullet no "
        "longer than its original. Plain text, no bullet symbols, same bullet counts.\n"
        f"Target role: {ROLE_LABELS[template_role]}\n"
        f"SUMMARY: {source_summary}\n"
        f"NIELSENIQ: {dumps(source_nielsen)}\n"
        f"INTERNSHIP: {dumps(source_intern)}\n"
        f"PROJECTS: {dumps(source_projects)}\n"
        f"SKILLS:\n{skills_text}\n"
        + (f"COMPANY:\n{company_ctx}\n" if company_ctx else "")
        + f"JOB:\n{_clean(jd, 5000)}"
    )
    output, _model = ask(prompt, task="heavy", json_mode=True)
    data = parse_json(output)
    source_map = {
        "nielsen_bullets": source_nielsen,
        "intern_bullets": source_intern,
        "project_bullets": source_projects,
    }
    replacements = []
    summary = str(data.get("summary") or "").strip() or source_summary
    replacements.append((sections["summary"], summary))

    for key, indices in (("nielsen_bullets", sections["nielsen"]),
                         ("intern_bullets", sections["intern"]),
                         ("project_bullets", sections["projects"])):
        raw_values = data.get(key)
        clean_values = []
        if isinstance(raw_values, list):
            clean_values = [str(v).strip() for v in raw_values if str(v).strip()]
        # If fewer bullets returned, pad from original template bullets
        if len(clean_values) < len(indices):
            clean_values.extend(source_map[key][len(clean_values):len(indices)])
        clean_values = clean_values[:len(indices)]
        replacements.extend(zip(indices, clean_values))

    for index, value in replacements:
        _set_paragraph_text(paragraphs[index], value.strip())
    try:
        fit = max(0, min(100, int(float(data.get("fit_score")))))
    except (TypeError, ValueError):
        fit = None
    document.core_properties.title = f"Adheeb Ashim - {ROLE_LABELS[template_role]} Resume"
    document.core_properties.author = "Adheeb Ashim"
    document.core_properties.subject = f"Tailored application resume for {ROLE_LABELS[template_role]}"
    output_buffer = io.BytesIO()
    document.save(output_buffer)
    return output_buffer.getvalue(), fit
