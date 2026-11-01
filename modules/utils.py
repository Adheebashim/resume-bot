"""
Shared utilities - URL/SSRF validation, public-page parsing, vision OCR, PDF text.
"""

import io
import re
import base64
import socket
import ipaddress
import requests
from html.parser import HTMLParser
from urllib.parse import urlparse

from pypdf import PdfReader

from modules.llm_provider import ask, GEMINI_KEY
from config.settings import REFERENCE_LINKS


# ---------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------
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


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract selectable text from an uploaded resume PDF."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 150:
        raise ValueError("I couldn't extract enough selectable text from this PDF. "
                         "Please upload a text-based PDF, not a scanned image.")
    return text


# ---------------------------------------------------------------------
# Public page fetching with SSRF protection
# ---------------------------------------------------------------------
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
                "description", "og:title", "og:description", "twitter:title", "twitter:description"
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
    """Read public page text only; never bypass login, anti-bot, or access controls."""
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


# ---------------------------------------------------------------------
# Small text helpers
# ---------------------------------------------------------------------
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


def safe_filename(value: str, max_len: int = 45) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", value or "").strip("_")
    return (cleaned[:max_len].strip("_") or "Application")


def make_references_line() -> str:
    return " · ".join(REFERENCE_LINKS.values())
