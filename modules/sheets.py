"""
Google Sheets module - application tracking.
"""

import json
from datetime import datetime

import gspread

from config.settings import (
    GOOGLE_SHEET_ID,
    GOOGLE_SERVICE_ACCOUNT_JSON,
    GOOGLE_SHEET_TAB,
)

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


def log_application(company, role, resume_used, recipient, fit_score, status, source, filename):
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


def update_application_status(row_number, status):
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


def sheet_url():
    return f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/edit" if GOOGLE_SHEET_ID else None
