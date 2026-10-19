import os
import re
import io
import json
import base64
import requests
from html.parser import HTMLParser
from urllib.parse import urlparse
import ipaddress
import socket
from email.message import EmailMessage

from xml.sax.saxutils import escape
from ddgs import DDGS

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
from pypdf import PdfReader

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

with open("resume.txt", "r", encoding="utf-8") as resume_file:
    RESUME = resume_file.read()

GROQ_MODEL = "llama-3.3-70b-versatile"
GEMINI_MODEL = "gemini-2.5-flash"


# ---------------------------------------------------------------------
# LLM providers with failover
# ---------------------------------------------------------------------
def _groq(prompt: str) -> str:
    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_KEY}"},
        json={
            "model": GROQ_MODEL,
            "temperature": 0.3,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=90,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def _gemini(prompt: str) -> str:
    response = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent",
        headers={"x-goog-api-key": GEMINI_KEY},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=90,
    )
    response.raise_for_status()
    return response.json()["candidates"][0]["content"]["parts"][0]["text"]


def ask(prompt: str):
    providers = []
    if GROQ_KEY:
        providers.append(("Groq", _groq))
    if GEMINI_KEY:
        providers.append(("Gemini", _gemini))

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
    response = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent",
        headers={"x-goog-api-key": GEMINI_KEY},
        json={
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
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": encoded_image,
                        }
                    },
                ]
            }]
        },
        timeout=90,
    )
    response.raise_for_status()
    data = response.json()
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()



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
            "• Upload a resume PDF to use it for a specific application, or use /use_default for the saved resume.\n"
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
# Job parsing, company research, and fit decision
# ---------------------------------------------------------------------
def extract(jd: str) -> dict:
    prompt = (
        'From this job posting return ONLY valid JSON: '
        '{"company": "", "role": "", "key_skills": []}\n\n'
        + jd
    )
    try:
        return parse_json(ask(prompt)[0])
    except Exception:
        return {}


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
    prompt = f"""You are a hiring-fit analyst. Compare the resume to the job and
company information. Return ONLY valid JSON:
{{"fit_score": 0, "verdict": "apply|stretch|skip",
 "angle": "one sentence on how to position the candidate",
 "emphasize": ["resume items to put first"],
 "gaps": ["missing requirements"]}}
Use a fit_score from 0 to 100. Be honest and do not inflate the score.

RESUME:
{resume_text}

COMPANY INFO (web snippets may be incomplete):
{company_ctx or 'none found'}

JOB DESCRIPTION:
{jd}"""

    try:
        result = parse_json(ask(prompt)[0])
        if "fit_score" in result:
            try:
                result["fit_score"] = max(0, min(100, int(result["fit_score"])))
            except (ValueError, TypeError):
                result["fit_score"] = "?"
        return result
    except Exception:
        return {}


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

POSITIONING: {decision.get('angle', '')}
PUT FIRST: {decision.get('emphasize', [])}

RESUME:
{resume_text}

COMPANY INFO:
{company_ctx or 'none'}

JOB DESCRIPTION:
{jd}"""
    return ask(prompt)


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


def send_mail(to: str, subject: str, pdf: bytes) -> None:
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
    message["From"] = GMAIL_SENDER
    message["Subject"] = subject
    message.set_content(
        "Hello,\n\n"
        "Please find my resume attached for your consideration.\n\n"
        "Regards"
    )
    message.add_attachment(
        pdf,
        maintype="application",
        subtype="pdf",
        filename="Resume.pdf",
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
            "assess fit, tailor your resume, and ask before emailing it. "
            "Upload a resume PDF for a specific application, or use /use_default to use your saved resume. Send /help for all options."
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
            "• Upload a resume PDF to use it for a specific application, or use /use_default for the saved resume.\n"
            "• Send a public job-post link or upload a screenshot — I'll extract available text.\n"
            "• If the recruiter email is missing, I'll ask you for it.\n"
            "• I'll research the company, score the fit, tailor your resume, and create a PDF.\n"
            "• I'll email it only after you press the Send button."
        )


async def process_job_posting(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    job_text: str,
    resume_choice_confirmed: bool = False,
) -> None:
    """Extract job details, assess fit, tailor the selected resume, and prepare a PDF."""
    message = update.effective_message
    if not message:
        return

    if not resume_choice_confirmed and not ctx.user_data.get("selected_resume_text"):
        ctx.user_data["pending_resume_choice_job"] = job_text
        await message.reply_text(
            "Job details received. Which resume should I tailor?\n\n"
            "📎 Upload a PDF to use that resume, or send /use_default to use your saved resume.txt."
        )
        return

    active_resume = ctx.user_data.get("selected_resume_text", RESUME)
    emails = re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", job_text)
    recipient = emails[0] if emails else ctx.user_data.get("pending_recipient")
    if not recipient:
        ctx.user_data["pending_job_text"] = job_text
        await message.reply_text(
            "I extracted the job details. 📄\n\n"
            "What is the recruiter's email address? Send just the email address "
            "and I'll continue."
        )
        return

    ctx.user_data.pop("pending_job_text", None)
    ctx.user_data.pop("pending_recipient", None)

    status = await message.reply_text(
        "Got it! 🔎 Researching the company and tailoring your resume. This may take a minute..."
    )

    try:
        info = extract(job_text)
        company = str(info.get("company", "") or "")
        role = str(info.get("role", "") or "")
        company_ctx = research(company)
        decision = decide(job_text, company_ctx, active_resume)
        resume_text, used_model = tailor(job_text, company_ctx, decision, active_resume)
        pdf = make_pdf(resume_text)
        ctx.user_data.pop("selected_resume_text", None)
        ctx.user_data.pop("selected_resume_source", None)
    except Exception as exc:
        await status.edit_text(f"Sorry, I couldn't generate the resume: {exc}")
        return

    key = str(message.message_id)
    subject = f"Application for {role}" if role else "Job Application - Resume"
    ctx.bot_data[key] = {"to": recipient, "pdf": pdf, "subject": subject}

    gaps = decision.get("gaps", [])
    if not isinstance(gaps, list):
        gaps = [str(gaps)] if gaps else []

    caption = (
        f"{role or 'Role not identified'} at {company or 'Company not identified'}\n"
        f"Fit: {decision.get('fit_score', '?')}/100 "
        f"({decision.get('verdict', '?')})\n"
        f"Angle: {decision.get('angle', '-')}\n"
        f"Gaps: {', '.join(map(str, gaps)) or '-'}\n"
        f"Model: {used_model}"
    )[:1000]

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"Send to {recipient}", callback_data=f"send:{key}"),
        InlineKeyboardButton("Cancel", callback_data=f"no:{key}"),
    ]])

    await status.delete()
    await message.reply_document(
        document=io.BytesIO(pdf),
        filename="Resume.pdf",
        caption=caption,
        reply_markup=keyboard,
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
            send_mail(item["to"], item["subject"], item["pdf"])
            ctx.bot_data.pop(key, None)
            await query.message.reply_text(f"Resume emailed to {item['to']}.")
        except Exception as exc:
            await query.message.reply_text(f"Email failed: {exc}")
    elif action == "no":
        ctx.bot_data.pop(key, None)
        await query.message.reply_text("Cancelled. The email was not sent.")
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
