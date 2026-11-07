"""
Web Dashboard Module - Monitor bot status, usage, applications, and live controls.
Uses Tornado (already installed and compatible with PTB).
"""

import html as _html
import json
import time
import logging
from datetime import datetime, timezone
import tornado.ioloop
import tornado.web
from typing import Dict, List, Optional

from config.settings import (
    OWNER,
    OWNER_NAME,
    ROLE_LABELS,
    BASE_URL,
    PORT,
    RUNTIME_SETTINGS,
    persist_runtime_settings,
    INCEPTION_KEY,
    GEMINI_KEY,
    GROQ_KEY,
)
from modules.settings_store import save_settings_to_sheets
from modules.llm_provider import usage_report, _usage, _cooldown, test_providers

log = logging.getLogger("resume-bot.dashboard")

# Global reference to the PTB application so dashboard endpoints can trigger Telegram messages
_BOT_APPLICATION = None

STATS = {
    "start_time": time.time(),
    "job_events": [],  # List of timestamps when jobs were processed
    "email_events": [],  # List of timestamps when emails were sent
    "last_job": None,
    "applications": [],  # List of recent application dicts
    "scan_running": False,
    "last_scan_result": None,
}


_BG_TASKS = set()  # keeps references to background draft tasks so they aren't garbage-collected


def _scan_results_html(result) -> str:
    """HTML for the 'Last scan results' panel (server-rendered, also returned by the scan API)."""
    jobs = (result or {}).get("jobs") or []
    if not jobs:
        msg = "No scan has been run since the bot started. Click “Run a scan now”." if not result \
            else "The last scan found no new matches."
        return f'<div class="empty">{_html.escape(msg)}</div>'
    rows = []
    for j in jobs:
        url = str(j.get("url") or "")
        title = _html.escape(str(j.get("title") or "Untitled"))
        if url.startswith(("http://", "https://")):
            title = f'<a href="{_html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{title}</a>'
        emails = j.get("emails") or []
        if emails:
            contact = _html.escape(", ".join(emails[:2]))
            status = '<span class="ok-text">Draft sent to Telegram</span>'
        else:
            contact = "—"
            status = '<span class="muted-text">No HR email — open link</span>'
        fit = j.get("fit_score") or 0
        rows.append(
            "<tr>"
            f"<td>{_html.escape(str(j.get('role_label') or ''))}</td>"
            f"<td>{title}</td>"
            f"<td>{fit}/100</td>"
            f"<td>{contact}</td>"
            f"<td>{status}</td>"
            "</tr>"
        )
    return (
        '<div class="table-wrap"><table><thead><tr>'
        '<th scope="col">Resume</th><th scope="col">Job</th><th scope="col">Fit</th>'
        '<th scope="col">HR email</th><th scope="col">Status</th>'
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )


def set_bot_application(app):
    global _BOT_APPLICATION
    _BOT_APPLICATION = app

class DashboardHandler(tornado.web.RequestHandler):
    def get(self):
        uptime_sec = int(time.time() - STATS["start_time"])
        uptime_h = uptime_sec // 3600
        uptime_m = (uptime_sec % 3600) // 60
        uptime_str = f"{uptime_h}h {uptime_m}m"

        interval = RUNTIME_SETTINGS.get("ALERT_INTERVAL_MIN", 15)
        results = RUNTIME_SETTINGS.get("ALERT_MAX_RESULTS", 6)
        loop_on = bool(RUNTIME_SETTINGS.get("JOB_SCAN_LOOP", True))
        checked = "checked" if loop_on else ""
        # Prepare data for dashboard
        now = time.time()
        def count_in_period(events, period_sec):
            return len([e for e in events if now - e <= period_sec])

        # Default: All Time (None)
        jobs_count = len(STATS["job_events"])
        emails_count = len(STATS["email_events"])

        apps_json = json.dumps(STATS["applications"], ensure_ascii=True).replace("</", "<\\/")

        # New: stats data for client-side filtering
        stats_data = {
            "all": {"jobs": jobs_count, "emails": emails_count},
            "day": {"jobs": count_in_period(STATS["job_events"], 86400), "emails": count_in_period(STATS["email_events"], 86400)},
            "week": {"jobs": count_in_period(STATS["job_events"], 604800), "emails": count_in_period(STATS["email_events"], 604800)}
        }
        stats_json = json.dumps(stats_data)

        webhook_state = "Active" if BASE_URL else "Local (dev)"
        usage_text = _html.escape(usage_report())

        css = """
:root {
  --bg: #0a0d13;
  --surface: #11161f;
  --surface2: #18202c;
  --surface3: #212b3a;
  --ink: #e9edf5;
  --muted: #8b96ab;
  --hairline: #1e2735;
  --hairline2: #2b3748;
  --accent: #8b7cf7;
  --accent-strong: #6f5de8;
  --accent-soft: rgba(139, 124, 247, .16);
  --cyan: #2fd4ee;
  --ok: #3ad6a0;
  --warn: #f6bf4a;
  --bad: #f57d7d;
  --info: #4cb4f5;
  --focus: rgba(139, 124, 247, .4);
  --shadow-lg: 0 18px 44px -18px rgba(0, 0, 0, .55);
  --shadow-sm: 0 4px 14px -6px rgba(0, 0, 0, .45);
}
* { box-sizing: border-box; }
html, body { margin: 0; }
html { scrollbar-color: var(--surface3) transparent; }
body {
  background:
    radial-gradient(1100px 480px at 50% -12%, rgba(30, 41, 66, .9) 0%, rgba(10, 13, 19, 0) 62%),
    radial-gradient(800px 400px at 88% 8%, rgba(47, 212, 238, .05) 0%, rgba(10, 13, 19, 0) 55%),
    var(--bg);
  color: var(--ink);
  font: 15px/1.5 "Figtree", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  -webkit-font-smoothing: antialiased;
  min-height: 100vh;
}
::selection { background: rgba(139, 124, 247, .35); }
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-thumb { background: var(--surface3); border-radius: 8px; border: 2px solid var(--bg); }
::-webkit-scrollbar-track { background: transparent; }

/* --- Topbar --- */
.topbar {
  background: color-mix(in srgb, var(--surface) 82%, transparent);
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  border-bottom: 1px solid var(--hairline);
  position: sticky; top: 0; z-index: 20;
}
.wrap { max-width: 1120px; margin: 0 auto; padding: 0 28px; }
.topbar-inner { height: 64px; display: flex; align-items: center; justify-content: space-between; }
.brand { display: flex; align-items: center; gap: 11px; font-weight: 700; letter-spacing: -.01em; font-size: 16px; }
.brand-mark {
  width: 26px; height: 26px; border-radius: 8px;
  background: linear-gradient(135deg, var(--accent), var(--cyan));
  position: relative;
  box-shadow: 0 0 0 1px rgba(255, 255, 255, .08) inset, 0 0 18px rgba(124, 108, 240, .5);
  display: inline-block;
}
.brand-mark::after {
  content: ""; position: absolute; inset: 7px; border-radius: 4px;
  border: 2px solid #fff; opacity: .92;
}
.status-pill {
  display: flex; align-items: center; gap: 8px;
  font-size: 13px; color: var(--muted);
  background: var(--surface2); border: 1px solid var(--hairline);
  border-radius: 999px; padding: 5px 14px;
}
.dot {
  width: 8px; height: 8px; border-radius: 50%; background: var(--ok);
  animation: pulse 2.4s ease-in-out infinite;
  box-shadow: 0 0 8px rgba(58, 214, 160, .7);
}
@keyframes pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(58, 214, 160, .45); }
  50% { box-shadow: 0 0 0 6px rgba(58, 214, 160, 0); }
}

/* --- Stats --- */
.stats {
  display: grid; grid-template-columns: repeat(4, 1fr);
  background: linear-gradient(180deg, var(--surface), var(--surface) 60%, color-mix(in srgb, var(--surface) 82%, var(--bg)));
  border: 1px solid var(--hairline);
  border-top: none;
  border-radius: 0 0 18px 18px;
  box-shadow: var(--shadow-lg);
  margin-bottom: 26px;
}
.stats-header { display: flex; justify-content: flex-end; padding: 12px 18px 0; }
.stats-filter-select {
  appearance: none; -webkit-appearance: none;
  background: var(--surface2) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%238b96ab' stroke-width='1.5' fill='none' stroke-linecap='round'/%3E%3C/svg%3E") no-repeat right 10px center;
  color: var(--ink); border: 1px solid var(--hairline2);
  border-radius: 8px; padding: 6px 28px 6px 12px; font-size: 12.5px;
  font-weight: 600; cursor: pointer;
  transition: border-color .15s, box-shadow .15s;
}
.stats-filter-select:hover { border-color: var(--muted); }
.stats-filter-select:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--focus); }
.stat {
  position: relative; padding: 24px 20px 22px;
  display: flex; flex-direction: column; gap: 4px;
}
.stat + .stat { border-left: 1px solid var(--hairline); }
.stat-icon {
  width: 36px; height: 36px; border-radius: 11px;
  display: grid; place-items: center;
  background: var(--surface2); border: 1px solid var(--hairline2);
  color: var(--accent); margin-bottom: 14px;
  box-shadow: var(--shadow-sm);
}
.stat-icon svg { width: 18px; height: 18px; display: block; }
.stat:nth-child(2) .stat-icon { color: var(--cyan); }
.stat:nth-child(3) .stat-icon { color: var(--info); }
.stat:nth-child(4) .stat-icon { color: var(--ok); }
.stat-num {
  font-size: 30px; font-weight: 700; letter-spacing: -.025em;
  font-variant-numeric: tabular-nums; margin: 0;
  background: linear-gradient(180deg, #fff, var(--ink) 70%);
  -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent;
}
.stat-label { font-size: 12.5px; color: var(--muted); margin: 0; font-weight: 500; letter-spacing: .01em; }

/* --- Layout --- */
.grid {
  display: grid; grid-template-columns: 7fr 5fr;
  gap: 26px; margin-bottom: 26px;
}
.panel {
  background: var(--surface);
  border: 1px solid var(--hairline);
  border-radius: 18px;
  padding: 26px 28px;
  box-shadow: var(--shadow-sm);
}
.grid .panel { margin: 0; }
.panel h2 { font-size: 15px; font-weight: 650; margin: 0; letter-spacing: .01em; }
.sub-panel + .sub-panel { margin-top: 30px; }
.panel-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 18px; }

/* --- Forms --- */
.field { margin-bottom: 20px; }
.field label { display: block; font-weight: 600; font-size: 13.5px; margin-bottom: 7px; color: var(--ink); }
.field input[type="number"],
.select, .search-input {
  border: 1px solid var(--hairline2); border-radius: 10px;
  background: var(--surface2); color: var(--ink);
  font: inherit;
  transition: border-color .15s, box-shadow .15s, background .15s;
}
.field input[type="number"] {
  width: 150px; height: 40px; padding: 0 13px;
  font-variant-numeric: tabular-nums;
}
.field input[type="number"]:hover, .select:hover, .search-input:hover { border-color: var(--muted); }
.field input[type="number"]:focus, .select:focus, .search-input:focus {
  outline: none; border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--focus);
}
.hint { font-size: 12.5px; color: var(--muted); margin: 7px 0 0; line-height: 1.5; }
.field-switch {
  display: flex; align-items: center; justify-content: space-between;
  gap: 16px; padding: 14px 16px;
  background: var(--surface2); border: 1px solid var(--hairline);
  border-radius: 12px;
}
.switch {
  appearance: none; -webkit-appearance: none; flex: none;
  width: 44px; height: 25px; border-radius: 999px;
  border: 1px solid var(--hairline2); background: var(--surface3);
  position: relative; cursor: pointer; margin: 0;
  transition: background .18s, border-color .18s;
}
.switch::after {
  content: ""; position: absolute; top: 3px; left: 3px;
  width: 17px; height: 17px; border-radius: 50%;
  background: #c6cdda; transition: transform .18s, background .18s;
  box-shadow: 0 1px 3px rgba(0, 0, 0, .4);
}
.switch:checked { background: var(--accent); border-color: var(--accent); }
.switch:checked::after { transform: translateX(19px); background: #fff; }
.switch:focus-visible { box-shadow: 0 0 0 3px var(--focus); }
.actions { display: flex; gap: 10px; margin-top: 26px; }
.btn {
  height: 40px; padding: 0 18px; border-radius: 10px;
  border: 1px solid transparent; font: inherit;
  font-weight: 600; font-size: 14px; cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  transition: background .18s, border-color .18s, color .18s, box-shadow .18s, transform .08s;
}
.btn:active { transform: translateY(1px) scale(.99); }
.btn.primary {
  background: linear-gradient(135deg, var(--accent), #5f7cf0);
  color: #fff;
  box-shadow: 0 6px 18px -6px rgba(124, 108, 240, .55);
}
.btn.primary:hover { box-shadow: 0 8px 24px -6px rgba(124, 108, 240, .65); filter: brightness(1.08); }
.btn.primary:disabled { opacity: .55; cursor: default; box-shadow: none; filter: none; }
.btn.ghost { background: var(--surface2); border-color: var(--hairline2); color: var(--ink); }
.btn.ghost:hover { border-color: var(--muted); background: var(--surface3); }
.btn:focus-visible { outline: none; box-shadow: 0 0 0 3px var(--focus); }

/* --- Usage panel --- */
.usage pre {
  margin: 0; font-family: inherit; font-size: 13px; line-height: 1.65;
  color: #b8c2d6; white-space: pre-wrap;
  background: var(--surface2); border: 1px solid var(--hairline);
  border-radius: 12px; padding: 14px 16px;
  font-variant-numeric: tabular-nums;
}

/* --- AI providers --- */
.ai-list { display: grid; gap: 10px; }
.ai-provider {
  display: flex; align-items: center; gap: 13px;
  background: var(--surface2); border: 1px solid var(--hairline);
  border-radius: 13px; padding: 13px 15px;
  transition: border-color .15s, transform .15s;
}
.ai-provider:hover { border-color: var(--hairline2); transform: translateX(2px); }
.ai-provider .p-name { font-weight: 650; font-size: 14px; }
.ai-provider .p-model { font-size: 12px; color: var(--muted); font-family: ui-monospace, "Cascadia Mono", Consolas, monospace; }
.provider-dot { width: 9px; height: 9px; border-radius: 50%; flex: none; }
.provider-dot.ok { background: var(--ok); box-shadow: 0 0 8px rgba(58, 214, 160, .6); }
.provider-dot.error { background: var(--bad); box-shadow: 0 0 8px rgba(245, 125, 125, .5); }
.provider-dot.skipped { background: var(--muted); }
.ai-provider .p-detail { font-size: 12.5px; color: var(--muted); }
.ai-provider .p-latency {
  margin-left: auto; font-size: 12.5px;
  font-variant-numeric: tabular-nums; color: var(--muted); white-space: nowrap;
  background: var(--surface3); border-radius: 999px; padding: 2px 10px;
}

/* --- Filters --- */
.filters { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; }
.search-input { flex: 1; min-width: 180px; height: 40px; padding: 0 14px; }
.search-input::placeholder { color: var(--muted); }
.select { height: 40px; padding: 0 13px; cursor: pointer; }
.count {
  font-size: 12.5px; font-weight: 600; color: var(--muted);
  background: var(--surface2); border: 1px solid var(--hairline);
  border-radius: 999px; padding: 4px 12px;
  font-variant-numeric: tabular-nums;
}

/* --- Scan results --- */
td a { color: var(--info); text-decoration: none; }
td a:hover { text-decoration: underline; }
.ok-text { color: var(--ok); font-weight: 600; }
.muted-text { color: var(--muted); }

/* --- Table --- */
.table-wrap {
  overflow: hidden; border: 1px solid var(--hairline);
  border-radius: 14px; background: var(--surface2);
}
table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
thead { display: table-header-group; }
th {
  font-size: 11.5px; font-weight: 700; color: var(--muted);
  text-align: left; padding: 12px 16px; letter-spacing: .06em; text-transform: uppercase;
  border-bottom: 1px solid var(--hairline); background: var(--surface3);
  position: sticky; top: 0;
}
td { padding: 13px 16px; border-bottom: 1px solid var(--hairline); font-variant-numeric: tabular-nums; }
tbody tr { transition: background .12s; }
tbody tr:nth-child(even) td { background: rgba(255, 255, 255, .015); }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover td { background: rgba(139, 124, 247, .07); }
.fitchip {
  display: inline-block; font-size: 12px; font-weight: 600;
  color: #c9bcff; background: var(--accent-soft);
  border: 1px solid rgba(139, 124, 247, .3);
  border-radius: 999px; padding: 2px 10px;
}
.statuschip { font-size: 12px; font-weight: 600; padding: 2px 10px; border-radius: 999px; color: var(--muted); background: var(--surface3); }
.statuschip.ok { color: var(--ok); background: rgba(58, 214, 160, .12); }
.statuschip.warn { color: var(--warn); background: rgba(246, 191, 74, .12); }
.statuschip.bad { color: var(--bad); background: rgba(245, 125, 125, .12); }
.empty { text-align: center; color: var(--muted); padding: 30px 16px; font-size: 13.5px; }

/* --- Footer / toast --- */
.foot { color: var(--muted); font-size: 12.5px; text-align: center; padding: 26px 0 36px; }
.toast {
  position: fixed; bottom: 24px; left: 50%;
  transform: translate(-50%, 16px);
  background: var(--surface3); color: var(--ink);
  border: 1px solid var(--hairline2); box-shadow: var(--shadow-lg);
  font-size: 13.5px; padding: 11px 20px; border-radius: 12px;
  opacity: 0; pointer-events: none;
  transition: opacity .22s, transform .22s;
  z-index: 50; max-width: calc(100vw - 48px);
}
.toast.show { opacity: 1; transform: translate(-50%, 0); }
.toast.error { background: rgba(185, 28, 28, .92); border-color: var(--bad); }

/* --- Responsive --- */
@media (max-width: 900px) {
  .grid { grid-template-columns: 1fr; }
  .wrap { padding: 0 18px; }
}
@media (max-width: 760px) {
  .stats { grid-template-columns: 1fr 1fr; }
  .stat + .stat { border-left: none; }
  .stat:nth-child(even) { border-left: 1px solid var(--hairline); }
  .stat:nth-child(n+3) { border-top: 1px solid var(--hairline); }
  .panel { padding: 20px 18px; }
}
@media (prefers-reduced-motion: reduce) {
  .dot { animation: none; }
  * { transition: none !important; }
}
"""

        js = """
const $ = sel => document.querySelector(sel);
function toast(msg, isError = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.toggle('error', isError);
  t.classList.add('show');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => t.classList.remove('show'), 3600);
}

const form = $('#settings-form');
form.addEventListener('submit', async e => {
  e.preventDefault();
  const btn = $('#save-btn');
  btn.disabled = true;
  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        ALERT_INTERVAL_MIN: parseInt($('#alert-interval').value, 10),
        ALERT_MAX_RESULTS: parseInt($('#alert-max-results').value, 10),
        JOB_SCAN_LOOP: $('#scan-loop-toggle').checked
      })
    });
    const data = await res.json();
    if (res.ok) {
      const where = data.persisted === 'sheets' ? 'Google Sheets' : 'this device';
      toast(`Settings saved to ${where}.`);
    } else {
      toast(data.error || 'Could not save settings.', true);
    }
  } catch (err) {
    toast('Network error: ' + err.message, true);
  } finally {
    btn.disabled = false;
  }
});

const scanBtn = $('#trigger-scan');
scanBtn.addEventListener('click', async () => {
  scanBtn.disabled = true;
  const original = scanBtn.textContent;
  scanBtn.textContent = 'Scanning…';
  try {
    const res = await fetch('/api/trigger-scan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: '{}'
    });
    const data = await res.json();
    if (res.ok) {
      const n = data.found ?? 0;
      const box = document.getElementById('scan-results');
      if (box && data.html) box.innerHTML = data.html;
      const meta = document.getElementById('scan-meta');
      if (meta) meta.textContent = n + ' job(s) · ' + (data.scanned_at || '');
      toast(n
        ? `Scan complete — ${n} job(s) found, ${data.with_email || 0} with an HR email. Sent to Telegram.`
        : 'Scan complete — no new matches.');
      if (box) box.scrollIntoView({behavior: 'smooth', block: 'nearest'});
    } else {
      toast(data.error || 'Scan failed.', true);
    }
  } catch (err) {
    toast('Network error: ' + err.message, true);
  } finally {
    scanBtn.disabled = false;
    scanBtn.textContent = original;
  }
});

// --- Stats period filtering --------------------------------------------
const statsPeriod = $('#stats-period');
statsPeriod.addEventListener('change', () => {
    const data = window.STATS_DATA[statsPeriod.value];
    $('#stat-jobs').textContent = data.jobs;
    $('#stat-emails').textContent = data.emails;
});

// --- CSV Export -----------------------------------------------------------
$('#export-csv').addEventListener('click', () => {
    const csv = ['Time,Company,Role,Fit,Status', ...APPS.map(a =>
        [a.time, a.company, a.role, a.fit, a.status].map(v => `"${(v||'').replace(/"/g, '""')}"`).join(',')
    )].join('\\n');
    const blob = new Blob([csv], {type: 'text/csv'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `applications-${new Date().toISOString().slice(0,10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
    toast('CSV exported.');
});

// --- AI provider status --------------------------------------------------
const PROVIDER_LABELS = { inception: 'Inception', gemini: 'Gemini', groq: 'Groq' };

function providerRow(p) {
  const dotClass = p.status === 'ok' ? 'ok' : (p.status === 'error' ? 'error' : 'skipped');
  const statusText = p.status === 'ok' ? 'online'
    : p.status === 'error' ? 'error'
    : 'not configured';
  const latency = p.latency_ms != null ? `${p.latency_ms} ms` : '—';
  const model = p.model ? `<span class="p-model">${p.model}</span>` : '';
  return `
    <div class="ai-provider">
      <span class="provider-dot ${dotClass}" style="background:${p.status === 'ok' ? 'var(--ok)' : p.status === 'error' ? 'var(--bad)' : 'var(--muted)'}" aria-hidden="true"></span>
      <div>
        <div class="p-name">${PROVIDER_LABELS[p.provider] || p.provider}</div>
        ${model}
      </div>
      <span class="p-detail">${statusText} — ${p.detail || ''}</span>
      <span class="p-latency">${latency}</span>
    </div>`;
}

let providerTestRunning = false;
async function testProviders() {
  if (providerTestRunning) return;
  providerTestRunning = true;
  const btn = $('#test-providers-btn');
  btn.disabled = true;
  btn.textContent = 'Testing…';
  try {
    const res = await fetch('/api/test-providers', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: '{}'
    });
    const data = await res.json();
    if (res.ok && data.results) {
      $('#ai-list').innerHTML = data.results.map(providerRow).join('');
      const ok = data.results.filter(p => p.status === 'ok').length;
      $('#ai-online').textContent = `${ok}/${data.results.length}`;
      toast(`AI check complete — ${ok} of ${data.results.length} providers online.`);
    } else {
      $('#ai-list').innerHTML = '<div class="empty">Provider check failed — see server logs.</div>';
      toast(data.error || 'Provider check failed.', true);
    }
  } catch (err) {
    $('#ai-list').innerHTML = '<div class="empty">Could not reach the AI check endpoint.</div>';
    toast('Network error: ' + err.message, true);
  } finally {
    providerTestRunning = false;
    btn.disabled = false;
    btn.textContent = 'Test now';
  }
}
$('#test-providers-btn').addEventListener('click', testProviders);

// --- Applications: filters + client-side rendering ------------------------
const APPS = window.APPS || [];
const fitSel = $('#fit-filter');
const statusSel = $('#status-filter');
const searchInput = $('#search-input');

function unique(values) { return [...new Set(values.filter(Boolean))]; }
function fillSelect(sel, values, allLabel) {
  const current = sel.value;
  sel.innerHTML = `<option value="All">${allLabel}</option>` +
    values.map(v => `<option value="${v}">${v}</option>`).join('');
  if (values.includes(current)) sel.value = current;
}

function renderApps() {
  fillSelect(fitSel, unique(APPS.map(a => a.fit)), 'All fits');
  fillSelect(statusSel, unique(APPS.map(a => a.status)), 'All statuses');
  const fit = fitSel.value;
  const status = statusSel.value;
  const q = searchInput.value.trim().toLowerCase();
  let shown = 0;
  const rows = [...APPS].reverse().map(a => {
    const aFit = String(a.fit || '');
    const aStatus = String(a.status || '');
    const matchFit = fit === 'All' || aFit === fit;
    const matchStatus = status === 'All' || aStatus === status;
    const matchQ = !q || String(a.company || '').toLowerCase().includes(q)
      || String(a.role || '').toLowerCase().includes(q);
    if (!(matchFit && matchStatus && matchQ)) return '';
    shown += 1;
    const fitChip = a.fit
      ? `<span class="fitchip">${a.fit}</span>`
      : '<span class="statuschip">—</span>';
    const statusClass = aStatus.toLowerCase().includes('sent') ? 'ok'
      : aStatus.toLowerCase().includes('await') ? 'warn'
      : aStatus.toLowerCase().includes('reject') ? 'bad' : '';
    return `<tr>
      <td>${a.time || ''}</td>
      <td>${a.company || 'Unknown'}</td>
      <td>${a.role || 'Unknown'}</td>
      <td>${fitChip}</td>
      <td><span class="statuschip ${statusClass}">${aStatus}</span></td>
    </tr>`;
  }).join('');

  if (!rows) {
    const msg = APPS.length === 0
      ? 'No applications processed in this session yet — send a job post to the bot to get started.'
      : 'No applications match the current filters.';
    $('#apps-body').innerHTML = `<tr><td colspan="5" class="empty">${msg}</td></tr>`;
  } else {
    $('#apps-body').innerHTML = rows;
  }
  const countEl = $('#apps-count');
  countEl.textContent = `${shown} of ${APPS.length}`;
}

fitSel.addEventListener('change', renderApps);
statusSel.addEventListener('change', renderApps);
searchInput.addEventListener('input', renderApps);

renderApps();

// Auto-run the provider check when the page loads.
testProviders();
"""

        _scan = STATS.get("last_scan_result")
        scan_html = _scan_results_html(_scan)
        scan_meta = _html.escape(f"{_scan['found']} job(s) · {_scan['scanned_at']}") if _scan else ""

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Resume Bot · Dashboard</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Figtree:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>{css}</style>
</head>
<body>
    <div class="topbar">
        <div class="wrap topbar-inner">
            <div class="brand"><span class="brand-mark" aria-hidden="true"></span>Resume Bot</div>
            <div class="status-pill"><span class="dot" aria-hidden="true"></span>Online <span class="uptime-note">· up {uptime_str}</span></div>
        </div>
    </div>

    <main class="wrap">
        <section class="stats" aria-label="Statistics">
            <div class="stats-header" style="grid-column: 1 / -1; padding: 0 16px;">
                <select id="stats-period" class="stats-filter-select">
                    <option value="all">All time</option>
                    <option value="day">Last 24h</option>
                    <option value="week">Last 7 days</option>
                </select>
            </div>
            <div class="stat">
                <div class="stat-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/></svg></div>
                <p class="stat-num" id="stat-jobs">{jobs_count}</p>
                <p class="stat-label">Jobs processed</p>
            </div>
            <div class="stat">
                <div class="stat-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/></svg></div>
                <p class="stat-num" id="stat-emails">{emails_count}</p>
                <p class="stat-label">Emails sent</p>
            </div>
            <div class="stat">
                <div class="stat-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg></div>
                <p class="stat-num">{uptime_str}</p>
                <p class="stat-label">Uptime</p>
            </div>
            <div class="stat">
                <div class="stat-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v3m0 12v3m9-9h-3M6 12H3m13.5-6.5-2 2m-7 7-2 2m11 0-2-2m-7-7-2-2"/></svg></div>
                <p class="stat-num" id="ai-online">–</p>
                <p class="stat-label">AI providers online</p>
            </div>
        </section>

        <div class="grid">
            <section class="panel" aria-labelledby="setup-h">
                <h2 id="setup-h">Setup</h2>
                <form id="settings-form" class="setup-form">
                    <div class="field">
                        <label for="alert-interval">Alert interval</label>
                        <input type="number" id="alert-interval" min="1" max="10080" value="{interval}">
                        <p class="hint">Minutes between job scans.</p>
                    </div>
                    <div class="field">
                        <label for="alert-max-results">Max results per scan</label>
                        <input type="number" id="alert-max-results" min="1" max="30" value="{results}">
                        <p class="hint">How many matches to keep in each scan.</p>
                    </div>
                    <div class="field field-switch">
                        <div>
                            <label for="scan-loop-toggle">Background scan</label>
                            <p class="hint">Keep searching for new job postings on a timer.</p>
                        </div>
                        <input type="checkbox" id="scan-loop-toggle" class="switch" {checked} aria-label="Background scan">
                    </div>
                    <div class="actions">
                        <button type="submit" class="btn primary" id="save-btn">Save changes</button>
                        <button type="button" class="btn ghost" id="trigger-scan">Run a scan now</button>
                    </div>
                </form>
            </section>

            <div class="panel">
                <section class="sub-panel" aria-labelledby="models-h">
                    <div class="panel-head">
                        <h2 id="models-h">AI models</h2>
                        <button type="button" class="btn ghost" id="test-providers-btn">Test now</button>
                    </div>
                    <div class="ai-list" id="ai-list"><div class="empty">Testing provider connectivity…</div></div>
                </section>
                <section class="sub-panel" aria-labelledby="usage-h">
                    <h2 id="usage-h">AI usage</h2>
                    <div class="usage"><pre>{usage_text}</pre></div>
                    <p class="hint">Resets when the bot restarts.</p>
                </section>
            </div>
        </div>

        <section class="panel" aria-labelledby="scan-h">
            <div class="panel-head">
                <h2 id="scan-h">Last scan results</h2>
                <span class="count" id="scan-meta">{scan_meta}</span>
            </div>
            <div id="scan-results">{scan_html}</div>
        </section>

        <section class="panel" aria-labelledby="apps-h">
            <div class="panel-head">
                <h2 id="apps-h">Recent applications</h2>
                <div style="display: flex; gap: 10px; align-items: center;">
                    <button type="button" class="btn ghost" id="export-csv" style="height: 32px; padding: 0 10px; font-size: 12px;">Export CSV</button>
                    <span class="count" id="apps-count">{len(STATS["applications"])} total</span>
                </div>
            </div>
            <div class="filters">
                <input type="search" id="search-input" class="search-input"
                       placeholder="Search company or role…" aria-label="Search applications">
                <select id="fit-filter" class="select" aria-label="Filter by fit"></select>
                <select id="status-filter" class="select" aria-label="Filter by status"></select>
            </div>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th scope="col">Time</th>
                            <th scope="col">Company</th>
                            <th scope="col">Role</th>
                            <th scope="col">Fit</th>
                            <th scope="col">Status</th>
                        </tr>
                    </thead>
                    <tbody id="apps-body"></tbody>
                </table>
            </div>
        </section>

        <footer class="foot">{OWNER_NAME} · build v2.1 · webhook {webhook_state}</footer>
    </main>

    <div class="toast" id="toast" role="status" aria-live="polite"></div>
    <script>window.APPS = {apps_json}; window.STATS_DATA = {stats_json};</script>
    <script>{js}</script>
</body>
</html>
"""
        self.write(html)

class StatusApiHandler(tornado.web.RequestHandler):
    def get(self):
        self.set_header("Content-Type", "application/json")
        self.write({
            "status": "online",
            "uptime_sec": time.time() - STATS["start_time"],
            "processed_jobs": len(STATS["job_events"]),
            "emails_sent": len(STATS["email_events"]),
            "last_job": STATS["last_job"],
            "settings": dict(RUNTIME_SETTINGS),
        })

class SettingsApiHandler(tornado.web.RequestHandler):
    """POST endpoint to update runtime settings from the dashboard."""
    def post(self):
        self.set_header("Content-Type", "application/json")
        try:
            body = json.loads(self.request.body or b"{}")
        except Exception:
            self.set_status(400)
            self.write({"error": "Invalid JSON body"})
            return

        updated = {}
        if "ALERT_INTERVAL_MIN" in body:
            try:
                interval = max(1, int(body["ALERT_INTERVAL_MIN"]))
            except (TypeError, ValueError):
                self.set_status(400)
                self.write({"error": "ALERT_INTERVAL_MIN must be a number >= 1"})
                return
            RUNTIME_SETTINGS["ALERT_INTERVAL_MIN"] = interval
            updated["ALERT_INTERVAL_MIN"] = interval

        if "ALERT_MAX_RESULTS" in body:
            try:
                results = max(1, min(30, int(body["ALERT_MAX_RESULTS"])))
            except (TypeError, ValueError):
                self.set_status(400)
                self.write({"error": "ALERT_MAX_RESULTS must be a number between 1 and 30"})
                return
            RUNTIME_SETTINGS["ALERT_MAX_RESULTS"] = results
            updated["ALERT_MAX_RESULTS"] = results

        if "JOB_SCAN_LOOP" in body:
            raw = body["JOB_SCAN_LOOP"]
            if isinstance(raw, str):
                enabled = raw.strip().lower() in ("1", "true", "yes", "on")
            else:
                enabled = bool(raw)
            RUNTIME_SETTINGS["JOB_SCAN_LOOP"] = enabled
            updated["JOB_SCAN_LOOP"] = enabled

        log.info("Dashboard settings updated: %s", updated)
        # Persist locally (fallback) and to Google Sheets (survives redeploys).
        persist_runtime_settings()
        sheets_ok = save_settings_to_sheets(dict(RUNTIME_SETTINGS))
        self.write({
            "ok": True,
            "updated": updated,
            "settings": dict(RUNTIME_SETTINGS),
            "persisted": "sheets" if sheets_ok else "local",
        })

class TriggerScanApiHandler(tornado.web.RequestHandler):
    """POST endpoint to run a job scan on demand from the dashboard."""
    async def post(self):
        self.set_header("Content-Type", "application/json")
        if STATS["scan_running"]:
            self.write({"ok": False, "error": "A scan is already running.", "found": 0})
            return

        STATS["scan_running"] = True
        try:
            import asyncio
            from modules.job_matching import run_job_scan
            loop = asyncio.get_running_loop()
            jobs = await loop.run_in_executor(None, run_job_scan, True)

            with_email = sum(1 for j in jobs if j.get("emails"))
            if _BOT_APPLICATION and jobs:
                # Telegram: full job list first, then drafts (with the email review step)
                # for every job that has an HR email. Runs in the background so this
                # request returns immediately.
                from bot import handle_scan_results
                task = asyncio.create_task(handle_scan_results(
                    _BOT_APPLICATION.bot,
                    _BOT_APPLICATION.bot_data,
                    jobs,
                    source="Dashboard Manual Scan",
                    chat_id=OWNER or None,
                ))
                _BG_TASKS.add(task)
                task.add_done_callback(_BG_TASKS.discard)

            stats = {
                "found": len(jobs),
                "count": len(jobs),
                "with_email": with_email,
                "scanned_at": datetime.now().strftime("%H:%M:%S"),
                "jobs": [
                    {
                        "title": j.get("title", ""),
                        "url": j.get("url", ""),
                        "role_label": j.get("role_label", ""),
                        "emails": j.get("emails", []),
                        "fit_score": j.get("fit_score", 0),
                    }
                    for j in jobs
                ],
            }
            STATS["last_scan_result"] = stats
            self.write({"ok": True, **stats, "html": _scan_results_html(stats)})
        except Exception as exc:
            log.exception("Manual scan from dashboard failed")
            self.write({"ok": False, "error": str(exc), "found": 0})
        finally:
            STATS["scan_running"] = False

class TestProvidersApiHandler(tornado.web.RequestHandler):
    """POST endpoint to ping every configured LLM provider."""
    def post(self):
        self.set_header("Content-Type", "application/json")
        try:
            results = test_providers()
            self.write({"ok": True, "results": results})
        except Exception as exc:
            log.exception("Provider test from dashboard failed")
            self.set_status(500)
            self.write({"ok": False, "error": str(exc)})

def create_dashboard_app(ptb_webhook_handler: Optional[tornado.web.RequestHandler] = None, token: str = ""):
    """Create the Tornado application with both Dashboard and Webhook routes."""
    routes = [
        (r"/", DashboardHandler),
        (r"/api/status", StatusApiHandler),
        (r"/api/settings", SettingsApiHandler),
        (r"/api/trigger-scan", TriggerScanApiHandler),
        (r"/api/test-providers", TestProvidersApiHandler),
    ]

    if ptb_webhook_handler and token:
        # Note: ptb_webhook_handler should be the PTB class or instance
        routes.append((f"/{token}", ptb_webhook_handler))
        log.info("Registered Telegram webhook route at /%s", token[:5] + "...")

    return tornado.web.Application(routes)

def log_job_processed(company: str, role: str, fit: str, status: str = "Awaiting Approval"):
    """Update live dashboard stats from the bot logic."""
    now = time.time()
    STATS["job_events"].append(now)
    app_entry = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp": now,
        "company": company,
        "role": role,
        "fit": fit,
        "status": status
    }
    STATS["applications"].append(app_entry)
    STATS["last_job"] = app_entry
    if len(STATS["applications"]) > 100: STATS["applications"].pop(0)
    if len(STATS["job_events"]) > 1000: STATS["job_events"].pop(0)

def log_email_sent():
    """Update live dashboard stats when an email is successfully sent."""
    STATS["email_events"].append(time.time())
    if STATS["applications"]:
        STATS["applications"][-1]["status"] = "Sent"
