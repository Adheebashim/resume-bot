import os
import re
import io
import json
import smtplib
import requests

from xml.sax.saxutils import escape
from email.message import EmailMessage
from ddgs import DDGS

from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

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

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASS = os.environ["SMTP_PASS"]

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


def decide(jd: str, company_ctx: str) -> dict:
    prompt = f"""You are a hiring-fit analyst. Compare the resume to the job and
company information. Return ONLY valid JSON:
{{"fit_score": 0, "verdict": "apply|stretch|skip",
 "angle": "one sentence on how to position the candidate",
 "emphasize": ["resume items to put first"],
 "gaps": ["missing requirements"]}}
Use a fit_score from 0 to 100. Be honest and do not inflate the score.

RESUME:
{RESUME}

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


def tailor(jd: str, company_ctx: str, decision: dict):
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
{RESUME}

COMPANY INFO:
{company_ctx or 'none'}

JOB DESCRIPTION:
{jd}"""
    return ask(prompt)


# ---------------------------------------------------------------------
# PDF and email
# ---------------------------------------------------------------------
def make_pdf(text: str) -> bytes:
    buffer = io.BytesIO()
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=40,
        rightMargin=40,
        topMargin=36,
        bottomMargin=36,
    )

    story = []
    for line in text.splitlines():
        if line.strip():
            story.append(Paragraph(escape(line), styles["Normal"]))
        else:
            story.append(Spacer(1, 6))

    doc.build(story)
    return buffer.getvalue()


def send_mail(to: str, subject: str, pdf: bytes) -> None:
    message = EmailMessage()
    message["From"] = SMTP_USER
    message["To"] = to
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

    with smtplib.SMTP_SSL(SMTP_HOST, 465, timeout=30) as smtp:
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(message)


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
            "Send a message containing the job description and the "
            "recruiter's email address. I'll research the company, assess "
            "fit, generate a tailored PDF, and ask before emailing it."
        )


async def on_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message

    if not user or user.id != OWNER or not message or not message.text:
        return

    text = message.text.strip()
    found_emails = re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text)

    if not found_emails:
        await message.reply_text(
            "I couldn't find an email address. Send the job description "
            "and include the recruiter's email address in the same message."
        )
        return

    recipient = found_emails[0]
    status = await message.reply_text(
        "Researching the company and tailoring your resume. This may take "
        "a minute..."
    )

    try:
        info = extract(text)
        company = str(info.get("company", "") or "")
        role = str(info.get("role", "") or "")
        company_ctx = research(company)
        decision = decide(text, company_ctx)
        resume_text, used_model = tailor(text, company_ctx, decision)
        pdf = make_pdf(resume_text)
    except Exception as exc:
        await status.edit_text(f"Failed to generate the resume: {exc}")
        return

    key = str(message.message_id)
    subject = f"Application for {role}" if role else "Job Application - Resume"

    ctx.bot_data[key] = {
        "to": recipient,
        "pdf": pdf,
        "subject": subject,
    }

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

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                f"Send to {recipient}",
                callback_data=f"send:{key}",
            ),
            InlineKeyboardButton(
                "Cancel",
                callback_data=f"no:{key}",
            ),
        ]]
    )

    await status.delete()
    await message.reply_document(
        document=io.BytesIO(pdf),
        filename="Resume.pdf",
        caption=caption,
        reply_markup=keyboard,
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
