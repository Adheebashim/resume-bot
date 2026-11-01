"""
Email Module - Personalized email generation & Gmail sending
Generates tailored email bodies with embedded cover letters formatted as clean text.
"""

import base64
import re
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
    OWNER_EMAIL,
    OWNER_PHONE,
    OWNER_LOCATION,
    REFERENCE_LINKS,
)
from modules.llm_provider import ask


def _clean_email_body(text: str, role: str = "", company: str = "", recruiter_name: Optional[str] = None) -> str:
    """Clean hallucinated locations and substitute any square-bracket placeholders with actual values."""
    # Remove fabricated city residency like "Being based in Sydney, "
    text = re.sub(r"\bBeing based in (?!Nilambur|Kerala|India)[^,\.\n]+,\s*", "", text, flags=re.I)
    text = re.sub(r"\bAs a (?:resident|local) of (?!Nilambur|Kerala|India)[^,\.\n]+,\s*", "", text, flags=re.I)
    text = re.sub(r"(?<=\n\n)([a-z])", lambda m: m.group(1).upper(), text)

    linkedin_url = REFERENCE_LINKS.get("LinkedIn", "")
    portfolio_url = REFERENCE_LINKS.get("Portfolio", "")

    # Replace specific bracketed placeholders with real contact info
    replacements = [
        (r"\[(?:Your\s*)?Email(?:\s*Address)?\]", OWNER_EMAIL),
        (r"\[(?:Your\s*)?Phone(?:\s*Number)?\]", OWNER_PHONE),
        (r"\[(?:Your\s*)?LinkedIn(?:\s*Profile)?(?:\s*URL)?\]", linkedin_url),
        (r"\[(?:Your\s*)?Portfolio(?:\s*URL)?\]", portfolio_url),
        (r"\[(?:Your\s*)?GitHub(?:\s*URL)?\]", REFERENCE_LINKS.get("GitHub", "")),
        (r"\[(?:Your\s*)?Location\]", OWNER_LOCATION),
        (r"\[(?:Your\s*)?Name\]", OWNER_NAME),
        (r"\[(?:Insert\s*)?Date\]", ""),
        (r"\[Company(?:\s*Name)?\]", company),
        (r"\[(?:Target\s*)?Role\]", role),
        (r"\[(?:Hiring\s*Manager|Recruiter)(?:\s*Name)?\]", recruiter_name or "Hiring Team"),
    ]
    for pat, rep in replacements:
        text = re.sub(pat, rep, text, flags=re.I)

    # Clean any lingering [Your ...] or [Insert ...] artifacts
    text = re.sub(r"\[(?:Your|Insert)\s+[^\]]+\]", "", text, flags=re.I)
    return text.strip()


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
    contact_info = (
        f"Applicant Name: {OWNER_NAME}\n"
        f"Applicant Email: {OWNER_EMAIL}\n"
        f"Applicant Phone: {OWNER_PHONE}\n"
        f"Applicant Location: {OWNER_LOCATION}\n"
        f"Applicant LinkedIn: {REFERENCE_LINKS.get('LinkedIn', '')}\n"
        f"Applicant Portfolio: {REFERENCE_LINKS.get('Portfolio', '')}\n"
    )

    prompt = (
        f"Write a professional application email to send to a recruiter.\n"
        f"{contact_info}\n"
        f"Role: {role}\n"
        f"Company: {company}\n"
        f"Recruiter Name: {recruiter_name or 'Hiring Manager'}\n\n"
        f"Job Details / Requirements:\n{jd_summary[:1500]}\n\n"
        f"CRITICAL RULES:\n"
        f"1. LOCATION: The applicant is based in {OWNER_LOCATION}. "
        f"NEVER state or imply that the applicant is based in, living in, or a local resident of the company's city/country "
        f"(e.g. NEVER write 'Being based in Sydney' or 'As a resident of...').\n"
        f"2. NO PLACEHOLDERS: NEVER write bracketed placeholders like [Your Email Address], [Your Phone Number], [Your LinkedIn Profile URL]. "
        f"Use the actual applicant contact details provided above.\n"
    )

    if cover_letter:
        prompt += (
            f"Embed and format this cover letter cleanly inside the email body:\n"
            f"{cover_letter}\n\n"
            "Format: Warm professional greeting, 1 brief intro sentence, then the cover letter text "
            "formatted with clean line breaks, ending with a polite sign-off mentioning the attached resume.\n"
            f"Signature format:\nWarm regards,\n\n{OWNER_NAME}\n{role or 'Engineer'}\n"
            f"Email: {OWNER_EMAIL}\nPhone: {OWNER_PHONE}\nLinkedIn: {REFERENCE_LINKS.get('LinkedIn', '')}\n"
        )
    else:
        prompt += (
            "Write a personalized, concise email (3 short paragraphs) expressing enthusiasm for the role, "
            "highlighting 2 relevant strengths/achievements aligned with the job, and mentioning the attached resume.\n"
            f"Signature format:\nWarm regards,\n\n{OWNER_NAME}\n{role or 'Engineer'}\n"
            f"Email: {OWNER_EMAIL}\nPhone: {OWNER_PHONE}\nLinkedIn: {REFERENCE_LINKS.get('LinkedIn', '')}\n"
        )

    prompt += "\nReturn ONLY the email body text in English. No subject line. No markdown."

    try:
        body, _ = ask(prompt, task="email")
        return _clean_email_body(body, role=role, company=company, recruiter_name=recruiter_name)
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
            f"Best regards,\n\n"
            f"{OWNER_NAME}\n"
            f"Email: {OWNER_EMAIL}\n"
            f"Phone: {OWNER_PHONE}\n"
            f"LinkedIn: {REFERENCE_LINKS.get('LinkedIn', '')}\n"
        )
        return _clean_email_body(fallback, role=role, company=company, recruiter_name=recruiter_name)


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
