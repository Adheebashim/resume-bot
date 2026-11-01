"""
Conversation Module - More natural multi-turn dialogue with intent understanding
and lightweight context memory per job application session.
"""

import time
from typing import Optional

from modules.llm_provider import ask, _clean


# Simple intent patterns (fast, offline - doesn't burn LLM quota)
# Order matters: more specific patterns first.
INTENT_PATTERNS = {
    "jobs_search": ("find jobs", "search jobs", "job alerts", "alerts", "matching jobs", "match jobs", "job search"),
    "outreach": ("outreach", "recruiter", "referral", "cold message", "linkedin", "connect"),
    "apply": ("apply", "job", "vacancy", "opening", "role", "position", "hiring", "career"),
    "screenshot": ("screenshot", "photo", "image", "picture", "screen"),
    "email": ("email", "mail", "gmail", "address", "send"),
    "cover_letter": ("cover letter", "coverletter", "letter"),
    "resume": ("resume", "cv", "curriculum", "template"),
    "sheet": ("sheet", "spreadsheet", "tracker", "track"),
    "quota": ("quota", "tokens", "usage", "limit", "left"),
    "help": ("help", "what can you do", "how do you work", "commands"),
    "thanks": ("thanks", "thank you", "ty", "appreciate"),
    "greeting": ("hi", "hello", "hey", "good morning", "good afternoon", "good evening", "yo", "namaste"),
}


def classify_intent(text: str) -> str:
    """Fast keyword-based intent classification (no LLM needed)."""
    lowered = text.lower()
    for intent, keywords in INTENT_PATTERNS.items():
        for kw in keywords:
            if kw in lowered:
                return intent
    return "general"


def get_greeting(name: Optional[str] = None) -> str:
    return f"Hi {name}! 👋 What can I help you with today?" if name else "Hi! 👋 What can I help you with today?"


def should_capitalize_context(text: str) -> bool:
    """Heuristic: a context question about the current job flow."""
    return any(w in text.lower() for w in ("this job", "that job", "this role", "current", "the job"))


async def interactive_reply(message, text: str, ctx=None) -> None:
    """
    Reply naturally using intent-aware LLM chat with lightweight session memory.
    `ctx` (optional) is the PTB ContextTypes.DEFAULT_TYPE to fetch user_data.
    """
    intent = classify_intent(text)
    simple_replies = {
        "thanks": "You're welcome! Glad I could help. 😊",
        "greeting": get_greeting(),
    }
    if intent in simple_replies:
        await message.reply_text(simple_replies[intent])
        return

    # Build short user memory for better follow-up answers
    history = ""
    if ctx is not None:
        user_data = ctx.user_data
        recent = user_data.get("chat_history", [])
        if recent:
            history = "Recent conversation:\n" + "\n".join(recent[-6:]) + "\n"
    try:
        answer, _provider = ask(
            "You are a concise, friendly Telegram assistant for a resume-tailoring bot. "
            "Always reply in English. Reply briefly. Don't claim actions you haven't performed. "
            "If the user seems to share a job post, ask for the recruiter's email.\n\n"
            + history +
            f"User: {_clean(text, 1200)}",
            task="chat",
            use_cache=False,
        )
        if ctx is not None:
            hist = ctx.user_data.get("chat_history", [])
            hist.append(f"User: {text[:200]}")
            hist.append(f"Bot: {answer[:200]}")
            ctx.user_data["chat_history"] = hist[-20:]
        await message.reply_text(answer[:4000])
    except Exception:
        await message.reply_text(
            "I can help with job descriptions, screenshots, resume tailoring, and "
            "emailing after your approval. Send /help to see the options."
        )
