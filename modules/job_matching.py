"""
Smart Job Matching Module
Searches for job postings matching the three base resumes and identifies HR/recruiter direct emails.
Provides fit scoring and customizable frequency control.
"""

import re
import time
import json
from concurrent.futures import ThreadPoolExecutor
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


_SKILL_KEYWORDS = (
    "python", "sql", "data", "machine learning", "etl", "pipeline", "llm",
    "ai", "analytics", "spark", "airflow", "databricks",
)


# ddgs queries several search engines. A single slow/failing engine (e.g. mojeek timing out)
# used to abort the whole search, so we try engines one at a time and skip the bad ones.
_SEARCH_BACKENDS = ("yahoo", "bing", "duckduckgo", "brave")
_SEARCH_TIMEOUT = 8  # seconds per engine


def _web_search(queries: List[str], max_results: int) -> List[Dict]:
    """Return results for the first query/engine combination that works."""
    last_exc = None
    for query in queries:
        failures = 0
        for backend in _SEARCH_BACKENDS:
            try:
                try:
                    client = DDGS(timeout=_SEARCH_TIMEOUT)
                except TypeError:  # older ddgs versions without a timeout argument
                    client = DDGS()
                results = list(client.text(query, max_results=max_results, backend=backend))
                if results:
                    return results
            except Exception as exc:
                failures += 1
                last_exc = exc
                print(f"Search engine '{backend}' failed: {str(exc)[:160]}")
        if failures == len(_SEARCH_BACKENDS):
            break  # every engine errored (network problem) - a second query would only wait again
    if last_exc:
        print(f"Search failed for this role: {str(last_exc)[:160]}")
    return []


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
    strict_query = f'"{role_label}" job "apply" "email" OR "recruiter" -indeed -glassdoor'
    loose_query = f"{role_label} job vacancy apply email recruiter"

    try:
        raw_results = _web_search([strict_query, loose_query], max_results)

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

            # Relevance check: count skill keywords found in the POSTING itself
            # (earlier code also counted words from the resume, so everything passed).
            posting_text = (title + " " + snippet).lower()
            keyword_hits = sum(
                1 for word in _SKILL_KEYWORDS
                if re.search(rf"\b{re.escape(word)}\b", posting_text)
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

            if email_filter or keyword_hits >= 2:
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

    # Search the roles in parallel so one slow search engine can't stall the whole scan.
    role_keys = list(TEMPLATES)
    all_jobs: List[Dict] = []
    with ThreadPoolExecutor(max_workers=max(1, len(role_keys))) as pool:
        for jobs in pool.map(lambda rk: search_jobs_for_role(rk, max_results=max_results), role_keys):
            all_jobs.extend(jobs)

    # Deduplicate by title (case-insensitive)
    unique_jobs: List[Dict] = []
    seen = set()
    for job in all_jobs:
        title_key = job["title"].lower()[:80]
        if title_key not in seen:
            seen.add(title_key)
            unique_jobs.append(job)

    # Filter out already seen jobs across scans (unless force=True)
    fresh_jobs: List[Dict] = []
    for job in unique_jobs:
        dedupe_key = (job.get("url") or job["title"]).lower()[:100]
        if force or dedupe_key not in _job_state["seen_titles"]:
            fresh_jobs.append(job)
            if not force:
                _job_state["seen_titles"].add(dedupe_key)

    if len(_job_state["seen_titles"]) > 500:
        _job_state["seen_titles"] = set(list(_job_state["seen_titles"])[-250:])

    # Sort by fit relevance (emails present first, then keyword hits)
    fresh_jobs.sort(key=lambda x: (len(x["emails"]), x["keyword_hits"]), reverse=True)
    fresh_jobs = fresh_jobs[:max_results]

    # Score top results: each job against its own role's resume
    if fresh_jobs:
        by_role = {}
        for job in fresh_jobs:
            by_role.setdefault(job["role_key"], []).append(job)
        for role_key, jobs in by_role.items():
            resume_text = _get_resume_text(role_key)
            score_jobs(jobs, "", resume_text)

    return fresh_jobs


def format_job_alerts(jobs: List[Dict]) -> str:
    """Format job alert results into a readable Telegram message."""
    if not jobs:
        return "No new matching job alerts found. I'll keep checking automatically."

    interval_min = RUNTIME_SETTINGS.get("ALERT_INTERVAL_MIN", 15)
    with_email = sum(1 for j in jobs if j.get("emails"))
    lines = [f"🎯 Smart Job Alerts — {len(jobs)} fresh match(es)", ""]
    for i, job in enumerate(jobs, 1):
        role_tag = job.get("role_label") or "Match"
        fit = job.get("fit_score", 0)
        fit_str = f" · Fit {fit}/100" if fit else ""
        lines.append(f"{i}. {job['title']}{fit_str}")
        lines.append(f"   🏷 {role_tag}")
        if job.get("emails"):
            lines.append(f"   📧 HR Email: {', '.join(job['emails'][:2])} → draft coming below")
        else:
            lines.append("   📭 No HR email in the listing — open the link, or send me the post + recruiter email")
        if job.get("url"):
            lines.append(f"   🔗 {job['url'][:120]}")
        lines.append("")

    lines.append(f"{with_email} of {len(jobs)} have an HR email and are being drafted for your approval.")
    lines.append(f"Scan frequency: every {interval_min} min")
    return "\n".join(lines)
