"""
Resume Bot v2 - Modular, with Inception LLM provider, smart routing,
personalized emails + cover letters, smart job matching, and recruiter outreach drafting.
"""

import io
import logging
import os
import re
import threading
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

from config.settings import (
    TOKEN,
    OWNER,
    BASE_URL,
    PORT,
    TEMPLATES,
    ROLE_LABELS,
    OWNER_NAME,
    RUNTIME_SETTINGS,
)
from modules.llm_provider import usage_report
from modules.resume_logic import (
    extract,
    research,
    decide,
    tailor,
    tailor_docx,
    read_docx_text,
    get_default_resume_text,
)
from modules.pdf_renderer import docx_to_pdf, make_pdf
from modules.email import generate_email_body, send_mail
from modules.cover_letter import generate_cover_letter
from modules.sheets import (
    log_application,
    update_application_status,
    update_application_row,
    sheet_url,
)
from modules.utils import (
    extract_text_from_image,
    extract_text_from_pdf,
    extract_public_page,
    find_url,
    is_likely_job_posting,
    safe_filename,
)
from modules.conversation import interactive_reply
from modules.job_matching import run_job_scan, format_job_alerts
import tornado.ioloop
import tornado.web
from modules.settings_store import apply_persisted_settings
from modules.web_dashboard import create_dashboard_app, log_job_processed, log_email_sent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("resume-bot")

RESUME = get_default_resume_text()


# ---------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != OWNER:
        if update.effective_message:
            await update.effective_message.reply_text("Sorry, this bot is private.")
        return
    if update.effective_message:
        await update.effective_message.reply_text(
            "Resume Bot is online!\n\n"
            "Say hi to chat, send a public job-post link or job description, or upload a screenshot. "
            "I can read the screenshot, ask for the recruiter email if needed, "
            "tailor your resume and ask before emailing it. "
            "I'll automatically choose among your Data Engineer, AI Engineer, and Data Analyst Word templates.\n"
            "New in v2: personalised email body + cover letter for every application, smart job "
            "alerts (/alerts), and recruiter outreach drafting (/outreach). Send /help for all options."
        )


async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.effective_user.id == OWNER and update.effective_message:
        await update.effective_message.reply_text(
            "I can help you in these ways:\n"
            "• Say hi or ask a general question — I'll reply conversationally.\n"
            "• Send a job description as text, a public job-post link, or a screenshot.\n"
            "• I'll automatically select the best of your three Word templates: Data Engineer, AI Engineer, or Data Analyst.\n"
            "• You can upload a PDF if you want to use a custom resume for one application.\n"
            "• I'll create a tailored PDF and show a simple job-fit score out of 10.\n"
            "• Personalized email body with a tailored cover letter for every application.\n"
            "• I'll email it only after you press Send.\n"
            "• Under each PDF, tap another template to regenerate it with that one.\n"
            "• /sheet — link to your tracking sheet.\n"
            "• /quota — AI usage today.\n"
            "• /alerts — scan for fresh matching jobs with recruiter emails.\n"
            "• /outreach — draft a LinkedIn cold-outreach or referral message."
        )


async def sheet_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or update.effective_user.id != OWNER or not update.effective_message:
        return
    url = sheet_url()
    await update.effective_message.reply_text(
        f"📋 Your applications sheet:\n{url}" if url else "Sheet tracking isn't set up (GOOGLE_SHEET_ID is missing)."
    )


async def quota_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or update.effective_user.id != OWNER or not update.effective_message:
        return
    await update.effective_message.reply_text(usage_report())


async def alerts_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual trigger: run a smart job scan and show matches."""
    if not update.effective_user or update.effective_user.id != OWNER or not update.effective_message:
        return
    message = update.effective_message
    status = await message.reply_text("Scanning job boards for fresh matches… 🔎")
    try:
        jobs = run_job_scan()
        await status.edit_text(format_job_alerts(jobs))
    except Exception as exc:
        await status.edit_text(f"Job scan failed: {str(exc)[:500]}")


async def outreach_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Draft outreach: looks for context in recent user_data (last job processed) so the
    user gets relevant recruiter options without re-typing the job.
    """
    if not update.effective_user or update.effective_user.id != OWNER or not update.effective_message:
        return
    message = update.effective_message
    last_job = ctx.user_data.get("last_job_text")
    last_company = ctx.user_data.get("last_company", "")
    last_role = ctx.user_data.get("last_role", "")

    # If we have a previous job, auto-detect recruiter from it
    if last_job:
        detected = detect_recruiter(last_job, last_company)
        name = detected["candidate_names"][0] if detected["candidate_names"] else None
        result = draft_outreach(
            role=last_role,
            company=last_company,
            recruiter_name=name,
            context=last_job[:1200],
            style="cold",
        )
        result["linkedin_profiles"] = detected["linkedin_profiles"]
        await message.reply_text(format_outreach_message(result))
        return

    await message.reply_text(
        "I can draft a perfect cold-outreach or referral message for a recruiter.\n"
        "Send a job description first (text, link, or screenshot), and I'll find recruiter "
        "details and draft both options for you to copy."
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
        extracted_text = extract_text_from_image(buffer.getvalue(), "image/jpeg")
        if not extracted_text.strip():
            await status.edit_text("I couldn't read any text from that screenshot. Please send a clearer image.")
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


async def on_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or user.id != OWNER or not message or not message.text:
        return

    text = message.text.strip()
    lowered = text.lower()

    # Quick keywords
    if len(lowered) <= 40:
        if "sheet" in lowered:
            await sheet_command(update, ctx)
            return
        if any(w in lowered for w in ("quota", "tokens", "usage")):
            await quota_command(update, ctx)
            return
        if any(w in lowered for w in ("alerts", "find jobs", "search jobs", "job alert", "matching")):
            await alerts_command(update, ctx)
            return
        if any(w in lowered for w in ("outreach", "recruiter", "referral", "cold", "linkedin")):
            await outreach_command(update, ctx)
            return

    # If we asked for an email, treat this message as the email
    pending_job = ctx.user_data.get("pending_job_text")
    if pending_job:
        emails = re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text)
        if emails:
            ctx.user_data["pending_recipient"] = emails[0]
            await process_job_posting(update, ctx, pending_job)
            return
        await message.reply_text("Please send a valid recruiter email address, such as recruiter@example.com.")
        return

    # Public job-post URL
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

    # Job description text
    if is_likely_job_posting(text):
        ctx.user_data["application_source"] = "Text job description"
        await process_job_posting(update, ctx, text)
        return

    # Anything else -> conversational reply
    await interactive_reply(message, text, ctx)


def _make_filename(template_role: str, company: str, role: str = "") -> str:
    return f"Adheeb_{safe_filename(ROLE_LABELS.get(template_role, role or 'Tailored_Resume'))}_{safe_filename(company) if company else 'Application'}.pdf"


def _make_caption(role: str, company: str, resume_used: str, fit_10, template_role: str) -> str:
    return (
        f"{role or ROLE_LABELS.get(template_role, 'Role not identified')} · {company or 'Company not identified'}\n"
        f"Resume used: {resume_used}\n"
        f"Job fit: {fit_10}/10"
    )[:1000]


def _approval_keyboard(key: str, template_role, switchable: bool) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton("Send resume", callback_data=f"send:{key}"),
        InlineKeyboardButton("Cancel", callback_data=f"no:{key}"),
    ]]
    if switchable and template_role in ROLE_LABELS:
        rows.append([
            InlineKeyboardButton(f"↻ {label}", callback_data=f"sw_{role_key}:{key}")
            for role_key, label in ROLE_LABELS.items() if role_key != template_role
        ])
    return InlineKeyboardMarkup(rows)


async def process_job_posting(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    job_text: str,
    resume_choice_confirmed: bool = False,
) -> None:
    """Select the best role template, tailor it, generate a personalized email + cover
    letter, and prepare an approval PDF."""
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
            docx_bytes, fit = tailor_docx(template_role, job_text, company_ctx, {})
            if fit is None:
                fit = decide(job_text, company_ctx, read_docx_text(TEMPLATES[template_role])).get("fit_score", 0)
            decision = {"fit_score": fit}
            pdf = docx_to_pdf(docx_bytes)
            used_model = "smart tiered AI provider"
            template_label = ROLE_LABELS[template_role]
        ctx.user_data.pop("selected_resume_text", None)
        ctx.user_data.pop("selected_resume_source", None)
    except Exception as exc:
        await status.edit_text(f"Sorry, I couldn't generate the resume: {str(exc)[:900]}")
        return

    # Store job context for the /outreach command
    ctx.user_data["last_job_text"] = job_text[:3000]
    ctx.user_data["last_company"] = company
    ctx.user_data["last_role"] = role

    # --- Personalized cover letter + email body (always generated) ---
    cover_letter_text = ""
    email_body = ""
    try:
        resume_for_letter = None
        if uploaded_text:
            resume_for_letter = uploaded_text
        else:
            resume_for_letter = read_docx_text(TEMPLATES[template_role])
        cover_letter_text = generate_cover_letter(
            role=role or ROLE_LABELS.get(template_role, "the role"),
            company=company,
            jd=job_text,
            resume_text=resume_for_letter,
            company_ctx=company_ctx,
        )
        email_body = generate_email_body(
            role=role or ROLE_LABELS.get(template_role, "the role"),
            company=company,
            jd_summary=job_text[:1500],
            cover_letter=cover_letter_text,
        )
    except Exception as exc:
        log.warning("Cover letter / email generation failed: %s", exc)
        email_body = ""

    filename = _make_filename(template_role, company, role)
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
        log_job_processed(company=company, role=role or ROLE_LABELS.get(template_role, ""), fit=f"{fit_score_10}/10", status="Awaiting Approval")
    except Exception as sheet_exc:
        sheet_row = None
        log_job_processed(company=company, role=role or ROLE_LABELS.get(template_role, ""), fit=f"{fit_score_10}/10", status="Awaiting Approval")
        print(f"Google Sheets logging failed: {sheet_exc}")

    ctx.bot_data[key] = {
        "to": recipient, "pdf": pdf, "subject": subject, "filename": filename,
        "role": role or ROLE_LABELS.get(template_role, ""), "company": company,
        "resume_used": resume_used, "fit_score_10": fit_score_10,
        "sheet_row": sheet_row,
        "job_text": job_text, "company_ctx": company_ctx, "role_raw": role,
        "template_role": None if uploaded_text else template_role,
        "email_body": email_body,
        "cover_letter_text": cover_letter_text,
    }

    caption = _make_caption(role, company, resume_used, fit_score_10, template_role)
    cover_note = "\n✉️ Personalized email + cover letter will be included in the email body." if email_body else ""
    caption = (caption + cover_note)[:1000]
    keyboard = _approval_keyboard(key, None if uploaded_text else template_role, not uploaded_text)
    await status.delete()
    await message.reply_document(
        document=io.BytesIO(pdf), filename=filename, caption=caption, reply_markup=keyboard,
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
        await query.message.reply_text("This resume request has expired. Please send the job description again.")
        return

    if action == "send":
        try:
            body = item.get("email_body")
            send_mail(
                item["to"], item["subject"], item["pdf"], item["filename"],
                item.get("role", ""), item.get("company", ""), custom_body=body,
            )
            log_email_sent()
            try:
                update_application_status(item.get("sheet_row"), "Sent")
            except Exception as sheet_exc:
                print(f"Google Sheets status update failed: {sheet_exc}")
            ctx.bot_data.pop(key, None)
            await query.message.reply_text(f"Resume sent to {item['to']}.")
        except Exception as exc:
            await query.message.reply_text(f"Email failed: {exc}")
    elif action.startswith("sw_"):
        new_role = action[3:]
        if new_role not in TEMPLATES or not item.get("template_role") or not item.get("job_text"):
            await query.message.reply_text("I can't switch the template for this request.")
            return
        if new_role == item["template_role"]:
            await query.message.reply_text("That template is already in use.")
            return
        status = await query.message.reply_text(f"Switching to the {ROLE_LABELS[new_role]} template…")
        try:
            docx_bytes, fit = tailor_docx(new_role, item["job_text"], item.get("company_ctx", ""), {})
            if fit is None:
                fit = decide(item["job_text"], "", read_docx_text(TEMPLATES[new_role])).get("fit_score", 0)
            pdf = docx_to_pdf(docx_bytes)
        except Exception as exc:
            await status.edit_text(f"Couldn't switch template: {str(exc)[:600]}")
            return

        # Regenerate cover letter + email body for the new template
        try:
            new_resume = read_docx_text(TEMPLATES[new_role])
            new_cover = generate_cover_letter(
                role=item.get("role_raw") or ROLE_LABELS[new_role],
                company=item.get("company", ""),
                jd=item["job_text"],
                resume_text=new_resume,
                company_ctx=item.get("company_ctx", ""),
            )
            item["email_body"] = generate_email_body(
                role=item.get("role_raw") or ROLE_LABELS[new_role],
                company=item.get("company", ""),
                jd_summary=item["job_text"][:1500],
                cover_letter=new_cover,
            )
        except Exception:
            pass

        company = item.get("company", "")
        role_shown = item.get("role_raw") or ROLE_LABELS[new_role]
        fit_10 = round(float(fit) / 10, 1)
        item.update({
            "pdf": pdf,
            "template_role": new_role,
            "resume_used": ROLE_LABELS[new_role],
            "fit_score_10": fit_10,
            "role": role_shown,
            "filename": _make_filename(new_role, company, item.get("role_raw", "")),
            "subject": f"Adheeb Ashim | Application for {role_shown}" + (f" | {company}" if company else ""),
        })
        try:
            update_application_row(
                item.get("sheet_row"),
                role=None if item.get("role_raw") else role_shown,
                resume_used=item["resume_used"], fit_score=f"{fit_10}/10", filename=item["filename"],
            )
        except Exception as sheet_exc:
            print(f"Google Sheets update failed: {sheet_exc}")
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await status.delete()
        await query.message.reply_document(
            document=io.BytesIO(pdf), filename=item["filename"],
            caption=_make_caption(item.get("role_raw", ""), company, item["resume_used"], fit_10, new_role),
            reply_markup=_approval_keyboard(key, new_role, True),
        )
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
# Background job scanning (respects ALERT_INTERVAL_MIN throttle)
# ---------------------------------------------------------------------
def _job_scan_loop() -> None:
    """Continuously scan for matching jobs and send alerts if enabled.

    Reads JOB_SCAN_LOOP and ALERT_INTERVAL_MIN from RUNTIME_SETTINGS on every
    iteration, so changes made from the Web Dashboard take effect immediately
    without restarting the bot.
    """
    while True:
        # Dynamic pause/interval: pause 15s between loops so dashboard
        # changes are picked up quickly (cheap check).
        if not RUNTIME_SETTINGS.get("JOB_SCAN_LOOP", True):
            time.sleep(15)
            continue
        interval_min = max(1, int(RUNTIME_SETTINGS.get("ALERT_INTERVAL_MIN", 15)))
        time.sleep(interval_min * 60)
        try:
            jobs = run_job_scan()
            if jobs:
                log.info("Job scan: %d fresh matches found", len(jobs))
        except Exception as exc:
            log.info("Job scan skipped: %s", exc)


# ---------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------
def _keep_alive(base_url: str) -> None:
    """Ping our own public URL so Render's free tier doesn't spin the bot down."""
    interval = max(60, int(os.environ.get("KEEP_ALIVE_SECONDS", "600")))
    while True:
        time.sleep(interval)
        try:
            import requests
            requests.get(base_url + "/", timeout=20)
        except Exception:
            pass


def main() -> None:
    if not BASE_URL:
        raise RuntimeError(
            "RENDER_EXTERNAL_URL is missing. Deploy this as a Render Web "
            "Service, or configure the webhook URL before starting."
        )

    # Load settings persisted in Google Sheets (survives redeploys). Must run
    # before the job scan loop and dashboard start.
    try:
        apply_persisted_settings()
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not load persisted settings: %s", exc)

    async def post_init(app):
        # Attach Web Dashboard handlers to PTB's Tornado server
        if hasattr(app.updater, "web_app") and app.updater.web_app:
            from modules.web_dashboard import (
                DashboardHandler,
                StatusApiHandler,
                SettingsApiHandler,
                TriggerScanApiHandler,
                TestProvidersApiHandler,
            )
            app.updater.web_app.add_handlers(r".*", [
                (r"/", DashboardHandler),
                (r"/api/status", StatusApiHandler),
                (r"/api/settings", SettingsApiHandler),
                (r"/api/trigger-scan", TriggerScanApiHandler),
                (r"/api/test-providers", TestProvidersApiHandler),
            ])
            log.info("Web Dashboard registered at %s/", BASE_URL)

    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("use_default", use_default_resume))
    application.add_handler(CommandHandler("sheet", sheet_command))
    application.add_handler(CommandHandler("quota", quota_command))
    application.add_handler(CommandHandler("alerts", alerts_command))
    application.add_handler(CommandHandler("outreach", outreach_command))
    application.add_handler(MessageHandler(filters.Document.PDF | (filters.Document.ALL), on_document))
    application.add_handler(MessageHandler(filters.PHOTO, on_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_msg))
    application.add_handler(CallbackQueryHandler(on_button))

    if os.environ.get("KEEP_ALIVE", "1") != "0":
        threading.Thread(target=_keep_alive, args=(BASE_URL,), daemon=True).start()

    # Background job scanning (disabled in dev unless JOB_SCAN_LOOP=1)
    # Background job scanning (runtime-controlled from the Web Dashboard)
    threading.Thread(target=_job_scan_loop, daemon=True).start()

    log.info("Starting webhook server on 0.0.0.0:%s (public URL %s)", PORT, BASE_URL)
    log.info("LLM providers: Inception + Gemini + Groq (smart routing)")
    log.info(
        "Job scan loop: %s · alert interval: every %d min",
        "enabled" if RUNTIME_SETTINGS.get("JOB_SCAN_LOOP", True) else "disabled",
        int(RUNTIME_SETTINGS.get("ALERT_INTERVAL_MIN", 15)),
    )

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TOKEN,
        webhook_url=f"{BASE_URL}/{TOKEN}",
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
