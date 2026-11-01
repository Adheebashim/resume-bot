"""
settings_store.py — cloud-persisted runtime settings.

Saves dashboard-controlled settings (alert interval, max results, scan loop)
to a dedicated "Bot Settings" tab in the user's Google Sheets spreadsheet.
This makes changes survive redeploys and container restarts (unlike a local
file on Render's ephemeral filesystem). The local JSON file is kept as a
fallback when Google Sheets is not configured.
"""

import json
from datetime import datetime

from config.settings import (
    GOOGLE_SHEET_ID,
    GOOGLE_SERVICE_ACCOUNT_JSON,
    RUNTIME_SETTINGS_FILE,
)

try:
    import gspread
except ImportError:  # pragma: no cover
    gspread = None

SETTINGS_TAB = "Bot Settings"
SETTINGS_KEYS = (
    "ALERT_INTERVAL_MIN",
    "ALERT_MAX_RESULTS",
    "JOB_SCAN_LOOP",
)
HEADERS = ["Key", "Value", "Updated At"]


def _get_spreadsheet():
    """Return the configured spreadsheet, or None if Sheets tracking is absent."""
    if gspread is None or not GOOGLE_SHEET_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return None
    try:
        credentials_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        client = gspread.service_account_from_dict(credentials_info)
        return client.open_by_key(GOOGLE_SHEET_ID)
    except Exception as exc:  # noqa: BLE001
        print(f"settings_store: could not open spreadsheet: {exc}")
        return None


def _get_settings_worksheet():
    spreadsheet = _get_spreadsheet()
    if spreadsheet is None:
        return None
    try:
        return spreadsheet.worksheet(SETTINGS_TAB)
    except gspread.WorksheetNotFound:
        try:
            ws = spreadsheet.add_worksheet(title=SETTINGS_TAB, rows=20, cols=3)
            ws.append_row(list(HEADERS), value_input_option="USER_ENTERED")
            return ws
        except Exception as exc:  # noqa: BLE001
            print(f"settings_store: could not create settings tab: {exc}")
            return None


def _normalize(key: str, raw: str):
    """Convert a sheet string back to the right Python type."""
    if key == "JOB_SCAN_LOOP":
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def load_settings_from_sheets() -> dict:
    """Return a dict of settings persisted in Google Sheets, or {} if unavailable."""
    ws = _get_settings_worksheet()
    if ws is None:
        return {}
    try:
        rows = ws.get_all_values()
    except Exception as exc:  # noqa: BLE001
        print(f"settings_store: could not read settings tab: {exc}")
        return {}

    result = {}
    for row in rows[1:]:
        if len(row) < 2:
            continue
        key, raw = row[0].strip(), row[1].strip()
        if key in SETTINGS_KEYS:
            value = _normalize(key, raw)
            if value is not None:
                result[key] = value
    return result


def save_settings_to_sheets(settings: dict) -> bool:
    """Persist the given settings to Google Sheets. Returns True on success."""
    ws = _get_settings_worksheet()
    if ws is None:
        return False
    try:
        now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        rows = [list(HEADERS)]
        for key in SETTINGS_KEYS:
            if key not in settings:
                continue
            value = settings[key]
            if isinstance(value, bool):
                value = "true" if value else "false"
            rows.append([key, str(value), now])
        ws.clear()
        ws.update(rows, value_input_option="USER_ENTERED")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"settings_store: could not save settings tab: {exc}")
        return False


def apply_persisted_settings() -> None:
    """Merge any Google Sheets settings into RUNTIME_SETTINGS at startup.

    Priority: Sheets > local file > environment defaults. After applying, the
    local JSON file is refreshed to stay in sync as a fallback.
    """
    from config.settings import RUNTIME_SETTINGS, persist_runtime_settings

    sheet_settings = load_settings_from_sheets()
    if sheet_settings:
        RUNTIME_SETTINGS.update(sheet_settings)
        persist_runtime_settings()
        print("settings_store: loaded persisted settings from Google Sheets")
    else:
        print("settings_store: no Google Sheets settings found; using local/env defaults")
