"""
Smart Recruiter Detection & Outreach Drafting Module
Identifies likely HR managers / recruiters from job postings and company research,
then drafts the perfect referral or cold-outreach message. The user connects and
sends the LinkedIn request manually — this module only produces the message options.
"""

import re
from typing import Dict, List, Optional

from ddgs import DDGS

from config.settings import OWNER_NAME, OWNER_EMAIL, REFERENCE_LINKS
from modules.llm_provider import ask, _clean

# LinkedIn URL extractor (handles /in/... profiles)
_LINKEDIN_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(?:in|pub)/[^\s\)\]>,\"']+", re.I)


def find_linkedin_profiles(text: str) -> List[str]:
    """Extract LinkedIn profile URLs found in any source text."""
    return list(dict.fromkeys(_LINKEDIN_RE.findall(text or "")))  # dedupe, keep order


def detect_recruiter(job_text: str, company: str) -> Dict:
    """
    Combine signals to find the best likely recruiter/HR contact:
    - email addresses in the posting
    - recruiter/talent names mentioned
    - LinkedIn profile links in the posting
    - a quick web search for the company's hiring team
    """
    emails = re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", job_text or "")
    linkedin = find_linkedin_profiles(job_text)

    # Names near recruiter keywords (heuristic, low-risk)
    names = []
    for m in re.finditer(r"(?:recruiter|talent|hr|hiring)\s+(?:manager|lead)?\s*[:\-]?\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
                         job_text or ""):
        names.append(m.group(1).strip())

    web_context = ""
    if company:
        try:
            results = DDGS().text(
                f"{company} recruiter OR talent OR 'human resources' hiring",
                max_results=4,
            )
            lines = [f"- {(r.get('title') or '')[:70]}: {(r.get('body') or '')[:200]}" for r in results]
            web_context = "\n".join(lines)
            linkedin = list(dict.fromkeys(linkedin + find_linkedin_profiles(web_context)))
        except Exception:
            pass

    return {
        "company": company,
        "emails": emails,
        "linkedin_profiles": linkedin,
        "candidate_names": names,
        "web_context": web_context,
    }


def draft_outreach(
    role: str,
    company: str,
    recruiter_name: Optional[str] = None,
    context: Optional[str] = None,
    style: str = "cold",
) -> Dict[str, str]:
    """
    Draft the perfect outreach options for the user to copy.
    style: "cold" = no prior contact, "referral" = a mutual connection / referral angle.
    Returns a dict with the options (all plain text for easy copying).
    """
    name = recruiter_name or "your team"
    role_label = role or "the open role"

    if style == "referral":
        context_line = f"Background on the referral: {context}" if context else ""
        prompt = (
            f"Write two options for a SHORT LinkedIn message (each under 300 chars total) from "
            f"{OWNER_NAME} to {name} at {company}. A mutual connection referred us. "
            f"I'm interested in the {role_label}. \n{context_line}\n\n"
            f"Rules: professional and warm, no fluff, no emojis, don't invent facts about my "
            f"background beyond: data engineer, AI engineer, and data analyst experience with "
            f"Python, SQL, ETL pipelines, and LLM/RAG projects. Include my portfolio: "
            f"{REFERENCE_LINKS.get('Portfolio', '')}\n\n"
            f"Return the two options labelled 'Option A' and 'Option B', plain text, no Markdown."
        )
    else:
        prompt = (
            f"Write two options for a SHORT, non-pushy cold LinkedIn connection note to {name} "
            f"at {company}. I'm reaching out about the {role_label}. "
            f"Rules: max 300 characters per note (LinkedIn connection limit is ~300). Be "
            f"genuine and specific, no generic 'I hope this finds you well'. Don't invent "
            f"facts. My background: data/AI engineering with Python, SQL, ETL, and LLM/RAG. "
            f"Option A: interest-based (why I admire the company's work). "
            f"Option B: value-based (a concrete way I can help the team). "
            f"\n\nReturn both labelled 'Option A' and 'Option B', plain text, no Markdown, no emojis."
        )

    try:
        text, _ = ask(prompt, task="light")
        options_text = text.strip()
    except Exception:
        options_text = (
            f"Option A:\nHi {recruiter_name or 'there'}, I'm {OWNER_NAME}, a data/AI engineer. "
            f"I'm very interested in the {role_label} at {company} and would love to connect. "
            f"You can see my work at {REFERENCE_LINKS.get('Portfolio', '')}\n\n"
            f"Option B:\nHi {recruiter_name or 'there'}, I've been following {company}'s work in "
            f"the space and would love to share how my ETL + LLM experience could help the "
            f"{role_label} team."
        )

    return {
        "style": style,
        "options": options_text,
        "linkedin_profiles": find_linkedin_profiles(context or ""),
        "profile_hint": (
            "Find the recruiter on LinkedIn, tap 'Connect', and paste one of the notes above "
            "into the invite message."
        ),
    }


def format_outreach_message(result: Dict) -> str:
    """Render outreach options into a readable Telegram message."""
    style = result.get("style", "cold")
    header = "📇 Referral Outreach" if style == "referral" else "🤝 Cold Outreach Draft"
    lines = [header, ""]
    if result.get("linkedin_profiles"):
        lines.append("LinkedIn found:")
        for prof in result["linkedin_profiles"][:3]:
            lines.append(f"  • {prof}")
        lines.append("")
    lines.append(result.get("options", ""))
    lines.append("")
    lines.append(f"ℹ️ {result.get('profile_hint', '')}")
    return "\n".join(lines)
