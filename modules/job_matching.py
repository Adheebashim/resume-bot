"""
Smart Job Matching Module
Searches for job postings matching the three base resumes and identifies HR/recruiter direct emails.
Provides fit scoring and customizable frequency control.
"""

import re
import time
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional
from ddgs import DDGS

from config.settings import (
    ROLE_LABELS,
    RUNTIME_SETTINGS,
    OWNER_NAME,
)
from modules.llm_provider import ask, parse_json, _clean
from docx import Document
from config.settings import TEMPLATES

# In-memory state to track last search time and seen postings
_job_state = {
    "last_search": 0,
    "seen_titles": set(),
    "seen_companies": set(),
}


def _get_resume_text(role_key: str) -> str:
    """Read text content from a Word template resume."""
    try:
        doc_path = TEMPLATES[role_key]
        if not doc_path.exists():
            return ""
        document = Document(str(doc_path))
        return "\n".join(p.text for p in document.paragraphs if p.text.strip())
    except Exception:
        return ""


def _is_fresh(timestamp_str: str) -> bool:
    """Heuristic check if a job posting is likely fresh (within ~14 days)."""
    try:
        for pattern in [r"(\d+)\s*days?\s*ago", r"(\d+)\s*week[s]?\s*ago",
                        r"just\s*posted", r"new\s*today", r"(\d+)\s*hours?\s*ago"]:
            match = re.search(pattern, timestamp_str, re.I)
            if match:
                if match.group(0).lower() in ("just posted", "new today"):
                    return True
                num = int(match.group(1))
                if "day" in match.group(0).lower():
                    return num <= 14
                if "week" in match.group(0).lower():
                    return num <= 2
                if "hour" in match.group(0).lower():
                    return True
    except (ValueError, IndexError):
        pass
    return False


def _extract_emails(text: str) -> List[str]:
    """Find all email addresses in text."""
    return re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text)


def search_jobs_for_role(role_key: str, max_results: int = 8) -> List[Dict]:
    """
    Search DuckDuckGo for fresh job postings matching a specific role.
    Returns a list of job details with extracted emails.
    """
    results = []
    resume_text = _get_resume_text(role_key)
    if not resume_text:
        return results

    # Build targeted search query from role and top skills
    role_label = ROLE_LABELS.get(role_key, role_key)
    query = f'"{role_label}" job "apply" "email" OR "recruiter" -indeed -glassdoor'

    try:
        ddgs = DDGS()
        raw_results = ddgs.text(query, max_results=max_results)

        for item in raw_results:
            title = (item.get("title") or "").strip()
            snippet = (item.get("body") or "").strip()
            url = (item.get("href") or item.get("url") or "").strip()
            source_site = ""

            # Extract site domain
            if url:
                try:
                    from urllib.parse import urlparse
                    source_site = urlparse(url).hostname or ""
                except Exception:
                    pass

            # Collect all email addresses found in title and snippet
            emails = _extract_emails(f"{title} {snippet}")

            # Filter out common non-apply domains
            email_filter = [e for e in emails if not e.lower().endswith(
                ("example.com", "wixsite.com", "no-reply.com", "noreply.com")
            )]

            # Basic relevance check against resume keywords
            resume_lower = resume_text.lower()
            snippet_lower = (title + " " + snippet).lower()
            keyword_hits = sum(
                1 for word in ("python", "sql", "data", "machine learning",
                               "etl", "pipeline", "llm", "ai", "analytics",
                               "spark", "airflow", "databricks")
                if word in snippet_lower or word in resume_lower
            )

            result = {
                "role_key": role_key,
                "role_label": role_label,
                "title": title[:120],
                "snippet": snippet[:400],
                "url": url,
                "source_site": source_site,
                "emails": email_filter,
                "keyword_hits": keyword_hits,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            if email_filter or keyword_hits >= 3:
                results.append(result)

    except Exception as exc:
        print(f"Job search error for {role_key}: {exc}")

    return results[:max_results]


def score_jobs(jobs: List[Dict], jd_text: str, resume_text: str) -> List[Dict]:
    """
    Use LLM to score each job's fit (0-100) against the base resume.
    """
    if not jobs:
        return []

    # Batch score — keep prompt compact
    job_list = "\n---\n".join(
        f"Job {i+1}: {j['title']} | {j['snippet'][:200]} | Emails: {', '.join(j['emails']) or 'none'}"
        for i, j in enumerate(jobs)
    )

    prompt = (
        f'Score each job below for fit with this resume (0-100 integer). '
        f'Return ONLY JSON: {{"scores": [int, int, ...]}} in same order as listed jobs.\n\n'
        f"RESUME:\n{_clean(resume_text, 2500)}\n\n"
        f"JOBS:\n{job_list}"
    )

    try:
        result = parse_json(ask(prompt, task="light", json_mode=True)[0])
        scores = result.get("scores", [])
        for i, job in enumerate(jobs):
            job["fit_score"] = int(scores[i]) if i < len(scores) else 0
    except Exception:
        for job in jobs:
            job["fit_score"] = 0

    return jobs


def run_job_scan(force: bool = False) -> List[Dict]:
    """
    Full scan: search jobs for all 3 roles, deduplicate, score, and return best matches.
    Uses RUNTIME_SETTINGS["ALERT_INTERVAL_MIN"] to throttle repeated scans unless force=True.
    """
    now = time.time()
    interval_min = RUNTIME_SETTINGS.get("ALERT_INTERVAL_MIN", 15)
    max_results = RUNTIME_SETTINGS.get("ALERT_MAX_RESULTS", 6)

    if not force and (now - _job_state["last_search"] < interval_min * 60):
        return []

    _job_state["last_search"] = now

    all_jobs: List[Dict] = []
    for role_key in TEMPLATES:
        jobs = search_jobs_for_role(role_key, max_results=max_results)
        all_jobs.extend(jobs)

    # Deduplicate by title (case-insensitive)
    unique_jobs: List[Dict] = []
    seen = set()
    for job in all_jobs:
        title_key = job["title"].lower()[:80]
        if title_key not in seen:
            seen.add(title_key)
            unique_jobs.append(job)

    # Sort by fit relevance (emails present first, then keyword hits)
    unique_jobs.sort(key=lambda x: (len(x["emails"]), x["keyword_hits"]), reverse=True)
    unique_jobs = unique_jobs[:max_results]

    # Score top results
    if unique_jobs:
        # Score against each role's resume
        best_role = unique_jobs[0]["role_key"]
        resume_text = _get_resume_text(best_role)
        score_jobs(unique_jobs, "", resume_text)

    return unique_jobs


def format_job_alerts(jobs: List[Dict]) -> str:
    """Format job alert results into a readable Telegram message."""
    if not jobs:
        return "No new matching job alerts found. I'll keep checking automatically."

    interval_min = RUNTIME_SETTINGS.get("ALERT_INTERVAL_MIN", 15)
    lines = ["🎯 Smart Job Alerts — Fresh Matches Found", ""]
    for i, job in enumerate(jobs, 1):
        role_tag = f"[{job.get('role_label', 'Match')}]"
        fit = job.get("fit_score", 0)
        fit_str = f" · Fit {fit}/100" if fit else ""
        lines.append(f"{i}. {job['title']}{fit_str}")
        if job.get("emails"):
            lines.append(f"   📧 HR Email: {', '.join(job['emails'][:2])}")
        if job.get("url"):
            lines.append(f"   🔗 {job['url'][:120]}")
        lines.append("")

    lines.append(f"Scan frequency: every {interval_min} min · Top {len(jobs)} shown")
    return "\n".join(lines)
