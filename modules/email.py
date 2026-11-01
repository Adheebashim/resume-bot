"""
Email Module - Personalized email generation & Gmail sending
Generates tailored email bodies with embedded cover letters formatted as clean text.
"""

import base64
import requests
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from typing import Optional

from config.settings import (
    GMAIL_CLIENT_ID,
    GMAIL_CLIENT_SECRET,
    GMAIL_REFRESH_TOKEN,
    GMAIL_SENDER,
    OWNER_NAME,
)
from modules.llm_provider import ask


def generate_email_body(
    role: str,
    company: str,
    jd_summary: str,
    cover_letter: Optional[str] = None,
    recruiter_name: Optional[str] = None,
) -> str:
    """
    Generate a personalized application email body.
    Includes the cover letter formatted naturally into the body text.
    """
    prompt = (
        f"Write a professional application email to send to a recruiter.\n"
        f"Applicant Name: {OWNER_NAME}\n"
        f"Role: {role}\n"
        f"Company: {company}\n"
        f"Recruiter Name: {recruiter_name or 'Hiring Manager'}\n\n"
        f"Job Details / Requirements:\n{jd_summary[:1500]}\n\n"
    )

    if cover_letter:
        prompt += (
            f"Embed and format this cover letter cleanly inside the email body:\n"
            f"{cover_letter}\n\n"
            "Format: Warm professional greeting, 1 brief intro sentence, then the cover letter text "
            "formatted with clean line breaks, ending with a polite sign-off mentioning the attached resume."
        )
    else:
        prompt += (
            "Write a personalized, concise email (3 short paragraphs) expressing enthusiasm for the role, "
            "highlighting 2 relevant strengths/achievements aligned with the job, and mentioning the attached resume."
        )

    prompt += "\nReturn ONLY the email body text in English. No subject line. No markdown."

    try:
        body, _ = ask(prompt, task="email")
        return body.strip()
    except Exception as exc:
        # Fallback if LLM call fails
        greeting = f"Dear {recruiter_name}," if recruiter_name else "Dear Hiring Team,"
        position = f" for the {role} position" if role else ""
        at_company = f" at {company}" if company else ""
        fallback = (
            f"{greeting}\n\n"
            f"I am writing to express my strong interest in the {role} position at {company}.\n\n"
        )
        if cover_letter:
            fallback += f"{cover_letter}\n\n"
        fallback += (
            f"Please find attached my resume{position}{at_company} for your review.\n\n"
            f"Best regards,\n{OWNER_NAME}"
        )
        return fallback


def send_mail(
    to: str,
    subject: str,
    pdf: bytes,
    filename: str,
    role: str = "",
    company: str = "",
    custom_body: Optional[str] = None,
) -> None:
    """Send a resume PDF through the Gmail API using OAuth refresh credentials."""
    required = {
        "GMAIL_CLIENT_ID": GMAIL_CLIENT_ID,
        "GMAIL_CLIENT_SECRET": GMAIL_CLIENT_SECRET,
        "GMAIL_REFRESH_TOKEN": GMAIL_REFRESH_TOKEN,
        "GMAIL_SENDER": GMAIL_SENDER,
    }
    missing = [name for name, val in required.items() if not val]
    if missing:
        raise RuntimeError(
            "Missing Gmail API environment variable(s): "
            + ", ".join(missing)
            + ". Add them in Render -> Environment."
        )

    token_resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": GMAIL_CLIENT_ID,
            "client_secret": GMAIL_CLIENT_SECRET,
            "refresh_token": GMAIL_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if not token_resp.ok:
        try:
            detail = token_resp.json()
        except Exception:
            detail = token_resp.text[:500]
        raise RuntimeError(
            f"Google OAuth token refresh failed ({token_resp.status_code}): {detail}"
        )

    access_token = token_resp.json().get("access_token")
    if not access_token:
        raise RuntimeError("Google OAuth did not return an access token.")

    message = EmailMessage()
    message["To"] = to
    sender_email = parseaddr(GMAIL_SENDER)[1] or GMAIL_SENDER
    message["From"] = formataddr((OWNER_NAME, sender_email))
    message["Subject"] = subject

    if custom_body:
        message.set_content(custom_body)
    else:
        greeting = "Dear Hiring Team,"
        position = f" for the {role} position" if role else ""
        at_company = f" at {company}" if company else ""
        message.set_content(
            f"{greeting}\n\n"
            f"Please find attached my resume{position}{at_company} for your consideration.\n\n"
            f"Regards,\n{OWNER_NAME}\n"
        )

    message.add_attachment(
        pdf,
        maintype="application",
        subtype="pdf",
        filename=filename,
    )

    raw_msg = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    resp = requests.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"raw": raw_msg},
        timeout=30,
    )
    if not resp.ok:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text[:500]
        raise RuntimeError(
            f"Gmail API send failed ({resp.status_code}): {detail}"
        )
