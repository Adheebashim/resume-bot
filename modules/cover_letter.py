"""
Cover Letter Module - Generates tailored cover letters for all applications.
Formats as clean text to be embedded directly into the email body.
Adapts length dynamically based on the job role and complexity.
"""

import re
from typing import Optional
from config.settings import (
    OWNER_NAME,
    OWNER_EMAIL,
    OWNER_PHONE,
    OWNER_LOCATION,
    REFERENCE_LINKS,
)
from modules.llm_provider import ask, _clean


def _clean_cover_letter(text: str) -> str:
    """Strip hallucinated location claims and placeholder brackets."""
    # Strip fabricated residency like "Being based in Sydney, "
    text = re.sub(r"\bBeing based in (?!Nilambur|Kerala|India)[^,\.\n]+,\s*", "", text, flags=re.I)
    text = re.sub(r"\bAs a (?:resident|local) of (?!Nilambur|Kerala|India)[^,\.\n]+,\s*", "", text, flags=re.I)
    # Capitalize start of paragraph if we stripped an introductory clause
    text = re.sub(r"(?<=\n\n)([a-z])", lambda m: m.group(1).upper(), text)
    # Replace any leftover bracket placeholders
    replacements = [
        (r"\[(?:Your\s*)?Email(?:\s*Address)?\]", OWNER_EMAIL),
        (r"\[(?:Your\s*)?Phone(?:\s*Number)?\]", OWNER_PHONE),
        (r"\[(?:Your\s*)?LinkedIn(?:\s*Profile)?(?:\s*URL)?\]", REFERENCE_LINKS.get("LinkedIn", "")),
        (r"\[(?:Your\s*)?Portfolio(?:\s*URL)?\]", REFERENCE_LINKS.get("Portfolio", "")),
        (r"\[(?:Your\s*)?Location\]", OWNER_LOCATION),
        (r"\[(?:Your\s*)?Name\]", OWNER_NAME),
        (r"\[(?:Insert\s*)?Date\]", ""),
    ]
    for pat, rep in replacements:
        text = re.sub(pat, rep, text, flags=re.I)
    text = re.sub(r"\[(?:Your|Insert)\s+[^\]]+\]", "", text, flags=re.I)
    return text.strip()


def _determine_length_instruction(jd: str, role: str) -> str:
    """
    Decide appropriate cover letter length based on seniority and description depth.
    - Junior / simple roles: concise 150-200 words (3 short paragraphs)
    - Mid-senior / complex roles: 250-350 words with specific impact metrics
    """
    senior_markers = ("lead", "senior", "principal", "staff", "architect", "head", "manager")
    role_lower = role.lower()
    is_senior = any(marker in role_lower for marker in senior_markers)
    is_detailed_jd = len(jd) > 2000

    if is_senior or is_detailed_jd:
        return (
            "Write a compelling 250-320 word cover letter. Focus on architectural decisions, "
            "production deployments, quantifiable business impact, and leadership in technical solutions."
        )
    return (
        "Write a punchy, focused 160-220 word cover letter (3 short paragraphs). "
        "Hook the reader immediately, cite 2 specific matching technical achievements, "
        "and express genuine enthusiasm for the company's mission."
    )


def generate_cover_letter(
    role: str,
    company: str,
    jd: str,
    resume_text: str,
    company_ctx: Optional[str] = None,
    recruiter_name: Optional[str] = None,
) -> str:
    """
    Generate a tailored cover letter formatted as clean text for email body.
    Strictly follows facts in the resume — never hallucinates credentials.
    """
    length_rule = _determine_length_instruction(jd, role)

    prompt = (
        f"You are writing a professional, personalized cover letter for {OWNER_NAME}.\n"
        f"Target Role: {role}\n"
        f"Target Company: {company}\n"
        f"Recipient: {recruiter_name or 'Hiring Manager'}\n\n"
        f"GUIDELINES:\n"
        f"1. {length_rule}\n"
        f"0. LANGUAGE: The letter must be written entirely in English.\n"
        f"2. TRUTHFULNESS: Use ONLY facts, tools, metrics, and experiences present in the resume below. "
        f"NEVER invent skills, companies, degrees, dates, or numbers.\n"
        f"3. LOCATION: The applicant is currently based in {OWNER_LOCATION}. "
        f"NEVER claim or imply that the applicant is based in, living in, or a local resident of the company's city or country "
        f"(e.g. NEVER write 'Being based in ...' or 'As a resident of ...'). If location is relevant, emphasize remote collaboration or relocation.\n"
        f"4. FORMAT: Plain text with clean paragraph breaks. No Markdown (no **bold**, no headers). "
        f"Do not include applicant address header or date (this goes directly into an email body).\n"
        f"5. NO PLACEHOLDERS: NEVER use square-bracket placeholders like [Your Email], [Your Phone], [LinkedIn URL], etc.\n"
        f"6. Start with a tailored greeting.\n"
        f"7. End with a professional sign-off by {OWNER_NAME}.\n\n"
        f"APPLICANT RESUME:\n{_clean(resume_text, 4000)}\n\n"
        + (f"COMPANY CONTEXT:\n{company_ctx}\n\n" if company_ctx else "")
        + f"JOB DESCRIPTION:\n{_clean(jd, 3500)}"
    )

    try:
        cover_letter, _ = ask(prompt, task="cover_letter")
        return _clean_cover_letter(cover_letter)
    except Exception as exc:
        # Fallback short template
        greeting = f"Dear {recruiter_name}," if recruiter_name else "Dear Hiring Manager,"
        fallback = (
            f"{greeting}\n\n"
            f"I am writing to express my enthusiasm for the {role} position at {company}. "
            f"With hands-on experience in building scalable data pipelines, generative AI solutions, "
            f"and machine learning systems, I am excited about the opportunity to contribute to your team.\n\n"
            f"My background spans end-to-end data workflows, ETL pipelines, and LLM applications. "
            f"I would welcome the chance to discuss how my technical skills and problem-solving mindset "
            f"can add value to {company}.\n\n"
            f"Thank you for your time and consideration.\n\n"
            f"Sincerely,\n{OWNER_NAME}"
        )
        return _clean_cover_letter(fallback)
