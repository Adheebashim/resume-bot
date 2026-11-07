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


_SIGNOFF_RE = re.compile(
    r"^\s*(?:warm regards|best regards|kind regards|best wishes|yours sincerely|yours faithfully|"
    r"sincerely|regards|respectfully|cheers|thanks|thank you)\s*[,!.]?\s*$",
    re.I,
)
_CONTACT_LINE_RE = re.compile(r"^\s*(?:email|e-mail|phone|mobile|linkedin|github|portfolio|location)\s*:", re.I)


def _signature(role: str = "") -> str:
    """Fixed signature built from settings, never from the AI's text."""
    return (
        "Warm regards,\n\n"
        f"{OWNER_NAME}\n"
        f"{role or 'Engineer'}\n"
        f"Email: {OWNER_EMAIL}\n"
        f"Phone: {OWNER_PHONE}\n"
        f"LinkedIn: {REFERENCE_LINKS.get('LinkedIn', '')}"
    )


def _enforce_signature(text: str, role: str = "") -> str:
    """Drop whatever sign-off/contact block the AI wrote and append the real one."""
    lines = text.rstrip().split("\n")
    cut = None
    # Prefer the last sign-off line within the final lines of the email.
    for i in range(len(lines) - 1, max(-1, len(lines) - 13), -1):
        if _SIGNOFF_RE.match(lines[i]):
            cut = i
            break
    if cut is None:
        # No sign-off found: trim trailing name/contact lines so we don't duplicate them.
        cut = len(lines)
        while cut > 0:
            last = lines[cut - 1].strip()
            if not last or _CONTACT_LINE_RE.match(last) or last.lower() == OWNER_NAME.lower():
                cut -= 1
            else:
                break
    body = "\n".join(lines[:cut]).rstrip()
    return f"{body}\n\n{_signature(role)}"


_NOT_HOME = r"(?!Nilambur|Kerala|India)"


def _clean_email_body(text: str, role: str = "", company: str = "", recruiter_name: Optional[str] = None) -> str:
    """Remove hallucinated locations and placeholders, and force the real signature."""
    # Fabricated residency: "Being based in Sydney, ..." / "Currently located in X, ..."
    text = re.sub(
        rf"\b(?:Being|Living|Residing|Currently)\s+(?:based|located|living|residing)\s+in\s+{_NOT_HOME}[^,\.\n]+,\s*",
        "", text, flags=re.I,
    )
    text = re.sub(rf"\bBased in\s+{_NOT_HOME}[^,\.\n]+,\s*(?=I\b)", "", text)
    text = re.sub(rf"\bAs a (?:resident|local) of\s+{_NOT_HOME}[^,\.\n]+,\s*", "", text, flags=re.I)
    # First-person claims without a leading clause: "I am based in Sydney."
    text = re.sub(
        rf"[^.\n]*\bI(?: am|'m|’m)\s+(?:currently\s+)?(?:based|located|living|residing)\s+in\s+{_NOT_HOME}[^.\n]*\.\s*",
        "", text, flags=re.I,
    )
    # Re-capitalise a sentence that lost its introductory clause.
    text = re.sub(r"(?<=\n\n)([a-z])", lambda m: m.group(1).upper(), text)

    linkedin_url = REFERENCE_LINKS.get("LinkedIn", "")
    portfolio_url = REFERENCE_LINKS.get("Portfolio", "")

    # Replace specific bracketed placeholders with real values
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
        (r"\[(?:Target\s*)?(?:Job\s*)?(?:Role|Position|Title)\]", role),
        (r"\[(?:Hiring\s*Manager|Recruiter)(?:['’]s)?(?:\s*Name)?\]", recruiter_name or "Hiring Team"),
    ]
    for pat, rep in replacements:
        text = re.sub(pat, rep, text, flags=re.I)

    # Catch-all: no square-bracket template text should ever reach a recruiter.
    text = re.sub(r"\[[^\]\n]{1,80}\]", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return _enforce_signature(text.strip(), role)


def generate_email_body(
    role: str,
    company: str,
    jd_summary: str,
    cover_letter: Optional[str] = None,
    recruiter_name: Optional[str] = None,
    fresh: bool = False,
) -> str:
    """
    Generate a SHORT personalized application email (about 80-110 words).
    The cover letter, when given, is only source material to condense.
    The signature is added by code (see _enforce_signature), never by the AI.
    fresh=True bypasses the response cache so a "rewrite" gives a new variation.
    """
    greeting = f"Dear {recruiter_name}," if recruiter_name else "Dear Hiring Team,"

    prompt = (
        f"Write a short, professional job application email body for {OWNER_NAME}.\n"
        f"Role: {role}\n"
        f"Company: {company}\n\n"
        f"Job details:\n{jd_summary[:1200]}\n\n"
        + (f"Source material (condense it, do not paste it):\n{cover_letter[:1800]}\n\n" if cover_letter else "")
        + "RULES:\n"
        f"1. Start exactly with: {greeting}\n"
        "2. LENGTH: at most 100 words after the greeting. Two short paragraphs, 2-3 sentences each.\n"
        "3. Paragraph 1: say you are applying for the role and name the 2 strongest, most relevant "
        "skills or achievements from the source material. Use only facts from it; never invent anything.\n"
        "4. Paragraph 2: one sentence that the resume is attached, one sentence offering to discuss.\n"
        f"5. LOCATION: the applicant is based in {OWNER_LOCATION}. Never say or imply the applicant lives in "
        "the company's city or country.\n"
        "6. NO signature, NO sign-off, NO contact details, NO square-bracket placeholders. "
        "Stop after the last sentence.\n"
        "7. Plain text only. No subject line. No markdown. English only."
    )
    if fresh:
        prompt += "\n8. Write a fresh variation with a different opening and different wording than usual."

    try:
        body, _ = ask(prompt, task="email", use_cache=not fresh)
        return _clean_email_body(body, role=role, company=company, recruiter_name=recruiter_name)
    except Exception:
        # Fallback if every LLM provider fails: still short and honest.
        position = f" for the {role} position" if role else ""
        at_company = f" at {company}" if company else ""
        fallback = (
            f"{greeting}\n\n"
            f"I am writing to apply{position}{at_company}. My background in data engineering, "
            f"analytics and AI applications aligns well with the role.\n\n"
            f"My resume is attached for your review, and I would be glad to discuss how I can contribute."
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
        message.set_content(_clean_email_body(custom_body, role=role, company=company))
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
