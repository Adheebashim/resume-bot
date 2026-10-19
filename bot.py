import os, re, io, json, smtplib, requests
from xml.sax.saxutils import escape
from email.message import EmailMessage
from ddgs import DDGS
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (ApplicationBuilder, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters)

TOKEN = os.environ["BOT_TOKEN"]
OWNER = int(os.environ["OWNER_ID"])
GROQ_KEY = os.environ.get("GROQ_API_KEY")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASS = os.environ["SMTP_PASS"]
RESUME = open("resume.txt", encoding="utf-8").read()

GROQ_MODEL = "llama-3.3-70b-versatile"
GEMINI_MODEL = "gemini-2.5-flash"  # check AI Studio for the current name


def _groq(prompt):
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_KEY}"},
        json={"model": GROQ_MODEL, "temperature": 0.3,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=90)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _gemini(prompt):
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        headers={"x-goog-api-key": GEMINI_KEY},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=90)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def ask(prompt):
    providers = []
    if GROQ_KEY:
        providers.append(("Groq", _groq))
    if GEMINI_KEY:
        providers.append(("Gemini", _gemini))
    errors = []
    for name, fn in providers:
        try:
            return fn(prompt), name
        except Exception as e:
            errors.append(f"{name}: {e}")
    raise RuntimeError("All LLMs failed - " + "; ".join(errors))


def parse_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else {}


def extract(jd):
    p = ('From this job posting return ONLY JSON: '
         '{"company": "", "role": "", "key_skills": []}\n\n' + jd)
    try:
        return parse_json(ask(p)[0])
    except Exception:
        return {}


def research(company):
    if not company:
        return ""
    try:
        res = DDGS().text(f"{company} company about products", max_results=5)
        return "\n".join(f"- {r['title']}: {r['body']}" for r in res)
    except Exception:
        return ""


def decide(jd, company_ctx):
    p = f"""You are a hiring-fit analyst. Compare the resume to the job and
company info. Return ONLY JSON:
{{"fit_score": 0-100, "verdict": "apply|stretch|skip",
 "angle": "one sentence on how to position the candidate",
 "emphasize": ["resume items to put first"],
 "gaps": ["missing requirements"]}}
Be honest, do not inflate the score.

RESUME:
{RESUME}

COMPANY INFO (web snippets, may be incomplete):
{company_ctx or 'none found'}

JOB DESCRIPTION:
{jd}"""
    try:
        return parse_json(ask(p)[0])
    except Exception:
        return {}


def tailor(jd, company_ctx, decision):
    p = f"""Tailor the resume below to the job description.
Rules: use ONLY facts in the resume. Never invent skills, employers,
dates or numbers. You may reorder, rephrase and emphasise relevant
items and mirror JD keywords where truthful. Use company info only to
choose relevant emphasis; do not state unverified claims about the
company. Output plain text only, no markdown, one page.

POSITIONING: {decision.get('angle', '')}
PUT FIRST: {decision.get('emphasize', [])}

RESUME:
{RESUME}

COMPANY INFO:
{company_ctx or 'none'}

JOB DESCRIPTION:
{jd}"""
    return ask(p)


def make_pdf(text):
    buf = io.BytesIO()
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=40,
                            rightMargin=40, topMargin=36, bottomMargin=36)
    story = []
    for line in text.split("\n"):
        story.append(Paragraph(escape(line), styles["Normal"]) if line.strip()
                     else Spacer(1, 6))
    doc.build(story)
    return buf.getvalue()


def send_mail(to, subject, pdf):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = SMTP_USER, to, subject
    m.set_content("Hello,\n\nPlease find my resume attached for your consideration.\n\nRegards")
    m.add_attachment(pdf, maintype="application", subtype="pdf",
                     filename="Resume.pdf")
    with smtplib.SMTP_SSL(SMTP_HOST, 465) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(m)


async def on_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER:
        return
    text = update.message.text or ""
    found = re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    if not found:
        await update.message.reply_text("No email found in your message.")
        return
    to = found[0]
    await update.message.reply_text("Researching and tailoring...")
    try:
        info = extract(text)
        company, role = info.get("company", ""), info.get("role", "")
        company_ctx = research(company)
        decision = decide(text, company_ctx)
        resume_text, used = tailor(text, company_ctx, decision)
        pdf = make_pdf(resume_text)
    except Exception as e:
        await update.message.reply_text(f"Failed: {e}")
        return
    key = str(update.message.message_id)
    subject = f"Application for {role}" if role else "Application - Resume"
    ctx.bot_data[key] = {"to": to, "pdf": pdf, "subject": subject}
    caption = (f"{role} at {company}\n"
               f"Fit: {decision.get('fit_score', '?')}/100 "
               f"({decision.get('verdict', '?')})\n"
               f"Angle: {decision.get('angle', '-')}\n"
               f"Gaps: {', '.join(decision.get('gaps', [])) or '-'}\n"
               f"Model: {used}")[:1000]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"Send to {to}", callback_data=f"send:{key}"),
        InlineKeyboardButton("Cancel", callback_data=f"no:{key}")]])
    await update.message.reply_document(io.BytesIO(pdf),
        filename="Resume.pdf", caption=caption, reply_markup=kb)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != OWNER:
        return
    action, key = q.data.split(":")
    item = ctx.bot_data.pop(key, None)
    if not item:
        await q.message.reply_text("Expired. Please send the JD again.")
    elif action == "send":
        try:
            send_mail(item["to"], item["subject"], item["pdf"])
            await q.message.reply_text(f"Sent to {item['to']}")
        except Exception as e:
            await q.message.reply_text(f"Email failed: {e}")
    else:
        await q.message.reply_text("Cancelled.")


app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_msg))
app.add_handler(CallbackQueryHandler(on_button))

if __name__ == "__main__":
    base = os.environ["RENDER_EXTERNAL_URL"]
    app.run_webhook(listen="0.0.0.0", port=int(os.environ.get("PORT", 10000)),
                    url_path=TOKEN, webhook_url=f"{base}/{TOKEN}")
