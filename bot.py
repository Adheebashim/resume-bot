import os
import re
import json
import time
import hashlib
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
import threading
import io
import base64
import requests
from html.parser import HTMLParser
from urllib.parse import urlparse
import ipaddress
import socket
import shutil
import subprocess
import tempfile
from pathlib import Path
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from xml.sax.saxutils import escape
from ddgs import DDGS

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
from pypdf import PdfReader
from docx import Document
import gspread

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

GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID")
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET")
GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN")
GMAIL_SENDER = os.environ.get("GMAIL_SENDER")

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SHEET_TAB = os.environ.get("GOOGLE_SHEET_TAB", "Applications")

TEMPLATE_DIR = Path(os.environ.get("RESUME_TEMPLATE_DIR", "templates"))
TEMPLATES = {
    "data_engineer": TEMPLATE_DIR / "Adheeb_DataEngineer.docx",
    "ai_engineer": TEMPLATE_DIR / "Adheeb_AIEngineer.docx",
    "data_analyst": TEMPLATE_DIR / "Adheeb_DataAnalyst.docx",
}
ROLE_LABELS = {
    "data_engineer": "Data Engineer",
    "ai_engineer": "AI Engineer",
    "data_analyst": "Data Analyst",
}
def read_docx_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Resume template not found: {path}. Add the three Word files to the templates/ folder.")
    document = Document(str(path))
    return "\n".join(p.text for p in document.paragraphs if p.text.strip())

def get_default_resume_text() -> str:
    # Compatibility fallback for old commands; Data Analyst is the neutral default.
    try:
        return read_docx_text(TEMPLATES["data_analyst"])
    except Exception:
        legacy = Path("resume.txt")
        return legacy.read_text(encoding="utf-8") if legacy.exists() else ""

RESUME = get_default_resume_text()


# ---------------------------------------------------------------------
# LLM providers with failover
# ---------------------------------------------------------------------
# Task-tiered model routing, tuned for free tiers.
#   light  = classification / fit score / chat   -> small, fast, high-quota models
#   heavy  = resume tailoring                    -> best quality model first
#   vision = screenshot OCR                      -> Gemini only (Groq has no vision here)
# Override any chain in Render with LLM_LIGHT / LLM_HEAVY / LLM_VISION, formatted as
#   provider:model,provider:model   e.g.  gemini:gemini-3.6-flash,groq:openai/gpt-oss-120b
def _parse_chain(env_name: str, default: str) -> list:
    chain = []
    for item in (os.environ.get(env_name) or default).split(","):
        if ":" in item:
            provider, model = item.strip().split(":", 1)
            chain.append((provider.strip().lower(), model.strip()))
    return chain


CHAINS = {
    "light": _parse_chain(
        "LLM_LIGHT",
        "gemini:gemini-3.5-flash-lite,groq:openai/gpt-oss-20b,"
        "gemini:gemini-2.5-flash-lite,groq:openai/gpt-oss-120b",
    ),
    "heavy": _parse_chain(
        "LLM_HEAVY",
        "gemini:gemini-3.6-flash,groq:openai/gpt-oss-120b,"
        "gemini:gemini-3.5-flash-lite,groq:qwen/qwen3.6-27b",
    ),
    "vision": _parse_chain(
        "LLM_VISION",
        "gemini:gemini-3.5-flash-lite,gemini:gemini-3.6-flash,gemini:gemini-2.5-flash-lite",
    ),
}
CHAINS["chat"] = CHAINS["light"]

# Output-token ceilings per task (for Gemini this also covers thinking tokens).
MAX_OUT = {"light": 600, "chat": 400, "heavy": 3000, "vision": 1800}


class LLMError(Exception):
    def __init__(self, status: int, message: str, response=None):
        super().__init__(message)
        self.status = status
        self.response = response


_cooldown: dict = {}          # (provider, model) -> unix time it may be used again
_cache: "OrderedDict" = OrderedDict()   # successful results, avoids paying twice on retries
_CACHE_MAX = 64


def _seconds_from_text(text: str):
    """Parse '46s', '7.2s', '1h2m3s' style retry hints; None if absent."""
    m = re.search(r'(?:retrydelay"?\s*[:=]\s*"?|try again in\s*)((?:\d+h)?(?:\d+m)?(?:[\d.]+s)?)', text, re.I)
    if not m or not m.group(1):
        return None
    h = re.search(r"(\d+)h", m.group(1))
    mi = re.search(r"(\d+)m", m.group(1))
    sec = re.search(r"([\d.]+)s", m.group(1))
    total = (int(h.group(1)) * 3600 if h else 0) + (int(mi.group(1)) * 60 if mi else 0) \
        + (float(sec.group(1)) if sec else 0)
    return int(total) or None


def _cooldown_for(err: LLMError) -> int:
    """How long to skip a model after a failure, so we don't burn calls on a dead/limited one."""
    body = str(err).lower()
    if err.status in (403, 404):
        return 6 * 3600                      # retired / not available on this plan
    if err.status == 429:
        if "limit: 0" in body:
            return 12 * 3600                 # model has zero free quota
        header = err.response.headers.get("retry-after") if err.response is not None else None
        hint = int(float(header)) if header and header.replace(".", "", 1).isdigit() \
            else _seconds_from_text(str(err))
        if "per day" in body or "perday" in body or "daily" in body or (hint and hint > 900):
            return min(hint or 3 * 3600, 6 * 3600)
        return max(20, min(hint or 60, 900))
    if err.status >= 500:
        return 30
    return 0


_usage: dict = {}   # (provider, model) -> {"day", "calls", "tokens", "hdr"}


def _day_key(provider: str) -> str:
    """Gemini's daily quota resets at midnight Pacific; Groq's at 00:00 UTC."""
    tz = timezone.utc
    if provider == "gemini":
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("America/Los_Angeles")
        except Exception:
            tz = timezone(timedelta(hours=-8))
    return datetime.now(tz).strftime("%Y-%m-%d")


def _record(provider: str, model: str, tokens, headers=None) -> None:
    day = _day_key(provider)
    u = _usage.get((provider, model))
    if not u or u["day"] != day:
        u = {"day": day, "calls": 0, "tokens": 0, "hdr": {}}
        _usage[(provider, model)] = u
    u["calls"] += 1
    u["tokens"] += int(tokens or 0)
    if headers:
        for k in ("x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens",
                  "x-ratelimit-limit-requests", "x-ratelimit-limit-tokens"):
            if headers.get(k):
                u["hdr"][k] = headers.get(k)


def _fmt_tokens(n: int) -> str:
    return f"{n/1000:.1f}k" if n >= 1000 else str(n)


def usage_report() -> str:
    """Human-readable LLM usage for /quota. Counts reset when the bot restarts."""
    now = time.time()
    lines = ["📊 LLM usage today (counted since the bot last started)"]
    any_rows = False
    for provider, title in (("gemini", "Gemini · day resets midnight Pacific"),
                            ("groq", "Groq · day resets 00:00 UTC")):
        models = []
        for chain in CHAINS.values():
            for p, m in chain:
                if p == provider and m not in models:
                    models.append(m)
        rows = []
        for m in models:
            u = _usage.get((provider, m))
            if u and u["day"] != _day_key(provider):
                u = None
            paused = _cooldown.get((provider, m), 0) - now
            if not u and paused <= 0:
                continue
            text = f"• {m}: " + (f"{u['calls']} call{'s' if u['calls'] != 1 else ''}, {_fmt_tokens(u['tokens'])} tokens" if u else "no calls")
            if u and u["hdr"].get("x-ratelimit-remaining-requests"):
                text += f" · {u['hdr']['x-ratelimit-remaining-requests']} requests left today"
            if u and u["hdr"].get("x-ratelimit-remaining-tokens"):
                text += f" · {u['hdr']['x-ratelimit-remaining-tokens']} tokens left this minute"
            if paused > 0:
                text += f" · ⏸ paused ~{max(1, int(paused // 60))} min (limit hit)"
            rows.append(text)
        if rows:
            any_rows = True
            lines += ["", title] + rows
    if not any_rows:
        lines += ["", "No AI calls yet today."]
    lines += ["", "Gemini doesn't report remaining quota; see the usage page in Google AI Studio for exact limits."]
    return "\n".join(lines)


def _clean(text: str, limit: int) -> str:
    """Collapse whitespace/blank lines and cap length: fewer input tokens, same meaning."""
    text = re.sub(r"[ \t\u00a0]+", " ", text or "")
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    return text[:limit]


def _err(provider: str, model: str, response) -> LLMError:
    body = (response.text or "").strip().replace("\n", " ")[:220]
    return LLMError(response.status_code, f"{provider}/{model} HTTP {response.status_code}: {body}", response)


def _gemini_once(model: str, task: str, prompt: str, json_mode: bool, image, optional: bool) -> str:
    gen = {"temperature": 0.3, "maxOutputTokens": MAX_OUT[task]}
    if optional:
        if json_mode:
            gen["responseMimeType"] = "application/json"
        heavy = task == "heavy"
        if model.startswith("gemini-2.5"):
            gen["thinkingConfig"] = {"thinkingBudget": 1024 if heavy else 0}
        else:
            gen["thinkingConfig"] = {"thinkingLevel": "low" if heavy else "minimal"}
    parts = [{"text": prompt}]
    if image:
        parts.append({"inline_data": {"mime_type": image[0], "data": image[1]}})
    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": GEMINI_KEY},
            json={"contents": [{"parts": parts}], "generationConfig": gen},
            timeout=90,
        )
    except requests.RequestException as exc:
        raise LLMError(0, f"gemini/{model}: {exc}")
    if not r.ok:
        raise _err("gemini", model, r)
    data = r.json()
    _record("gemini", model, (data.get("usageMetadata") or {}).get("totalTokenCount"))
    cand = (data.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if not p.get("thought"))
    if cand.get("finishReason") == "MAX_TOKENS" and json_mode:
        raise LLMError(0, f"gemini/{model}: output truncated")
    return text


def _groq_once(model: str, task: str, prompt: str, json_mode: bool, optional: bool) -> str:
    body = {
        "model": model,
        "temperature": 0.3,
        "max_completion_tokens": MAX_OUT[task],
        "messages": [{"role": "user", "content": prompt}],
    }
    if optional:
        if model.startswith("openai/gpt-oss"):
            body["reasoning_effort"] = "low"        # fewer hidden reasoning tokens
        if json_mode:
            body["response_format"] = {"type": "json_object"}
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            json=body,
            timeout=90,
        )
    except requests.RequestException as exc:
        raise LLMError(0, f"groq/{model}: {exc}")
    if not r.ok:
        raise _err("groq", model, r)
    data = r.json()
    _record("groq", model, (data.get("usage") or {}).get("total_tokens"), r.headers)
    choice = data["choices"][0]
    text = choice["message"].get("content") or ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    if choice.get("finish_reason") == "length" and json_mode:
        raise LLMError(0, f"groq/{model}: output truncated")
    return text


def ask(prompt: str, task: str = "light", json_mode: bool = False, image=None, use_cache: bool = True):
    """Run a prompt through the task's model chain. Returns (text, 'provider/model')."""
    chain = [
        (p, m) for p, m in CHAINS[task]
        if (p == "gemini" and GEMINI_KEY) or (p == "groq" and GROQ_KEY and not image)
    ]
    if not chain:
        raise RuntimeError("No LLM API key configured. Add GEMINI_API_KEY and/or GROQ_API_KEY.")

    cache_key = hashlib.sha256(
        f"{task}|{json_mode}|{prompt}|{image[1][:2000] if image else ''}".encode()
    ).hexdigest()
    if use_cache and cache_key in _cache:
        return _cache[cache_key]

    now = time.time()
    candidates = [c for c in chain if _cooldown.get(c, 0) <= now] or chain
    errors = []
    for provider, model in candidates:
        optional = True
        for attempt in range(3):
            try:
                if provider == "gemini":
                    text = _gemini_once(model, task, prompt, json_mode, image, optional)
                else:
                    text = _groq_once(model, task, prompt, json_mode, optional)
                if not text.strip():
                    errors.append(f"{provider}/{model}: empty response")
                    break
                result = (text, f"{provider}/{model}")
                if use_cache:
                    _cache[cache_key] = result
                    while len(_cache) > _CACHE_MAX:
                        _cache.popitem(last=False)
                return result
            except LLMError as exc:
                if exc.status == 400 and optional:
                    optional = False          # retry once without thinking/json extras
                    continue
                wait = _cooldown_for(exc)
                if wait:
                    _cooldown[(provider, model)] = time.time() + wait
                if exc.status >= 500 and attempt < 2:
                    time.sleep(2)
                    continue
                errors.append(str(exc)[:200])
                break
    raise RuntimeError("All LLM providers failed: " + " | ".join(errors))


def _shrink_image(image_bytes: bytes, max_side: int = 1600) -> bytes:
    """Downscale big phone screenshots: image tokens scale with size, text stays readable."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes))
        if max(img.size) <= max_side:
            return image_bytes
        img = img.convert("RGB")
        img.thumbnail((max_side, max_side))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85)
        return out.getvalue()
    except Exception:
        return image_bytes


def extract_text_from_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """Use Gemini vision to read text/data from a Telegram screenshot."""
    if not GEMINI_KEY:
        raise RuntimeError("Screenshot reading requires GEMINI_API_KEY. Add it in Render Environment.")
    image_bytes = _shrink_image(image_bytes)
    encoded = base64.b64encode(image_bytes).decode("ascii")
    text, _ = ask(
        "Extract the job post text from this screenshot verbatim: job title, company, "
        "responsibilities, skills, recruiter name, any email address. Keep line breaks. "
        "Don't guess unreadable text. Return only the text.",
        task="vision",
        image=("image/jpeg", encoded),
    )
    return text.strip()


class PublicPageParser(HTMLParser):
    """Extract visible text and useful metadata from an ordinary public HTML page."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.metadata = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style", "noscript", "svg", "nav", "footer", "header"):
            self._skip_depth += 1
        if tag == "meta":
            name = (attrs.get("name") or attrs.get("property") or "").lower()
            content = (attrs.get("content") or "").strip()
            if content and name in {
                "description", "og:title", "og:description", "twitter:title",
                "twitter:description"
            }:
                self.metadata.append(content)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg", "nav", "footer", "header") and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        cleaned = " ".join(data.split())
        if cleaned and not self._skip_depth:
            self.parts.append(cleaned)


def _validate_public_url(url: str) -> str:
    """Accept only public HTTP(S) URLs; do not fetch localhost/private addresses."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Please send a complete public http:// or https:// link.")
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("That link is not a public website.")
    try:
        addresses = socket.getaddrinfo(host, None)
        for entry in addresses:
            ip = ipaddress.ip_address(entry[4][0])
            if not ip.is_global:
                raise ValueError("That link does not point to a public website.")
    except socket.gaierror as exc:
        raise ValueError("I couldn't resolve that website address.") from exc
    return url


def extract_public_page(url: str) -> str:
    """Read public page text only; never bypass login, anti-bot checks, or access controls."""
    url = _validate_public_url(url)
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; ResumeHelper/1.0)"},
        timeout=20,
        allow_redirects=True,
    )
    response.raise_for_status()
    if response.url != url:
        _validate_public_url(response.url)
    content_type = response.headers.get("content-type", "").lower()
    if "text/html" not in content_type:
        raise ValueError("This link doesn't point to a readable HTML page.")
    parser = PublicPageParser()
    parser.feed(response.text[:2_000_000])
    chunks = parser.metadata + parser.parts
    # De-duplicate repeated metadata while preserving page order.
    seen = set()
    cleaned = []
    for chunk in chunks:
        if chunk not in seen:
            seen.add(chunk)
            cleaned.append(chunk)
    result = "\n".join(cleaned)
    if len(result.strip()) < 180:
        raise ValueError(
            "The page doesn't expose enough public text. LinkedIn may require sign-in "
            "or restrict automated access."
        )
    return result[:16000]


def find_url(text: str):
    match = re.search(r"https?://[^\s<>]+", text.strip(), flags=re.I)
    if not match:
        return None
    return match.group(0).rstrip(".,!?)]}")

def is_likely_job_posting(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "job description", "responsibilities", "requirements", "qualifications",
        "we are hiring", "apply now", "job title", "role:", "position:",
        "experience required", "skills required", "about the job",
    )
    return len(text) > 250 or sum(marker in lowered for marker in markers) >= 2


async def interactive_reply(update: Update, text: str) -> None:
    """Reply naturally to greetings and general chat, using an LLM when available."""
    message = update.effective_message
    if not message:
        return

    normalized = text.strip().lower().strip("!.,? ")
    simple_replies = {
        "hi": "Hi! 👋 How can I help you today?",
        "hello": "Hello! 👋 Send me a job description or screenshot whenever you're ready.",
        "hey": "Hey! 😊 What would you like to do?",
        "good morning": "Good morning! ☀️ How can I help?",
        "good afternoon": "Good afternoon! How can I help?",
        "good evening": "Good evening! How can I help?",
        "thanks": "You're welcome! 😊",
        "thank you": "You're welcome! Let me know if you need anything else.",
        "help": (
            "Here's what I can do:\n"
            "• Chat normally — say hi and I'll reply.\n"
            "• Send a job description as text, a public job-post link, or a screenshot.\n"
            "• I'll automatically select the best of your three Word templates: Data Engineer, AI Engineer, or Data Analyst.\n"
            "• You can upload a PDF if you want to use a custom resume for one application.\n"
            "• Send a public job-post link or upload a screenshot.\n"
            "• Include a recruiter email, or send it when I ask.\n"
            "• Review the fit, tailor your resume, and show you a PDF to approve.\n"
            "• Email the resume only after you press Send."
        ),
    }
    if normalized in simple_replies:
        await message.reply_text(simple_replies[normalized])
        return

    if GROQ_KEY or GEMINI_KEY:
        try:
            answer, _provider = ask(
                "You are a concise, friendly Telegram assistant for a resume-tailoring bot. "
                "Reply briefly. Don't claim actions you haven't performed. If the user seems "
                "to share a job post, ask for the recruiter's email.\n\n"
                f"User: {_clean(text, 1200)}",
                task="chat",
                use_cache=False,
            )
            await message.reply_text(answer[:4000])
            return
        except Exception:
            pass

    await message.reply_text(
        "I can help with job descriptions, screenshots, resume tailoring, and "
        "emailing after your approval. Send /help to see the options."
    )


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
# Google Sheets application tracker
# ---------------------------------------------------------------------
SHEET_HEADERS = [
    "Applied/Created At", "Company", "Job Role", "Resume Used",
    "Recruiter Email", "Fit Score (/10)", "Application Status", "Source",
    "Resume Filename",
]


def get_tracking_worksheet():
    """Return the configured Google Sheet tab, or None if tracking is not configured."""
    if not GOOGLE_SHEET_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return None
    credentials_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    client = gspread.service_account_from_dict(credentials_info)
    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
    try:
        worksheet = spreadsheet.worksheet(GOOGLE_SHEET_TAB)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=GOOGLE_SHEET_TAB, rows=1000, cols=len(SHEET_HEADERS))
    current = worksheet.row_values(1)
    if current[:len(SHEET_HEADERS)] != SHEET_HEADERS:
        if not current:
            worksheet.append_row(SHEET_HEADERS, value_input_option="USER_ENTERED")
        elif current[0] != SHEET_HEADERS[0]:
            worksheet.insert_row(SHEET_HEADERS, 1, value_input_option="USER_ENTERED")
    return worksheet


def log_application(company: str, role: str, resume_used: str, recipient: str,
                    fit_score: str, status: str, source: str, filename: str):
    """Append one application-preparation row; return its worksheet row number."""
    worksheet = get_tracking_worksheet()
    if worksheet is None:
        return None
    row = [
        datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        company or "Unknown",
        role or "Unknown",
        resume_used or "Unknown",
        recipient or "",
        fit_score,
        status,
        source or "Job description",
        filename or "",
    ]
    worksheet.append_row(row, value_input_option="USER_ENTERED")
    return len(worksheet.get_all_values())


def update_application_status(row_number, status: str):
    if not row_number:
        return
    worksheet = get_tracking_worksheet()
    if worksheet is not None:
        # Application Status is column G.
        worksheet.update_cell(int(row_number), 7, status)


def update_application_row(row_number, role=None, resume_used=None, fit_score=None, filename=None):
    """Update changed cells after a template switch (C=role, D=resume, F=fit, I=filename)."""
    if not row_number:
        return
    worksheet = get_tracking_worksheet()
    if worksheet is None:
        return
    row = int(row_number)
    updates = []
    for col, value in (("C", role), ("D", resume_used), ("F", fit_score), ("I", filename)):
        if value is not None:
            updates.append({"range": f"{col}{row}", "values": [[value]]})
    if updates:
        worksheet.batch_update(updates, value_input_option="USER_ENTERED")


# ---------------------------------------------------------------------
# Job parsing, company research, and fit decision
# ---------------------------------------------------------------------
def extract(jd: str) -> dict:
    prompt = (
        'Return ONLY JSON: {"company":"","role":"","template_role":"data_engineer|ai_engineer|data_analyst"}\n'
        "Pick template_role by actual duties, not title: data_engineer = ETL/pipelines/warehouses/"
        "orchestration; ai_engineer = LLMs/RAG/agents/ML applications; data_analyst = SQL/BI/"
        "reporting/insights.\n\nJOB:\n" + _clean(jd, 2500)
    )
    try:
        result = parse_json(ask(prompt, task="light", json_mode=True)[0])
    except Exception:
        result = {}
    result.setdefault("company", "")
    result.setdefault("role", "")
    selected = str(result.get("template_role", "")).strip().lower()
    if selected not in TEMPLATES:
        text = (str(result.get("role", "")) + "\n" + jd).lower()
        if any(k in text for k in ("generative ai", "agentic ai", "llm", "rag", "machine learning engineer", "ai engineer", "artificial intelligence")):
            selected = "ai_engineer"
        elif any(k in text for k in ("data engineer", "etl", "data pipeline", "data warehouse", "dbt", "airflow", "spark", "databricks")):
            selected = "data_engineer"
        else:
            selected = "data_analyst"
    result["template_role"] = selected
    return result


def research(company: str) -> str:
    """Tiny company snapshot (3 short results) - enough for emphasis, cheap in tokens."""
    if not company:
        return ""
    try:
        results = DDGS().text(f"{company} company about products", max_results=3)
        return "\n".join(
            f"- {(item.get('title') or '')[:60]}: {(item.get('body') or '')[:140]}"
            for item in results
        )
    except Exception:
        return ""


def decide(jd: str, company_ctx: str, resume_text: str) -> dict:
    """Stand-alone fit score (only used for uploaded-PDF resumes; templates score inside tailor_docx)."""
    prompt = (
        'Return ONLY JSON {"fit_score":0}: realistic integer 0-100 fit of this resume to the job '
        "(skills, experience, projects, qualifications). Do not inflate.\n\n"
        f"RESUME:\n{_clean(resume_text, 3500)}\n\nJOB:\n{_clean(jd, 3500)}"
    )
    try:
        result = parse_json(ask(prompt, task="light", json_mode=True)[0])
        result["fit_score"] = max(0, min(100, int(float(result.get("fit_score", 0)))))
        return result
    except Exception:
        return {"fit_score": 0}


def tailor(jd: str, company_ctx: str, decision: dict, resume_text: str):
    prompt = (
        "Tailor this resume to the job. Use ONLY facts in the resume; never invent skills, "
        "employers, dates, credentials or numbers. You may reorder, rephrase and emphasise; "
        "mirror job keywords only when truthful. Plain text, no Markdown, about one page.\n\n"
        f"RESUME:\n{_clean(resume_text, 6000)}\n\n"
        + (f"COMPANY:\n{company_ctx}\n\n" if company_ctx else "")
        + f"JOB:\n{_clean(jd, 5000)}"
    )
    return ask(prompt, task="heavy")


# ---------------------------------------------------------------------
# Word template tailoring and PDF conversion
# ---------------------------------------------------------------------
def _set_paragraph_text(paragraph, value: str) -> None:
    """Replace text while retaining the paragraph's existing style and layout."""
    runs = paragraph.runs
    if runs:
        runs[0].text = value
        for run in runs[1:]:
            run.text = ""
    else:
        paragraph.add_run(value)

def _template_sections(document):
    paragraphs = document.paragraphs
    texts = [p.text.strip() for p in paragraphs]
    summary_idx = next((i for i, t in enumerate(texts) if t.upper() == "SUMMARY"), None)
    experience_idx = next((i for i, t in enumerate(texts) if t.upper() == "EXPERIENCE"), None)
    project_idx = next((i for i, t in enumerate(texts) if t.upper() in {"PROJECTS", "AI PROJECTS"}), None)
    skills_idx = next((i for i, t in enumerate(texts) if t.upper() == "SKILLS"), None)
    if None in (summary_idx, experience_idx, project_idx, skills_idx):
        raise ValueError("The selected Word template is missing a required section heading.")
    summary = summary_idx + 1
    nielsen_idx = next((i for i, t in enumerate(texts) if i > experience_idx and "NielsenIQ" in t), None)
    vyza_idx = next((i for i, t in enumerate(texts) if i > experience_idx and "Vyza Solutions" in t), None)
    if nielsen_idx is None or vyza_idx is None or not (nielsen_idx < vyza_idx < project_idx):
        raise ValueError("Could not identify the experience sections in the selected Word template.")
    nielsen_bullets = [i for i in range(nielsen_idx + 1, vyza_idx) if texts[i]]
    intern_bullets = [i for i in range(vyza_idx + 1, project_idx) if texts[i]]
    project_bullets = [i for i in range(project_idx + 1, skills_idx) if texts[i] and " | " not in texts[i] and "  |  " not in texts[i]]
    education_idx = next((i for i, t in enumerate(texts) if i > skills_idx and t.upper() == "EDUCATION"), len(texts))
    skills_lines = [i for i in range(skills_idx + 1, education_idx) if texts[i]]
    return {"summary": summary, "nielsen": nielsen_bullets, "intern": intern_bullets, "projects": project_bullets, "skills": skills_lines}

def tailor_docx(template_role: str, jd: str, company_ctx: str, decision: dict):
    """One LLM call: tailors the template AND scores fit. Returns (docx_bytes, fit_score_0_100)."""
    document = Document(str(TEMPLATES[template_role]))
    sections = _template_sections(document)
    paragraphs = document.paragraphs
    source_summary = paragraphs[sections["summary"]].text
    source_nielsen = [paragraphs[i].text for i in sections["nielsen"]]
    source_intern = [paragraphs[i].text for i in sections["intern"]]
    source_projects = [paragraphs[i].text for i in sections["projects"]]
    skills_text = "\n".join(paragraphs[i].text.replace("\t", ": ") for i in sections["skills"])
    dumps = lambda v: json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    prompt = (
        'Tailor this resume content to the job. Return ONLY JSON: {"fit_score":0,"summary":"",'
        f'"nielsen_bullets":[{len(source_nielsen)} strings],"intern_bullets":[{len(source_intern)} strings],'
        f'"project_bullets":[{len(source_projects)} strings]}}\n'
        "fit_score = realistic integer 0-100 fit of the ORIGINAL resume (incl. skills) to the job; don't inflate.\n"
        "Rules: use only facts in the source; never invent skills, tools, employers, dates, credentials or "
        "metrics. Rephrase/reorder for relevance, mirror job keywords only when truthful. Each bullet no "
        "longer than its original. Plain text, no bullet symbols, same bullet counts.\n"
        f"Target role: {ROLE_LABELS[template_role]}\n"
        f"SUMMARY: {source_summary}\n"
        f"NIELSENIQ: {dumps(source_nielsen)}\n"
        f"INTERNSHIP: {dumps(source_intern)}\n"
        f"PROJECTS: {dumps(source_projects)}\n"
        f"SKILLS:\n{skills_text}\n"
        + (f"COMPANY:\n{company_ctx}\n" if company_ctx else "")
        + f"JOB:\n{_clean(jd, 5000)}"
    )
    output, _model = ask(prompt, task="heavy", json_mode=True)
    data = parse_json(output)
    replacements = [(sections["summary"], data.get("summary"))]
    for key, indices in (("nielsen_bullets", sections["nielsen"]), ("intern_bullets", sections["intern"]), ("project_bullets", sections["projects"])):
        values = data.get(key)
        if not isinstance(values, list) or len(values) != len(indices) or not all(isinstance(v, str) and v.strip() for v in values):
            raise ValueError(f"The AI returned an invalid {key} structure. Please retry.")
        replacements.extend(zip(indices, values))
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("The AI did not return a valid resume summary. Please retry.")
    for index, value in replacements:
        _set_paragraph_text(paragraphs[index], value.strip())
    try:
        fit = max(0, min(100, int(float(data.get("fit_score")))))
    except (TypeError, ValueError):
        fit = None
    document.core_properties.title = f"Adheeb Ashim - {ROLE_LABELS[template_role]} Resume"
    document.core_properties.author = "Adheeb Ashim"
    document.core_properties.subject = f"Tailored application resume for {ROLE_LABELS[template_role]}"
    output_buffer = io.BytesIO()
    document.save(output_buffer)
    return output_buffer.getvalue(), fit

def docx_to_pdf(docx_bytes: bytes) -> bytes:
    """Convert a tailored Word file using LibreOffice, installed in the Render build."""
    executable = (
        shutil.which("libreoffice") or shutil.which("soffice")
        or next((p for p in ("/usr/bin/soffice", "/usr/bin/libreoffice",
                             "/usr/lib/libreoffice/program/soffice",
                             "/opt/libreoffice/program/soffice") if os.path.exists(p)), None)
    )
    if not executable:
        raise RuntimeError(
            "LibreOffice is not installed in this deployment. This service must be deployed "
            "from the Dockerfile (Render > Settings > Language = Docker); the Python runtime "
            "cannot install it."
        )
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        docx_path = temp / "Adheeb_Tailored_Resume.docx"
        docx_path.write_bytes(docx_bytes)
        result = subprocess.run(
            [executable, "-env:UserInstallation=file://" + str(temp / "lo-profile"), "--headless", "--convert-to", "pdf", "--outdir", str(temp), str(docx_path)],
            capture_output=True, text=True, timeout=90, check=False,
        )
        pdf_path = temp / "Adheeb_Tailored_Resume.pdf"
        if result.returncode != 0 or not pdf_path.exists():
            detail = (result.stderr or result.stdout or "No conversion details").strip()
            raise RuntimeError("Word-to-PDF conversion failed: " + detail[:500])
        pdf = pdf_path.read_bytes()
        reader = PdfReader(io.BytesIO(pdf))
        if not reader.pages or len(reader.pages) > 3:
            raise RuntimeError(f"Generated resume has {len(reader.pages)} pages. Please shorten the content and try again.")
        if len("\n".join(p.extract_text() or "" for p in reader.pages).strip()) < 250:
            raise RuntimeError("Generated PDF text validation failed. Please retry.")
        return pdf

def safe_filename(value: str, max_len: int = 45) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", value or "").strip("_")
    return (cleaned[:max_len].strip("_") or "Application")


# ---------------------------------------------------------------------
# PDF and email
# ---------------------------------------------------------------------
REFERENCE_LINKS = {
    "LinkedIn": "https://www.linkedin.com/in/adheeb-hashim/",
    "GitHub": "https://github.com/Adheebashim",
    "Portfolio": "https://adheebashim.dev/",
}


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract selectable text from an uploaded resume PDF."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 150:
        raise ValueError("I couldn't extract enough selectable text from this PDF. Please upload a text-based PDF, not a scanned image.")
    return text

def make_pdf(text: str) -> bytes:
    """Create a compact, professional resume PDF matching the reference layout."""
    buffer = io.BytesIO()
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=43, rightMargin=43,
        topMargin=31, bottomMargin=31, title="Adheeb Ashim - Tailored Resume",
        author="Adheeb Ashim",
    )

    name_style = ParagraphStyle(
        "ResumeName", parent=styles["Normal"], fontName="Helvetica-Bold",
        fontSize=22, leading=24, alignment=TA_CENTER,
        textColor=colors.HexColor("#17202A"), spaceAfter=2,
    )
    subtitle_style = ParagraphStyle(
        "ResumeSubtitle", parent=styles["Normal"], fontName="Helvetica-Bold",
        fontSize=10, leading=12, alignment=TA_CENTER, spaceAfter=3,
    )
    contact_style = ParagraphStyle(
        "ResumeContact", parent=styles["Normal"], fontName="Helvetica",
        fontSize=8.5, leading=10, alignment=TA_CENTER, spaceAfter=2,
    )
    body_style = ParagraphStyle(
        "ResumeBody", parent=styles["Normal"], fontName="Helvetica",
        fontSize=8.5, leading=10.4, alignment=TA_LEFT, spaceAfter=1.8,
        textColor=colors.HexColor("#202124"),
    )
    bullet_style = ParagraphStyle(
        "ResumeBullet", parent=body_style, leftIndent=11, firstLineIndent=-8,
        spaceAfter=2,
    )
    section_style = ParagraphStyle(
        "ResumeSection", parent=styles["Normal"], fontName="Helvetica-Bold",
        fontSize=10, leading=11.5, spaceBefore=7, spaceAfter=3,
        textColor=colors.HexColor("#17202A"), keepWithNext=True,
    )
    role_style = ParagraphStyle(
        "ResumeRole", parent=body_style, fontName="Helvetica-Bold",
        fontSize=8.5, leading=10.5, spaceBefore=3, spaceAfter=2, keepWithNext=True,
    )
    small_style = ParagraphStyle(
        "ResumeSmall", parent=body_style, fontSize=7.8, leading=9.5,
    )

    section_names = {
        "SUMMARY", "PROFILE", "PROFESSIONAL SUMMARY", "EXPERIENCE",
        "WORK EXPERIENCE", "PROJECTS", "SELECTED PROJECTS",
        "INDEPENDENT ANALYTICAL SOLUTIONS & AI PROJECTS", "SKILLS",
        "TECHNICAL SKILLS", "EDUCATION", "CERTIFICATIONS", "ACHIEVEMENTS",
        "PUBLICATIONS",
    }
    story = []
    lines = [line.strip() for line in text.replace("\r", "").splitlines() if line.strip()]
    first = lines[0] if lines else ""
    has_name_header = bool(re.search(r"adheeb|^[A-Z][A-Za-z]+\s+[A-Z][A-Za-z]+$", first, re.I))

    if has_name_header:
        story.append(Paragraph(escape(first), name_style))
        lines = lines[1:]
        header_count = 0
        while lines and header_count < 3:
            candidate = lines[0]
            if candidate.upper().rstrip(":") in section_names or candidate.startswith(("-", "•", "–")):
                break
            story.append(Paragraph(escape(candidate), subtitle_style if header_count == 0 else contact_style))
            lines = lines[1:]
            header_count += 1
        story.append(Spacer(1, 4))
    else:
        story.append(Paragraph("ADHEEB ASHIM", name_style))
        story.append(Paragraph("Data Analyst · Machine Learning · Automation · Advanced Analytics", subtitle_style))
        story.append(Paragraph(
            'Nilambur, Kerala (Open to Relocate) · +91 8281759103 / +91 7306134684 · '
            'adheebashim1010@gmail.com', contact_style))
        links_html = ' · '.join(
            f'<link href="{url}" color="#1A73E8"><u>{label}</u></link>'
            for label, url in REFERENCE_LINKS.items()
        )
        story.append(Paragraph(links_html, contact_style))
        story.append(Spacer(1, 4))

    for raw in lines:
        line = raw.strip()
        # Convert reference link labels into real hyperlinks in the PDF.
        if re.fullmatch(r"(?:LinkedIn|GitHub|Portfolio)(?:\s*[·|•/]\s*(?:LinkedIn|GitHub|Portfolio))*", line, re.I):
            links_html = ' · '.join(
                f'<link href="{url}" color="#1A73E8"><u>{label}</u></link>'
                for label, url in REFERENCE_LINKS.items()
            )
            story.append(Paragraph(links_html, contact_style))
            continue
        normalized = re.sub(r"[:\s]+$", "", line).upper()
        if normalized in section_names or (len(line) < 42 and line.isupper() and not line.startswith(("-", "•"))):
            story.append(Spacer(1, 2))
            story.append(Paragraph(escape(normalized), section_style))
            story.append(HRFlowable(width="100%", thickness=0.55, color=colors.HexColor("#AAB2BD"), spaceBefore=0, spaceAfter=3))
        elif line.startswith(("-", "•", "–", "*")):
            story.append(Paragraph("– " + escape(line[1:].strip()), bullet_style))
        elif len(line) < 105 and (" — " in line or " – " in line or " | " in line or (line.endswith(":") and len(line) < 65)):
            story.append(Paragraph(escape(line), role_style))
        elif re.match(r"^(Technologies|Tools|Skills|Languages|Analytics|AI &|Generative AI|Operational Delivery)\s*:", line, re.I):
            label, value = line.split(":", 1)
            story.append(Paragraph(f"<b>{escape(label)}:</b>{escape(value)}", small_style))
        else:
            story.append(Paragraph(escape(line), body_style))

    doc.build(story)
    return buffer.getvalue()


def send_mail(to: str, subject: str, pdf: bytes, filename: str, role: str = "", company: str = "") -> None:
    """Send a resume PDF through the Gmail API using OAuth refresh credentials."""
    required = {
        "GMAIL_CLIENT_ID": GMAIL_CLIENT_ID,
        "GMAIL_CLIENT_SECRET": GMAIL_CLIENT_SECRET,
        "GMAIL_REFRESH_TOKEN": GMAIL_REFRESH_TOKEN,
        "GMAIL_SENDER": GMAIL_SENDER,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "Missing Gmail API environment variable(s): "
            + ", ".join(missing)
            + ". Add them in Render → Environment."
        )

    token_response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": GMAIL_CLIENT_ID,
            "client_secret": GMAIL_CLIENT_SECRET,
            "refresh_token": GMAIL_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if not token_response.ok:
        try:
            detail = token_response.json()
        except ValueError:
            detail = token_response.text[:500]
        raise RuntimeError(
            f"Google OAuth token refresh failed ({token_response.status_code}): {detail}"
        )

    access_token = token_response.json().get("access_token")
    if not access_token:
        raise RuntimeError("Google OAuth did not return an access token.")

    message = EmailMessage()
    message["To"] = to
    sender_email = parseaddr(GMAIL_SENDER)[1] or GMAIL_SENDER
    message["From"] = formataddr(("Adheeb Ashim", sender_email))
    message["Subject"] = subject
    greeting = "Dear Hiring Team,"
    position = f" for the {role} position" if role else ""
    at_company = f" at {company}" if company else ""
    message.set_content(
        f"{greeting}\n\n"
        f"Please find attached my resume{position}{at_company} for your consideration.\n\n"
        "Regards,\n"
        "Adheeb Ashim\n"
    )
    message.add_attachment(
        pdf,
        maintype="application",
        subtype="pdf",
        filename=filename,
    )

    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    response = requests.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"raw": raw_message},
        timeout=30,
    )
    if not response.ok:
        try:
            detail = response.json()
        except ValueError:
            detail = response.text[:500]
        raise RuntimeError(
            f"Gmail API send failed ({response.status_code}): {detail}"
        )


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
            "Say hi to chat, send a public job-post link or job description, or upload a screenshot. "
            "I can read the screenshot, ask for the recruiter email if needed, "
            "tailor your resume and ask before emailing it. "
            "I'll automatically choose among your Data Engineer, AI Engineer, and Data Analyst Word templates. Upload a PDF only if you want to use a custom resume for one application. Send /help for all options."
        )


def sheet_url():
    return f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/edit" if GOOGLE_SHEET_ID else None


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


async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.effective_user.id == OWNER and update.effective_message:
        await update.effective_message.reply_text(
            "I can help you in these ways:\n"
            "• Say hi or ask a general question — I'll reply conversationally.\n"
            "• Send a job description as text, a public job-post link, or a screenshot.\n"
            "• I'll automatically select the best of your three Word templates: Data Engineer, AI Engineer, or Data Analyst.\n"
            "• You can upload a PDF if you want to use a custom resume for one application.\n"
            "• Send a public job-post link or upload a screenshot — I'll extract available text.\n"
            "• If the recruiter email is missing, I'll ask you for it.\n"
            "• I'll create a tailored PDF and show a simple job-fit score out of 10.\n"
            "• I'll email it only after you press the Send button.\n"
            "• Under each PDF, tap another template to regenerate it with that one.\n"
            "• /sheet (or just type \"sheet\") — link to your tracking sheet.\n"
            "• /quota (or \"quota\") — AI usage today."
        )


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
    """Select the best default role template, tailor it, and prepare an approval PDF."""
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

        # An uploaded PDF can be used as a one-off custom source; otherwise use the
        # automatically selected, role-specific Word template.
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
            if fit is None:  # model omitted the score: fall back to a small stand-alone call
                fit = decide(job_text, company_ctx, read_docx_text(TEMPLATES[template_role])).get("fit_score", 0)
            decision = {"fit_score": fit}
            pdf = docx_to_pdf(docx_bytes)
            used_model = "tiered Gemini/Groq"
            template_label = ROLE_LABELS[template_role]
        ctx.user_data.pop("selected_resume_text", None)
        ctx.user_data.pop("selected_resume_source", None)
    except Exception as exc:
        await status.edit_text(f"Sorry, I couldn't generate the resume: {str(exc)[:900]}")
        return

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
    except Exception as sheet_exc:
        sheet_row = None
        print(f"Google Sheets logging failed: {sheet_exc}")

    ctx.bot_data[key] = {
        "to": recipient, "pdf": pdf, "subject": subject, "filename": filename,
        "role": role or ROLE_LABELS.get(template_role, ""), "company": company,
        "resume_used": resume_used, "fit_score_10": fit_score_10,
        "sheet_row": sheet_row,
        # Kept so the template can be switched with one tap (template resumes only).
        "job_text": job_text, "company_ctx": company_ctx, "role_raw": role,
        "template_role": None if uploaded_text else template_role,
    }
    caption = _make_caption(role, company, resume_used, fit_score_10, template_role)
    keyboard = _approval_keyboard(key, None if uploaded_text else template_role, not uploaded_text)
    await status.delete()
    await message.reply_document(
        document=io.BytesIO(pdf), filename=filename, caption=caption, reply_markup=keyboard,
    )


async def on_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or user.id != OWNER or not message or not message.text:
        return

    text = message.text.strip()

    # Quick keywords (also available as /sheet and /quota).
    lowered = text.lower()
    if len(lowered) <= 40:
        if "sheet" in lowered:
            await sheet_command(update, ctx)
            return
        if any(w in lowered for w in ("quota", "tokens", "usage")):
            await quota_command(update, ctx)
            return

    # If the bot previously asked for an email, treat this message as the email.
    pending_job = ctx.user_data.get("pending_job_text")
    if pending_job:
        emails = re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text)
        if emails:
            ctx.user_data["pending_recipient"] = emails[0]
            await process_job_posting(update, ctx, pending_job)
            return
        await message.reply_text(
            "Please send a valid recruiter email address, such as recruiter@example.com."
        )
        return

    # Try extracting details from a public job-post URL. This does not bypass login
    # walls, anti-bot protections, or other access controls.
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
            # Keep the original URL as context; page text may omit the company/role.
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

    # A job description can include an email directly, or bot can ask for it.
    if is_likely_job_posting(text):
        ctx.user_data["application_source"] = "Text job description"
        await process_job_posting(update, ctx, text)
        return

    await interactive_reply(update, text)


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
        image_bytes = buffer.getvalue()
        extracted_text = extract_text_from_image(image_bytes, "image/jpeg")

        if not extracted_text.strip():
            await status.edit_text(
                "I couldn't read any text from that screenshot. Please send a clearer image."
            )
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
            send_mail(item["to"], item["subject"], item["pdf"], item["filename"], item.get("role", ""), item.get("company", ""))
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
# Application startup
# ---------------------------------------------------------------------
def _keep_alive(base_url: str) -> None:
    """Ping our own public URL so Render's free tier doesn't spin the bot down after 15 idle minutes."""
    interval = max(60, int(os.environ.get("KEEP_ALIVE_SECONDS", "600")))
    while True:
        time.sleep(interval)
        try:
            requests.get(base_url + "/", timeout=20)
        except Exception:
            pass


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
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("use_default", use_default_resume))
    application.add_handler(CommandHandler("sheet", sheet_command))
    application.add_handler(CommandHandler("quota", quota_command))
    application.add_handler(MessageHandler(filters.Document.PDF | (filters.Document.ALL), on_document))
    application.add_handler(MessageHandler(filters.PHOTO, on_photo))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_msg)
    )
    application.add_handler(CallbackQueryHandler(on_button))

    if os.environ.get("KEEP_ALIVE", "1") != "0":
        threading.Thread(target=_keep_alive, args=(base_url,), daemon=True).start()

    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=TOKEN,
        webhook_url=f"{base_url}/{TOKEN}",
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
