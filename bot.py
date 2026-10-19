import os
import re
import json
import time
from datetime import datetime
import io
import base64
import requests
from html.parser import HTMLParser
from urllib.parse import urlparse
import ipaddress
import socket
import shutil
import subprocess
import tempfile
from pathlib import Path
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from xml.sax.saxutils import escape
from ddgs import DDGS

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
from pypdf import PdfReader
from docx import Document
import gspread

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
)


# ---------------------------------------------------------------------
# Environment configuration
# ---------------------------------------------------------------------
TOKEN = os.environ["BOT_TOKEN"]
OWNER = int(os.environ["OWNER_ID"])

GROQ_KEY = os.environ.get("GROQ_API_KEY")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID")
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET")
GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN")
GMAIL_SENDER = os.environ.get("GMAIL_SENDER")

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SHEET_TAB = os.environ.get("GOOGLE_SHEET_TAB", "Applications")

TEMPLATE_DIR = Path(os.environ.get("RESUME_TEMPLATE_DIR", "templates"))
TEMPLATES = {
    "data_engineer": TEMPLATE_DIR / "Adheeb_DataEngineer.docx",
    "ai_engineer": TEMPLATE_DIR / "Adheeb_AIEngineer.docx",
    "data_analyst": TEMPLATE_DIR / "Adheeb_DataAnalyst.docx",
}
ROLE_LABELS = {
    "data_engineer": "Data Engineer",
    "ai_engineer": "AI Engineer",
    "data_analyst": "Data Analyst",
}
# Comma-separated lists are tried in order. Override in Render with GROQ_MODELS / GEMINI_MODELS
# (or the old single-model GROQ_MODEL / GEMINI_MODEL variables) without touching code.
def _model_list(plural: str, single: str, default: str) -> list:
    raw = os.environ.get(plural) or os.environ.get(single) or default
    return [m.strip() for m in raw.split(",") if m.strip()]

GROQ_MODELS = _model_list(
    "GROQ_MODELS", "GROQ_MODEL",
    "openai/gpt-oss-120b,qwen/qwen3.6-27b,openai/gpt-oss-20b",
)
GEMINI_MODELS = _model_list(
    "GEMINI_MODELS", "GEMINI_MODEL",
    "gemini-2.5-flash,gemini-2.5-flash-lite",
)
GEMINI_MODEL = GEMINI_MODELS[0]

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


# ---------------------------------------------------------------------
# LLM providers with failover
# ---------------------------------------------------------------------
def _http_error(provider: str, model: str, response) -> str:
    body = (response.text or "").strip().replace("\n", " ")[:200]
    return f"{provider}/{model} HTTP {response.status_code}: {body}"


def _groq(prompt: str) -> str:
    errors = []
    for model in GROQ_MODELS:
        for attempt in range(2):
            try:
                response = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_KEY}"},
                    json={
                        "model": model,
                        "temperature": 0.3,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=90,
                )
            except requests.RequestException as exc:
                errors.append(f"Groq/{model}: {exc}")
                break
            if response.ok:
                content = response.json()["choices"][0]["message"].get("content")
                if content and content.strip():
                    return content
                errors.append(f"Groq/{model}: empty response")
                break
            if response.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                time.sleep(4)
                continue
            errors.append(_http_error("Groq", model, response))
            break  # 404/400/401 etc: move on to the next model
    raise RuntimeError(" | ".join(errors))


def _gemini_request(model: str, payload: dict):
    """POST to Gemini; retry once on 429/5xx. Returns the final response."""
    response = None
    for attempt in range(2):
        response = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent",
            headers={"x-goog-api-key": GEMINI_KEY},
            json=payload,
            timeout=90,
        )
        if response.status_code in (429, 500, 502, 503, 504) and attempt == 0:
            time.sleep(6)
            continue
        break
    return response


def _gemini_call(payload: dict) -> str:
    errors = []
    for model in GEMINI_MODELS:
        try:
            response = _gemini_request(model, payload)
        except requests.RequestException as exc:
            errors.append(f"Gemini/{model}: {exc}")
            continue
        if response.ok:
            try:
                return response.json()["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError, TypeError):
                errors.append(f"Gemini/{model}: empty or blocked response")
                continue
        errors.append(_http_error("Gemini", model, response))
    raise RuntimeError(" | ".join(errors))


def _gemini(prompt: str) -> str:
    return _gemini_call({"contents": [{"parts": [{"text": prompt}]}]})


def ask(prompt: str):
    providers = []
    # Gemini is primary; Groq is the automatic fallback.
    if GEMINI_KEY:
        providers.append(("Gemini", _gemini))
    if GROQ_KEY:
        providers.append(("Groq", _groq))

    if not providers:
        raise RuntimeError(
            "No LLM API key configured. Add GROQ_API_KEY and/or GEMINI_API_KEY."
        )

    errors = []
    for name, function in providers:
        try:
            return function(prompt), name
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    raise RuntimeError("All LLM providers failed: " + "; ".join(errors))


def extract_text_from_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """Use Gemini vision to read text/data from a Telegram screenshot."""
    if not GEMINI_KEY:
        raise RuntimeError(
            "Screenshot reading requires GEMINI_API_KEY. Add it in Render Environment."
        )

    encoded_image = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "contents": [{
            "parts": [
                {
                    "text": (
                        "Read this screenshot carefully and extract all useful text "
                        "verbatim, especially job title, company, job description, "
                        "skills, recruiter name, and any email address. Preserve "
                        "line breaks where useful. Do not guess unreadable text. "
                        "Return only the extracted text."
                    )
                },
                {"inline_data": {"mime_type": mime_type, "data": encoded_image}},
            ]
        }]
    }
    return _gemini_call(payload).strip()



class PublicPageParser(HTMLParser):
    """Extract visible text and useful metadata from an ordinary public HTML page."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.metadata = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style", "noscript", "svg", "nav", "footer", "header"):
            self._skip_depth += 1
        if tag == "meta":
            name = (attrs.get("name") or attrs.get("property") or "").lower()
            content = (attrs.get("content") or "").strip()
            if content and name in {
                "description", "og:title", "og:description", "twitter:title",
                "twitter:description"
            }:
                self.metadata.append(content)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg", "nav", "footer", "header") and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        cleaned = " ".join(data.split())
        if cleaned and not self._skip_depth:
            self.parts.append(cleaned)


def _validate_public_url(url: str) -> str:
    """Accept only public HTTP(S) URLs; do not fetch localhost/private addresses."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Please send a complete public http:// or https:// link.")
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("That link is not a public website.")
    try:
        addresses = socket.getaddrinfo(host, None)
        for entry in addresses:
            ip = ipaddress.ip_address(entry[4][0])
            if not ip.is_global:
                raise ValueError("That link does not point to a public website.")
    except socket.gaierror as exc:
        raise ValueError("I couldn't resolve that website address.") from exc
    return url


def extract_public_page(url: str) -> str:
    """Read public page text only; never bypass login, anti-bot checks, or access controls."""
    url = _validate_public_url(url)
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; ResumeHelper/1.0)"},
        timeout=20,
        allow_redirects=True,
    )
    response.raise_for_status()
    if response.url != url:
        _validate_public_url(response.url)
    content_type = response.headers.get("content-type", "").lower()
    if "text/html" not in content_type:
        raise ValueError("This link doesn't point to a readable HTML page.")
    parser = PublicPageParser()
    parser.feed(response.text[:2_000_000])
    chunks = parser.metadata + parser.parts
    # De-duplicate repeated metadata while preserving page order.
    seen = set()
    cleaned = []
    for chunk in chunks:
        if chunk not in seen:
            seen.add(chunk)
            cleaned.append(chunk)
    result = "\n".join(cleaned)
    if len(result.strip()) < 180:
        raise ValueError(
            "The page doesn't expose enough public text. LinkedIn may require sign-in "
            "or restrict automated access."
        )
    return result[:16000]


def find_url(text: str):
    match = re.search(r"https?://[^\s<>]+", text.strip(), flags=re.I)
    if not match:
        return None
    return match.group(0).rstrip(".,!?)]}")

def is_likely_job_posting(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "job description", "responsibilities", "requirements", "qualifications",
        "we are hiring", "apply now", "job title", "role:", "position:",
        "experience required", "skills required", "about the job",
    )
    return len(text) > 250 or sum(marker in lowered for marker in markers) >= 2


async def interactive_reply(update: Update, text: str) -> None:
    """Reply naturally to greetings and general chat, using an LLM when available."""
    message = update.effective_message
    if not message:
        return

    normalized = text.strip().lower().strip("!.,? ")
    simple_replies = {
        "hi": "Hi! 👋 How can I help you today?",
        "hello": "Hello! 👋 Send me a job description or screenshot whenever you're ready.",
        "hey": "Hey! 😊 What would you like to do?",
        "good morning": "Good morning! ☀️ How can I help?",
        "good afternoon": "Good afternoon! How can I help?",
        "good evening": "Good evening! How can I help?",
        "thanks": "You're welcome! 😊",
        "thank you": "You're welcome! Let me know if you need anything else.",
        "help": (
            "Here's what I can do:\n"
            "• Chat normally — say hi and I'll reply.\n"
            "• Send a job description as text, a public job-post link, or a screenshot.\n"
            "• I'll automatically select the best of your three Word templates: Data Engineer, AI Engineer, or Data Analyst.\n"
            "• You can upload a PDF if you want to use a custom resume for one application.\n"
            "• Send a public job-post link or upload a screenshot.\n"
            "• Include a recruiter email, or send it when I ask.\n"
            "• Review the fit, tailor your resume, and show you a PDF to approve.\n"
            "• Email the resume only after you press Send."
        ),
    }
    if normalized in simple_replies:
        await message.reply_text(simple_replies[normalized])
        return

    if GROQ_KEY or GEMINI_KEY:
        try:
            answer, _provider = ask(
                "You are a friendly, concise Telegram assistant that also helps "
                "tailor resumes to job postings. Reply naturally to the user's "
                "message. Do not claim you have performed an action you have not "
                "performed. If the user seems to be sharing a job post, ask them "
                "to include the recruiter email address or offer to collect it next.\n\n"
                f"User message:\n{text}"
            )
            await message.reply_text(answer[:4000])
            return
        except Exception:
            pass

    await message.reply_text(
        "I can help with job descriptions, screenshots, resume tailoring, and "
        "emailing after your approval. Send /help to see the options."
    )


def parse_json(text: str) -> dict:
    """Parse a JSON object even if the model wraps it in a code fence."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    match = re.search(r"\{.*\}", cleaned, re.S)
    if not match:
        return {}
    try:
        value = json.loads(match.group(0))
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------
# Google Sheets application tracker
# ---------------------------------------------------------------------
SHEET_HEADERS = [
    "Applied/Created At", "Company", "Job Role", "Resume Used",
    "Recruiter Email", "Fit Score (/10)", "Application Status", "Source",
    "Resume Filename",
]


def get_tracking_worksheet():
    """Return the configured Google Sheet tab, or None if tracking is not configured."""
    if not GOOGLE_SHEET_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return None
    credentials_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    client = gspread.service_account_from_dict(credentials_info)
    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
    try:
        worksheet = spreadsheet.worksheet(GOOGLE_SHEET_TAB)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=GOOGLE_SHEET_TAB, rows=1000, cols=len(SHEET_HEADERS))
    current = worksheet.row_values(1)
    if current[:len(SHEET_HEADERS)] != SHEET_HEADERS:
        if not current:
            worksheet.append_row(SHEET_HEADERS, value_input_option="USER_ENTERED")
        elif current[0] != SHEET_HEADERS[0]:
            worksheet.insert_row(SHEET_HEADERS, 1, value_input_option="USER_ENTERED")
    return worksheet


def log_application(company: str, role: str, resume_used: str, recipient: str,
                    fit_score: str, status: str, source: str, filename: str):
    """Append one application-preparation row; return its worksheet row number."""
    worksheet = get_tracking_worksheet()
    if worksheet is None:
        return None
    row = [
        datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        company or "Unknown",
        role or "Unknown",
        resume_used or "Unknown",
        recipient or "",
        fit_score,
        status,
        source or "Job description",
        filename or "",
    ]
    worksheet.append_row(row, value_input_option="USER_ENTERED")
    return len(worksheet.get_all_values())


def update_application_status(row_number, status: str):
    if not row_number:
        return
    worksheet = get_tracking_worksheet()
    if worksheet is not None:
        # Application Status is column G.
        worksheet.update_cell(int(row_number), 7, status)


# ---------------------------------------------------------------------
# Job parsing, company research, and fit decision
# ---------------------------------------------------------------------
def extract(jd: str) -> dict:
    prompt = (
        'Classify this job into exactly one target role and return ONLY valid JSON: '
        '{"company":"", "role":"", "key_skills":[], '
        '"template_role":"data_engineer|ai_engineer|data_analyst"}. '
        'Choose by actual responsibilities, not job title alone. Data engineering covers ETL, pipelines, warehouses, orchestration; '
        'AI engineering covers LLMs, RAG, agents, ML/AI application development; data analytics covers SQL, BI, reporting, insights.\n\n'
        + jd
    )
    try:
        result = parse_json(ask(prompt)[0])
    except Exception:
        result = {}
    result.setdefault("company", "")
    result.setdefault("role", "")
    result.setdefault("key_skills", [])
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
    if not company:
        return ""

    try:
        results = DDGS().text(
            f"{company} company about products",
            max_results=5,
        )
        return "\n".join(
            f"- {item.get('title', '')}: {item.get('body', '')}"
            for item in results
        )
    except Exception:
        return ""


def decide(jd: str, company_ctx: str, resume_text: str) -> dict:
    prompt = f"""Compare this candidate's resume with the job description and return ONLY valid JSON:
{{"fit_score": 0}}
Give fit_score as an integer from 0 to 100 based on relevant skills, experience, projects, and required qualifications. Be realistic; do not inflate the score. Do not return a verdict, angle, gaps, or recommendations.

RESUME:
{resume_text}

COMPANY INFO:
{company_ctx or 'none found'}

JOB DESCRIPTION:
{jd}"""
    try:
        result = parse_json(ask(prompt)[0])
        score = int(float(result.get("fit_score", 0)))
        result["fit_score"] = max(0, min(100, score))
        return result
    except Exception:
        return {"fit_score": 0}


def tailor(jd: str, company_ctx: str, decision: dict, resume_text: str):
    prompt = f"""Tailor the resume below to the job description.
Rules:
- Use ONLY facts in the resume.
- Never invent skills, employers, dates, qualifications, or numbers.
- You may reorder, rephrase, and emphasize relevant items.
- Mirror job-description keywords only when truthful.
- Use company information only to choose relevant emphasis.
- Do not state unverified claims about the company.
- Output plain text only, no Markdown symbols, ideally one page.

RESUME:
{resume_text}

COMPANY INFO:
{company_ctx or 'none'}

JOB DESCRIPTION:
{jd}"""
    return ask(prompt)


# ---------------------------------------------------------------------
# Word template tailoring and PDF conversion
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
    return {"summary": summary, "nielsen": nielsen_bullets, "intern": intern_bullets, "projects": project_bullets}

def tailor_docx(template_role: str, jd: str, company_ctx: str, decision: dict) -> bytes:
    template_path = TEMPLATES[template_role]
    document = Document(str(template_path))
    sections = _template_sections(document)
    paragraphs = document.paragraphs
    source_summary = paragraphs[sections["summary"]].text
    source_nielsen = [paragraphs[i].text for i in sections["nielsen"]]
    source_intern = [paragraphs[i].text for i in sections["intern"]]
    source_projects = [paragraphs[i].text for i in sections["projects"]]
    prompt = f"""Tailor the resume content for this job. Return ONLY valid JSON with this exact shape:
{{
  \"summary\": \"...\",
  \"nielsen_bullets\": [exactly {len(source_nielsen)} strings],
  \"intern_bullets\": [exactly {len(source_intern)} strings],
  \"project_bullets\": [exactly {len(source_projects)} strings]
}}
Rules: use only facts supported by the source resume; never invent skills, tools, employers, dates, credentials, achievements, or metrics. Rephrase and reorder emphasis only. Keep each bullet concise enough to fit the existing resume layout. Do not include bullet symbols. Keep all strings plain text. Preserve the candidate's name as Adheeb Ashim; never replace it with the word Resume.
Target role: {ROLE_LABELS[template_role]}
Emphasize: {decision.get('emphasize', [])}
Existing summary: {source_summary}
Existing NielsenIQ bullets: {json.dumps(source_nielsen, ensure_ascii=False)}
Existing internship bullets: {json.dumps(source_intern, ensure_ascii=False)}
Existing project bullets: {json.dumps(source_projects, ensure_ascii=False)}
Company context: {company_ctx or 'none'}
Job description: {jd}
"""
    output, _model = ask(prompt)
    data = parse_json(output)
    replacements = [
        (sections["summary"], data.get("summary")),
    ]
    for key, indices in (("nielsen_bullets", sections["nielsen"]), ("intern_bullets", sections["intern"]), ("project_bullets", sections["projects"])):
        values = data.get(key)
        if not isinstance(values, list) or len(values) != len(indices) or not all(isinstance(v, str) and v.strip() for v in values):
            raise ValueError(f"The AI returned an invalid {key} structure. Please retry.")
        replacements.extend(zip(indices, values))
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("The AI did not return a valid resume summary. Please retry.")
    for index, value in replacements:
        _set_paragraph_text(paragraphs[index], value.strip())
    document.core_properties.title = f"Adheeb Ashim - {ROLE_LABELS[template_role]} Resume"
    document.core_properties.author = "Adheeb Ashim"
    document.core_properties.subject = f"Tailored application resume for {ROLE_LABELS[template_role]}"
    output_buffer = io.BytesIO()
    document.save(output_buffer)
    return output_buffer.getvalue()

def docx_to_pdf(docx_bytes: bytes) -> bytes:
    """Convert a tailored Word file using LibreOffice, installed in the Render build."""
    executable = shutil.which("libreoffice") or shutil.which("soffice")
    if not executable:
        raise RuntimeError("LibreOffice is not installed. Use the included Render build command to install libreoffice-writer.")
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        docx_path = temp / "Adheeb_Tailored_Resume.docx"
        docx_path.write_bytes(docx_bytes)
        result = subprocess.run(
            [executable, "-env:UserInstallation=file://" + str(temp / "lo-profile"), "--headless", "--convert-to", "pdf", "--outdir", str(temp), str(docx_path)],
            capture_output=True, text=True, timeout=90, check=False,
        )
        pdf_path = temp / "Adheeb_Tailored_Resume.pdf"
        if result.returncode != 0 or not pdf_path.exists():
            detail = (result.stderr or result.stdout or "No conversion details").strip()
            raise RuntimeError("Word-to-PDF conversion failed: " + detail[:500])
        pdf = pdf_path.read_bytes()
        reader = PdfReader(io.BytesIO(pdf))
        if not reader.pages or len(reader.pages) > 3:
            raise RuntimeError(f"Generated resume has {len(reader.pages)} pages. Please shorten the content and try again.")
        if len("\n".join(p.extract_text() or "" for p in reader.pages).strip()) < 250:
            raise RuntimeError("Generated PDF text validation failed. Please retry.")
        return pdf

def safe_filename(value: str, max_len: int = 45) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", value or "").strip("_")
    return (cleaned[:max_len].strip("_") or "Application")


# ---------------------------------------------------------------------
# PDF and email
# ---------------------------------------------------------------------
REFERENCE_LINKS = {
    "LinkedIn": "https://www.linkedin.com/in/adheeb-hashim/",
    "GitHub": "https://github.com/Adheebashim",
    "Portfolio": "https://adheebashim.dev/",
}


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract selectable text from an uploaded resume PDF."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 150:
        raise ValueError("I couldn't extract enough selectable text from this PDF. Please upload a text-based PDF, not a scanned image.")
    return text

def make_pdf(text: str) -> bytes:
    """Create a compact, professional resume PDF matching the reference layout."""
    buffer = io.BytesIO()
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=43, rightMargin=43,
        topMargin=31, bottomMargin=31, title="Adheeb Ashim - Tailored Resume",
        author="Adheeb Ashim",
    )

    name_style = ParagraphStyle(
        "ResumeName", parent=styles["Normal"], fontName="Helvetica-Bold",
        fontSize=22, leading=24, alignment=TA_CENTER,
        textColor=colors.HexColor("#17202A"), spaceAfter=2,
    )
    subtitle_style = ParagraphStyle(
        "ResumeSubtitle", parent=styles["Normal"], fontName="Helvetica-Bold",
        fontSize=10, leading=12, alignment=TA_CENTER, spaceAfter=3,
    )
    contact_style = ParagraphStyle(
        "ResumeContact", parent=styles["Normal"], fontName="Helvetica",
        fontSize=8.5, leading=10, alignment=TA_CENTER, spaceAfter=2,
    )
    body_style = ParagraphStyle(
        "ResumeBody", parent=styles["Normal"], fontName="Helvetica",
        fontSize=8.5, leading=10.4, alignment=TA_LEFT, spaceAfter=1.8,
        textColor=colors.HexColor("#202124"),
    )
    bullet_style = ParagraphStyle(
        "ResumeBullet", parent=body_style, leftIndent=11, firstLineIndent=-8,
        spaceAfter=2,
    )
    section_style = ParagraphStyle(
        "ResumeSection", parent=styles["Normal"], fontName="Helvetica-Bold",
        fontSize=10, leading=11.5, spaceBefore=7, spaceAfter=3,
        textColor=colors.HexColor("#17202A"), keepWithNext=True,
    )
    role_style = ParagraphStyle(
        "ResumeRole", parent=body_style, fontName="Helvetica-Bold",
        fontSize=8.5, leading=10.5, spaceBefore=3, spaceAfter=2, keepWithNext=True,
    )
    small_style = ParagraphStyle(
        "ResumeSmall", parent=body_style, fontSize=7.8, leading=9.5,
    )

    section_names = {
        "SUMMARY", "PROFILE", "PROFESSIONAL SUMMARY", "EXPERIENCE",
        "WORK EXPERIENCE", "PROJECTS", "SELECTED PROJECTS",
        "INDEPENDENT ANALYTICAL SOLUTIONS & AI PROJECTS", "SKILLS",
        "TECHNICAL SKILLS", "EDUCATION", "CERTIFICATIONS", "ACHIEVEMENTS",
        "PUBLICATIONS",
    }
    story = []
    lines = [line.strip() for line in text.replace("\r", "").splitlines() if line.strip()]
    first = lines[0] if lines else ""
    has_name_header = bool(re.search(r"adheeb|^[A-Z][A-Za-z]+\s+[A-Z][A-Za-z]+$", first, re.I))

    if has_name_header:
        story.append(Paragraph(escape(first), name_style))
        lines = lines[1:]
        header_count = 0
        while lines and header_count < 3:
            candidate = lines[0]
            if candidate.upper().rstrip(":") in section_names or candidate.startswith(("-", "•", "–")):
                break
            story.append(Paragraph(escape(candidate), subtitle_style if header_count == 0 else contact_style))
            lines = lines[1:]
            header_count += 1
        story.append(Spacer(1, 4))
    else:
        story.append(Paragraph("ADHEEB ASHIM", name_style))
        story.append(Paragraph("Data Analyst · Machine Learning · Automation · Advanced Analytics", subtitle_style))
        story.append(Paragraph(
            'Nilambur, Kerala (Open to Relocate) · +91 8281759103 / +91 7306134684 · '
            'adheebashim1010@gmail.com', contact_style))
        links_html = ' · '.join(
            f'<link href="{url}" color="#1A73E8"><u>{label}</u></link>'
            for label, url in REFERENCE_LINKS.items()
        )
        story.append(Paragraph(links_html, contact_style))
        story.append(Spacer(1, 4))

    for raw in lines:
        line = raw.strip()
        # Convert reference link labels into real hyperlinks in the PDF.
        if re.fullmatch(r"(?:LinkedIn|GitHub|Portfolio)(?:\s*[·|•/]\s*(?:LinkedIn|GitHub|Portfolio))*", line, re.I):
            links_html = ' · '.join(
                f'<link href="{url}" color="#1A73E8"><u>{label}</u></link>'
                for label, url in REFERENCE_LINKS.items()
            )
            story.append(Paragraph(links_html, contact_style))
            continue
        normalized = re.sub(r"[:\s]+$", "", line).upper()
        if normalized in section_names or (len(line) < 42 and line.isupper() and not line.startswith(("-", "•"))):
            story.append(Spacer(1, 2))
            story.append(Paragraph(escape(normalized), section_style))
            story.append(HRFlowable(width="100%", thickness=0.55, color=colors.HexColor("#AAB2BD"), spaceBefore=0, spaceAfter=3))
        elif line.startswith(("-", "•", "–", "*")):
            story.append(Paragraph("– " + escape(line[1:].strip()), bullet_style))
        elif len(line) < 105 and (" — " in line or " – " in line or " | " in line or (line.endswith(":") and len(line) < 65)):
            story.append(Paragraph(escape(line), role_style))
        elif re.match(r"^(Technologies|Tools|Skills|Languages|Analytics|AI &|Generative AI|Operational Delivery)\s*:", line, re.I):
            label, value = line.split(":", 1)
            story.append(Paragraph(f"<b>{escape(label)}:</b>{escape(value)}", small_style))
        else:
            story.append(Paragraph(escape(line), body_style))

    doc.build(story)
    return buffer.getvalue()


def send_mail(to: str, subject: str, pdf: bytes, filename: str, role: str = "", company: str = "") -> None:
    """Send a resume PDF through the Gmail API using OAuth refresh credentials."""
    required = {
        "GMAIL_CLIENT_ID": GMAIL_CLIENT_ID,
        "GMAIL_CLIENT_SECRET": GMAIL_CLIENT_SECRET,
        "GMAIL_REFRESH_TOKEN": GMAIL_REFRESH_TOKEN,
        "GMAIL_SENDER": GMAIL_SENDER,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "Missing Gmail API environment variable(s): "
            + ", ".join(missing)
            + ". Add them in Render → Environment."
        )

    token_response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": GMAIL_CLIENT_ID,
            "client_secret": GMAIL_CLIENT_SECRET,
            "refresh_token": GMAIL_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if not token_response.ok:
        try:
            detail = token_response.json()
        except ValueError:
            detail = token_response.text[:500]
        raise RuntimeError(
            f"Google OAuth token refresh failed ({token_response.status_code}): {detail}"
        )

    access_token = token_response.json().get("access_token")
    if not access_token:
        raise RuntimeError("Google OAuth did not return an access token.")

    message = EmailMessage()
    message["To"] = to
    sender_email = parseaddr(GMAIL_SENDER)[1] or GMAIL_SENDER
    message["From"] = formataddr(("Adheeb Ashim", sender_email))
    message["Subject"] = subject
    greeting = "Dear Hiring Team,"
    position = f" for the {role} position" if role else ""
    at_company = f" at {company}" if company else ""
    message.set_content(
        f"{greeting}\n\n"
        f"Please find attached my resume{position}{at_company} for your consideration.\n\n"
        "Regards,\n"
        "Adheeb Ashim\n"
    )
    message.add_attachment(
        pdf,
        maintype="application",
        subtype="pdf",
        filename=filename,
    )

    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    response = requests.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"raw": raw_message},
        timeout=30,
    )
    if not response.ok:
        try:
            detail = response.json()
        except ValueError:
            detail = response.text[:500]
        raise RuntimeError(
            f"Gmail API send failed ({response.status_code}): {detail}"
        )


# ---------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != OWNER:
        if update.effective_message:
            await update.effective_message.reply_text(
                "Sorry, this bot is private."
            )
        return

    if update.effective_message:
        await update.effective_message.reply_text(
            "Resume Bot is online!\n\n"
            "Say hi to chat, send a public job-post link or job description, or upload a screenshot. "
            "I can read the screenshot, ask for the recruiter email if needed, "
            "tailor your resume and ask before emailing it. "
            "I'll automatically choose among your Data Engineer, AI Engineer, and Data Analyst Word templates. Upload a PDF only if you want to use a custom resume for one application. Send /help for all options."
        )


async def use_default_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or update.effective_user.id != OWNER:
        return
    job_text = ctx.user_data.get("pending_resume_choice_job")
    if not job_text:
        await update.effective_message.reply_text("Send a job description first, then choose a resume.")
        return
    ctx.user_data["selected_resume_text"] = RESUME
    ctx.user_data["selected_resume_source"] = "default"
    ctx.user_data.pop("pending_resume_choice_job", None)
    await process_job_posting(update, ctx, job_text, resume_choice_confirmed=True)


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Use an uploaded PDF as the source resume for the current or next job."""
    user = update.effective_user
    message = update.effective_message
    if not user or user.id != OWNER or not message or not message.document:
        return
    document = message.document
    filename = (document.file_name or "").lower()
    if document.mime_type != "application/pdf" and not filename.endswith(".pdf"):
        await message.reply_text("Please upload your resume as a PDF file.")
        return
    status = await message.reply_text("Reading your uploaded resume PDF… 📄")
    try:
        tg_file = await document.get_file()
        buffer = io.BytesIO()
        await tg_file.download_to_memory(out=buffer)
        resume_text = extract_text_from_pdf(buffer.getvalue())
        ctx.user_data["selected_resume_text"] = resume_text
        ctx.user_data["selected_resume_source"] = "uploaded"
        job_text = ctx.user_data.pop("pending_resume_choice_job", None)
        if job_text:
            await status.edit_text("Resume received. Tailoring this job application using your uploaded PDF…")
            await process_job_posting(update, ctx, job_text, resume_choice_confirmed=True)
        else:
            await status.edit_text(
                "Resume received and selected for your next application. I extracted the text successfully.\n"
                "Now send a job description, LinkedIn link, or screenshot."
            )
    except Exception as exc:
        await status.edit_text(f"I couldn't read that PDF: {exc}")


async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.effective_user.id == OWNER and update.effective_message:
        await update.effective_message.reply_text(
            "I can help you in these ways:\n"
            "• Say hi or ask a general question — I'll reply conversationally.\n"
            "• Send a job description as text, a public job-post link, or a screenshot.\n"
            "• I'll automatically select the best of your three Word templates: Data Engineer, AI Engineer, or Data Analyst.\n"
            "• You can upload a PDF if you want to use a custom resume for one application.\n"
            "• Send a public job-post link or upload a screenshot — I'll extract available text.\n"
            "• If the recruiter email is missing, I'll ask you for it.\n"
            "• I'll create a tailored PDF and show a simple job-fit score out of 10.\n"
            "• I'll email it only after you press the Send button."
        )


async def process_job_posting(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    job_text: str,
    resume_choice_confirmed: bool = False,
) -> None:
    """Select the best default role template, tailor it, and prepare an approval PDF."""
    message = update.effective_message
    if not message:
        return

    emails = re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", job_text)
    recipient = emails[0] if emails else ctx.user_data.get("pending_recipient")
    if not recipient:
        ctx.user_data["pending_job_text"] = job_text
        await message.reply_text(
            "What is the recruiter's email address? Send just the email address and I'll continue."
        )
        return

    ctx.user_data.pop("pending_job_text", None)
    ctx.user_data.pop("pending_recipient", None)
    status = await message.reply_text("Analysing the role and tailoring your resume…")

    try:
        info = extract(job_text)
        company = str(info.get("company", "") or "").strip()
        role = str(info.get("role", "") or "").strip()
        template_role = info.get("template_role", "data_analyst")
        if template_role not in TEMPLATES:
            template_role = "data_analyst"
        company_ctx = research(company)

        # An uploaded PDF can be used as a one-off custom source; otherwise use the
        # automatically selected, role-specific Word template.
        uploaded_text = ctx.user_data.get("selected_resume_text") if ctx.user_data.get("selected_resume_source") == "uploaded" else None
        if uploaded_text:
            decision = decide(job_text, company_ctx, uploaded_text)
            tailored_text, used_model = tailor(job_text, company_ctx, decision, uploaded_text)
            pdf = make_pdf(tailored_text)
            template_label = "Custom uploaded resume"
        else:
            if not TEMPLATES[template_role].exists():
                raise FileNotFoundError(
                    f"Missing template {TEMPLATES[template_role]}. Add all three Word templates to the GitHub templates/ folder."
                )
            active_resume = read_docx_text(TEMPLATES[template_role])
            decision = decide(job_text, company_ctx, active_resume)
            docx_bytes = tailor_docx(template_role, job_text, company_ctx, decision)
            pdf = docx_to_pdf(docx_bytes)
            used_model = "Gemini primary / Groq fallback"
            template_label = ROLE_LABELS[template_role]
        ctx.user_data.pop("selected_resume_text", None)
        ctx.user_data.pop("selected_resume_source", None)
    except Exception as exc:
        await status.edit_text(f"Sorry, I couldn't generate the resume: {str(exc)[:900]}")
        return

    filename = f"Adheeb_{safe_filename(ROLE_LABELS.get(template_role, role or 'Tailored_Resume'))}_{safe_filename(company) if company else 'Application'}.pdf"
    subject_role = role or ROLE_LABELS.get(template_role, "Job")
    subject_company = f" | {company}" if company else ""
    subject = f"Adheeb Ashim | Application for {subject_role}{subject_company}"
    key = str(message.message_id)
    score_100 = decision.get("fit_score", 0)
    try:
        fit_score_10 = round(float(score_100) / 10, 1)
    except (TypeError, ValueError):
        fit_score_10 = 0.0
    resume_used = template_label
    source = str(ctx.user_data.pop("application_source", "Job description"))
    try:
        sheet_row = log_application(
            company=company,
            role=role or ROLE_LABELS.get(template_role, ""),
            resume_used=resume_used,
            recipient=recipient,
            fit_score=f"{fit_score_10}/10",
            status="Awaiting approval",
            source=source,
            filename=filename,
        )
    except Exception as sheet_exc:
        sheet_row = None
        print(f"Google Sheets logging failed: {sheet_exc}")

    ctx.bot_data[key] = {
        "to": recipient, "pdf": pdf, "subject": subject, "filename": filename,
        "role": role or ROLE_LABELS.get(template_role, ""), "company": company,
        "resume_used": resume_used, "fit_score_10": fit_score_10,
        "sheet_row": sheet_row,
    }
    caption = (
        f"{role or ROLE_LABELS.get(template_role, 'Role not identified')} · {company or 'Company not identified'}\n"
        f"Resume used: {resume_used}\n"
        f"Job fit: {fit_score_10}/10"
    )[:1000]
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Send resume", callback_data=f"send:{key}"),
        InlineKeyboardButton("Cancel", callback_data=f"no:{key}"),
    ]])
    await status.delete()
    await message.reply_document(
        document=io.BytesIO(pdf), filename=filename, caption=caption, reply_markup=keyboard,
    )


async def on_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or user.id != OWNER or not message or not message.text:
        return

    text = message.text.strip()

    # If the bot previously asked for an email, treat this message as the email.
    pending_job = ctx.user_data.get("pending_job_text")
    if pending_job:
        emails = re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text)
        if emails:
            ctx.user_data["pending_recipient"] = emails[0]
            await process_job_posting(update, ctx, pending_job)
            return
        await message.reply_text(
            "Please send a valid recruiter email address, such as recruiter@example.com."
        )
        return

    # Try extracting details from a public job-post URL. This does not bypass login
    # walls, anti-bot protections, or other access controls.
    url = find_url(text)
    if url:
        status = await message.reply_text("Opening the public job-post link and extracting available details… 🔎")
        try:
            page_text = extract_public_page(url)
            if not is_likely_job_posting(page_text):
                await status.edit_text(
                    "I opened the link, but couldn't confidently identify a job posting. "
                    "Please paste the job description or send a screenshot of the post."
                )
                return
            # Keep the original URL as context; page text may omit the company/role.
            combined_text = f"Job-post URL: {url}\n\nExtracted public page text:\n{page_text}"
            ctx.user_data["application_source"] = "Job link"
            await status.delete()
            await process_job_posting(update, ctx, combined_text)
        except Exception as exc:
            await status.edit_text(
                "I couldn't extract enough text from that link. LinkedIn may require sign-in "
                "or block automated access. Please send a screenshot or copy/paste the post text.\n\n"
                f"Details: {str(exc)[:500]}"
            )
        return

    # A job description can include an email directly, or bot can ask for it.
    if is_likely_job_posting(text):
        ctx.user_data["application_source"] = "Text job description"
        await process_job_posting(update, ctx, text)
        return

    await interactive_reply(update, text)


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Download a screenshot, extract its text using Gemini vision, then process it."""
    user = update.effective_user
    message = update.effective_message
    if not user or user.id != OWNER or not message or not message.photo:
        return

    status = await message.reply_text("I got your screenshot 🖼️ Reading the text now...")
    try:
        photo = await message.photo[-1].get_file()
        buffer = io.BytesIO()
        await photo.download_to_memory(out=buffer)
        image_bytes = buffer.getvalue()
        extracted_text = extract_text_from_image(image_bytes, "image/jpeg")

        if not extracted_text.strip():
            await status.edit_text(
                "I couldn't read any text from that screenshot. Please send a clearer image."
            )
            return

        if is_likely_job_posting(extracted_text):
            ctx.user_data["application_source"] = "Screenshot"
            await status.edit_text("I extracted the screenshot text. Preparing your application...")
            await process_job_posting(update, ctx, extracted_text)
        else:
            preview = extracted_text[:3500]
            await status.edit_text(
                "Here's the text I could read from your screenshot:\n\n"
                f"{preview}\n\n"
                "If this is a job post, send another screenshot or paste the job description."
            )
    except Exception as exc:
        await status.edit_text(
            "I couldn't read that screenshot. Check that GEMINI_API_KEY is set correctly "
            f"in Render, then try again.\n\nDetails: {exc}"
        )


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    await query.answer()

    if not query.from_user or query.from_user.id != OWNER:
        return

    action, separator, key = (query.data or "").partition(":")
    if not separator:
        await query.message.reply_text("Invalid action. Please try again.")
        return

    item = ctx.bot_data.get(key)
    if not item:
        await query.message.reply_text(
            "This resume request has expired. Please send the job description again."
        )
        return

    if action == "send":
        try:
            # Keep the item until sending succeeds, so a transient email error
            # does not discard the generated PDF.
            send_mail(item["to"], item["subject"], item["pdf"], item["filename"], item.get("role", ""), item.get("company", ""))
            try:
                update_application_status(item.get("sheet_row"), "Sent")
            except Exception as sheet_exc:
                print(f"Google Sheets status update failed: {sheet_exc}")
            ctx.bot_data.pop(key, None)
            await query.message.reply_text(f"Resume sent to {item['to']}.")
        except Exception as exc:
            await query.message.reply_text(f"Email failed: {exc}")
    elif action == "no":
        try:
            update_application_status(item.get("sheet_row"), "Cancelled")
        except Exception as sheet_exc:
            print(f"Google Sheets status update failed: {sheet_exc}")
        ctx.bot_data.pop(key, None)
        await query.message.reply_text("Cancelled. No email was sent.")
    else:
        await query.message.reply_text("Unknown action.")


# ---------------------------------------------------------------------
# Application startup
# ---------------------------------------------------------------------
def main() -> None:
    # Render Web Service should provide RENDER_EXTERNAL_URL and PORT.
    base_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    port = int(os.environ.get("PORT", "10000"))

    if not base_url:
        raise RuntimeError(
            "RENDER_EXTERNAL_URL is missing. Deploy this as a Render Web "
            "Service, or configure the webhook URL before starting."
        )

    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("use_default", use_default_resume))
    application.add_handler(MessageHandler(filters.Document.PDF | (filters.Document.ALL), on_document))
    application.add_handler(MessageHandler(filters.PHOTO, on_photo))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_msg)
    )
    application.add_handler(CallbackQueryHandler(on_button))

    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=TOKEN,
        webhook_url=f"{base_url}/{TOKEN}",
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
