"""
Central configuration for the enhanced resume bot.
All values can be overridden through Render Environment variables.
Runtime dashboard settings are persisted to a JSON file so changes survive restarts.
"""

import json
import os
import threading
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    """Parse an int env var; fall back to default when unset or empty."""
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


# --- Telegram / deployment -------------------------------------------------
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER = _env_int("OWNER_ID", 0)
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = _env_int("PORT", 10000)

# --- LLM API keys ----------------------------------------------------------
# Inception Labs (10B free tokens, OpenAI-compatible).
INCEPTION_KEY = os.environ.get("INCEPTION_API_KEY")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
GROQ_KEY = os.environ.get("GROQ_API_KEY")

# --- Gmail -----------------------------------------------------------------
GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID")
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET")
GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN")
GMAIL_SENDER = os.environ.get("GMAIL_SENDER")

# --- Google Sheets tracking ------------------------------------------------
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SHEET_TAB = os.environ.get("GOOGLE_SHEET_TAB", "Applications")

# --- Resume templates ------------------------------------------------------
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

# --- Job alerts & Runtime Controls -----------------------------------------
ALERT_INTERVAL_MIN = _env_int("ALERT_INTERVAL_MIN", 15)
ALERT_MAX_RESULTS = _env_int("ALERT_MAX_RESULTS", 6)
JOB_SCAN_LOOP = os.environ.get("JOB_SCAN_LOOP", "1").strip() in ("1", "true", "True")

# Where persisted dashboard settings live (JSON file on disk).
RUNTIME_SETTINGS_FILE = Path(
    os.environ.get("RUNTIME_SETTINGS_FILE", "config/runtime_settings.json")
)

_SAVE_LOCK = threading.Lock()


def _env_defaults() -> dict:
    """Return the original env-based defaults for runtime settings."""
    return {
        "ALERT_INTERVAL_MIN": ALERT_INTERVAL_MIN,
        "ALERT_MAX_RESULTS": ALERT_MAX_RESULTS,
        "JOB_SCAN_LOOP": JOB_SCAN_LOOP,
    }


def _load_persisted_settings() -> dict:
    """Load settings previously saved from the Web Dashboard (if any).

    Persisted values win over env defaults, since they reflect the user's
    latest choices made from the dashboard.
    """
    defaults = _env_defaults()
    try:
        if RUNTIME_SETTINGS_FILE.exists():
            with open(RUNTIME_SETTINGS_FILE, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            if isinstance(saved, dict):
                for key, value in saved.items():
                    if key in defaults:
                        defaults[key] = value
    except Exception as exc:  # noqa: BLE001
        print(f"Could not read persisted settings: {exc}")
    return defaults


# Dynamic runtime settings controlling the bot, live from the Web Dashboard.
RUNTIME_SETTINGS = _load_persisted_settings()


def persist_runtime_settings() -> None:
    """Write current runtime settings to disk (thread-safe)."""
    with _SAVE_LOCK:
        try:
            RUNTIME_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = RUNTIME_SETTINGS_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(RUNTIME_SETTINGS, fh, indent=2)
            tmp.replace(RUNTIME_SETTINGS_FILE)
        except Exception as exc:  # noqa: BLE001
            print(f"Could not persist runtime settings: {exc}")

# --- Owner profile (used by cover letters / emails) ------------------------
OWNER_NAME = "Adheeb Ashim"
OWNER_EMAIL = "adheebashim1010@gmail.com"
OWNER_LOCATION = "Nilambur, Kerala (Open to Relocate)"
OWNER_PHONE = "+91 8281759103"
REFERENCE_LINKS = {
    "LinkedIn": "https://www.linkedin.com/in/adheeb-hashim/",
    "GitHub": "https://github.com/Adheebashim",
    "Portfolio": "https://adheebashim.dev/",
}