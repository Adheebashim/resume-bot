# Resume Bot v2 — Enhanced Modular Version

Upgrade of the original single-file `bot.py` with new features and a cleaner architecture.

## What's new

1. **Third LLM provider: Inception Labs** — `modules/llm_provider.py` routes smartly:
   - Vision (screenshots) → Gemini only (supports images)
   - Light/chat tasks (classification, fit scores, chat) → Inception first (10B free tokens), then Gemini, then Groq
   - Heavy/email/cover-letter tasks → Inception → Gemini → Groq, with auto-failover, cooldown tracking, caching, and `/quota` usage reports that include Inception
2. **Personalized email generation** (`modules/email.py`) — every application gets a custom email body instead of the generic template; embeds the tailored cover letter as formatted text.
3. **Cover letters for ALL applications** (`modules/cover_letter.py`) — length adapts to role seniority / JD detail (160–320 words); strictly limited to facts in your resume.
4. **Smart job matching alerts** (`modules/job_matching.py`) — searches fresh postings per role, extracts HR/recruiter emails, scores fit against all three base resumes, and can run on a timer (`ALERT_INTERVAL_MIN`, default 15 min). Trigger manually with `/alerts`.
5. **Recruiter detection & outreach drafting** (`modules/recruiter.py`) — detects recruiter emails / LinkedIn profiles, drafts perfect cold / referral messages for you to copy (manual connect + send, no automation).
6. **Web Monitoring Dashboard** (`modules/web_dashboard.py`) — A built-in web interface served directly at your Render root URL (`https://your-bot.onrender.com/`). Monitors system uptime, total jobs processed, emails sent, live AI usage quotas across all providers, and a live table of recent applications.
7. **Conversational Intelligence** (`modules/conversation.py`) — Intent classification and multi-turn memory. All outputs are strictly in English.

## Structure

```
resume-bot-enhanced/
├── bot.py                  # Telegram handlers + app wiring
├── config/settings.py      # all env vars, templates, references
├── modules/
│   ├── llm_provider.py     # Inception + Gemini + Groq smart routing
│   ├── resume_logic.py      # extract / research / decide / tailor / tailor_docx
│   ├── pdf_renderer.py      # docx→PDF (LibreOffice) + ReportLab custom PDF
│   ├── email.py             # personalized email body + Gmail send
│   ├── cover_letter.py      # tailored cover letters
│   ├── job_matching.py      # job search + fit scoring + HR email extraction
│   ├── recruiter.py         # outreach / referral drafting
│   ├── conversation.py      # intents + multi-turn memory
│   ├── sheets.py            # Google Sheets tracking
│   └── utils.py             # OCR, public-page fetch (SSRF-safe), helpers
├── templates/               # keep your 3 Word templates here
├── requirements.txt
├── Dockerfile
├── render.yaml
└── README.md
```

## Deploying to Render

1. Push this folder to your GitHub repo (replace the old `bot.py`, `requirements.txt`, `Dockerfile`, `render.yaml`).
2. In Render → your `adheeb-resume-bot` service → **Environment**, add one new variable:
   - `INCEPTION_API_KEY` = your Inception Labs key (`sk_b1b4…`)
3. Optional new variables (all have defaults):
   - `ALERT_INTERVAL_MIN` → minutes between job scans (default `15`)
   - `JOB_SCAN_LOOP` → `0` disables background scanning (default `1`)
4. Redeploy. The bot keeps all existing behavior (template selection, fit score, approval before send, Google Sheets tracking, /quota).

## Live Web Dashboard Controls

When the bot is running, open your Render root URL — it serves a built-in monitoring dashboard
(`https://your-bot.onrender.com/`) that shows system uptime, jobs processed, emails sent, AI usage,
and a live list of recent applications. The dashboard also lets you control the bot in real time
without redeploying or setting environment variables:

- **Alert Interval (minutes)** — change how often the background job scanner runs. Takes effect on the next loop cycle.
- **Max Results per Scan** — how many job matches to keep per scan.
- **Background Job Scan Loop** — toggle the scanner on/off.
- **Run Job Scan Now** — trigger an immediate scan.

The controls are powered by these API endpoints:
- `GET /api/status` — JSON status + current settings
- `POST /api/settings` — update `ALERT_INTERVAL_MIN`, `ALERT_MAX_RESULTS`, `JOB_SCAN_LOOP` (JSON body)
- `POST /api/trigger-scan` — run a job scan immediately (JSON body `{"force": true}`)

### Persistence

Settings changed from the dashboard are saved **twice**, so they survive restarts
**and** redeploys:

1. **Google Sheets (source of truth)** — a `Bot Settings` tab is created in your
   tracking spreadsheet (same one used for applications). This is cloud-hosted, so
   it survives any Render redeploy, restart, or fresh container.
2. **Local file (fallback)** — `config/runtime_settings.json` is updated too, and is
   used if Google Sheets isn't configured (e.g. `GOOGLE_SHEET_ID` /
   `GOOGLE_SERVICE_ACCOUNT_JSON` missing).

On startup, the bot loads settings with this priority: **Sheets > local file > env
defaults**. So whatever you set from the dashboard last is what the bot uses, even
after a redeploy.

The dashboard's Save button tells you where the settings were written ("Google Sheets"
or "local file").

## Notes

- Keep `GEMINI_API_KEY` — it's required for screenshot OCR and is the vision fallback.
- Keep `GROQ_API_KEY` and `GEMINI_API_KEY` as fallbacks even if Inception is your main provider.
- The old generic email body is retained as an automatic fallback if the LLM call fails.
- Job alerts are displayed on demand via `/alerts`; the background loop logs matches (push-to-chat can be enabled later if you want proactive alerts).
