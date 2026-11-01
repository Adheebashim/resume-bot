"""
LLM Provider Module - Smart routing across Inception, Gemini, and Groq
Automatically selects best provider based on task requirements, cost, and availability.
"""

import os
import re
import json
import time
import hashlib
import requests
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, Dict, List


def parse_json(text: str) -> dict:
    """Parse a JSON object even if the model wraps it in a code fence."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(), flags=re.I)
    match = re.search(r"\{.*\}", cleaned, re.S)
    if not match:
        return {}
    try:
        value = json.loads(match.group(0))
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


class LLMError(Exception):
    def __init__(self, status: int, message: str, response=None):
        super().__init__(message)
        self.status = status
        self.response = response


# API Keys (centralised in config.settings, which also feeds bot.py)
from config.settings import INCEPTION_KEY, GEMINI_KEY, GROQ_KEY

# Provider-specific configurations
PROVIDER_CONFIG = {
    "inception": {
        "base_url": "https://api.inceptionlabs.ai/v1/chat/completions",
        "models": {
            "light": "mercury-3",
            "heavy": "mercury-3",
            "vision": None,  # Add if Inception supports vision
        },
        "supports_json": True,
        "supports_vision": False,
        "cost_tier": 1,  # Lowest cost (10B free tokens)
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/models",
        "models": {
            "light": "gemini-2.0-flash",
            "heavy": "gemini-2.0-flash",
            "vision": "gemini-2.0-flash",
        },
        "supports_json": True,
        "supports_vision": True,
        "cost_tier": 2,
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1/chat/completions",
        "models": {
            "light": "llama-3.1-8b-instant",
            "heavy": "llama-3.3-70b-versatile",
            "vision": None,
        },
        "supports_json": True,
        "supports_vision": False,
        "cost_tier": 2,
    },
}

# Output token limits per task
MAX_OUT = {"light": 800, "chat": 500, "heavy": 3500, "vision": 2000, "email": 1200, "cover_letter": 1500}

# Cooldown tracking and caching
_cooldown: Dict[Tuple[str, str], float] = {}
_cache: OrderedDict = OrderedDict()
_CACHE_MAX = 128
_usage: Dict[Tuple[str, str], Dict] = {}


def _clean(text: str, limit: int) -> str:
    """Collapse whitespace and cap length."""
    text = re.sub(r"[ \t ]+", " ", text or "")
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    return text[:limit]


def _seconds_from_text(text: str) -> Optional[int]:
    """Parse retry delay from error messages."""
    m = re.search(r'(?:retrydelay"?\s*[:=]\s*"?|try again in\s*)((?:\d+h)?(?:\d+m)?(?:[\d.]+s)?)', text, re.I)
    if not m or not m.group(1):
        return None
    h = re.search(r"(\d+)h", m.group(1))
    mi = re.search(r"(\d+)m", m.group(1))
    sec = re.search(r"([\d.]+)s", m.group(1))
    total = (int(h.group(1)) * 3600 if h else 0) + (int(mi.group(1)) * 60 if mi else 0) + (float(sec.group(1)) if sec else 0)
    return int(total) or None


def _cooldown_for(err: LLMError) -> int:
    """Determine cooldown period after error."""
    body = str(err).lower()
    if err.status in (403, 404):
        return 6 * 3600
    if err.status == 429:
        if "limit: 0" in body:
            return 12 * 3600
        header = err.response.headers.get("retry-after") if err.response else None
        hint = int(float(header)) if header and header.replace(".", "", 1).isdigit() else _seconds_from_text(str(err))
        if "per day" in body or "daily" in body or (hint and hint > 900):
            return min(hint or 3 * 3600, 6 * 3600)
        return max(20, min(hint or 60, 900))
    if err.status >= 500:
        return 30
    return 0


def _day_key(provider: str) -> str:
    """Get day key for quota tracking (timezone-aware)."""
    tz = timezone.utc
    if provider == "gemini":
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("America/Los_Angeles")
        except Exception:
            tz = timezone(timedelta(hours=-8))
    return datetime.now(tz).strftime("%Y-%m-%d")


def _record(provider: str, model: str, tokens, headers=None) -> None:
    """Track usage statistics."""
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


def _inception_once(model: str, task: str, prompt: str, json_mode: bool, optional: bool) -> str:
    """Call Inception API (OpenAI-compatible)."""
    body = {
        "model": model,
        "temperature": 0.3,
        "max_tokens": MAX_OUT[task],
        "messages": [{"role": "user", "content": prompt}],
    }
    if json_mode and optional:
        body["response_format"] = {"type": "json_object"}

    try:
        r = requests.post(
            PROVIDER_CONFIG["inception"]["base_url"],
            headers={"Authorization": f"Bearer {INCEPTION_KEY}"},
            json=body,
            timeout=90,
        )
    except requests.RequestException as exc:
        raise LLMError(0, f"inception/{model}: {exc}")

    if not r.ok:
        raise LLMError(r.status_code, f"inception/{model} HTTP {r.status_code}: {r.text[:220]}", r)

    data = r.json()
    _record("inception", model, data.get("usage", {}).get("total_tokens"), r.headers)

    choice = data["choices"][0]
    text = choice["message"].get("content") or ""

    if choice.get("finish_reason") == "length" and json_mode:
        raise LLMError(0, f"inception/{model}: output truncated")

    return text


def _gemini_once(model: str, task: str, prompt: str, json_mode: bool, image, optional: bool) -> str:
    """Call Gemini API."""
    gen = {"temperature": 0.3, "maxOutputTokens": MAX_OUT[task]}
    if optional and json_mode:
        gen["responseMimeType"] = "application/json"

    parts = [{"text": prompt}]
    if image:
        parts.append({"inline_data": {"mime_type": image[0], "data": image[1]}})

    try:
        r = requests.post(
            f"{PROVIDER_CONFIG['gemini']['base_url']}/{model}:generateContent",
            headers={"x-goog-api-key": GEMINI_KEY},
            json={"contents": [{"parts": parts}], "generationConfig": gen},
            timeout=90,
        )
    except requests.RequestException as exc:
        raise LLMError(0, f"gemini/{model}: {exc}")

    if not r.ok:
        raise LLMError(r.status_code, f"gemini/{model} HTTP {r.status_code}: {r.text[:220]}", r)

    data = r.json()
    _record("gemini", model, (data.get("usageMetadata") or {}).get("totalTokenCount"))

    cand = (data.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if not p.get("thought"))

    if cand.get("finishReason") == "MAX_TOKENS" and json_mode:
        raise LLMError(0, f"gemini/{model}: output truncated")

    return text


def _groq_once(model: str, task: str, prompt: str, json_mode: bool, optional: bool) -> str:
    """Call Groq API (OpenAI-compatible)."""
    body = {
        "model": model,
        "temperature": 0.3,
        "max_tokens": MAX_OUT[task],
        "messages": [{"role": "user", "content": prompt}],
    }
    if optional and json_mode:
        body["response_format"] = {"type": "json_object"}

    try:
        r = requests.post(
            PROVIDER_CONFIG["groq"]["base_url"],
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            json=body,
            timeout=90,
        )
    except requests.RequestException as exc:
        raise LLMError(0, f"groq/{model}: {exc}")

    if not r.ok:
        raise LLMError(r.status_code, f"groq/{model} HTTP {r.status_code}: {r.text[:220]}", r)

    data = r.json()
    _record("groq", model, (data.get("usage") or {}).get("total_tokens"), r.headers)

    choice = data["choices"][0]
    text = choice["message"].get("content") or ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)

    if choice.get("finish_reason") == "length" and json_mode:
        raise LLMError(0, f"groq/{model}: output truncated")

    return text


def _smart_chain(task: str, image=None) -> List[Tuple[str, str]]:
    """
    Smart provider selection based on task requirements.

    Priority logic:
    1. Vision tasks -> only Gemini (if image present)
    2. Cost-sensitive tasks (light/chat) -> Inception first (10B free tokens)
    3. Quality-critical tasks (heavy/email/cover_letter) -> balanced across all
    4. Respect availability (API keys present)
    5. Skip providers on cooldown
    """
    chain = []

    # Vision requirement
    if image:
        if GEMINI_KEY and PROVIDER_CONFIG["gemini"]["supports_vision"]:
            model = PROVIDER_CONFIG["gemini"]["models"]["vision"]
            if model:
                chain.append(("gemini", model))
        return chain

    # Cost-optimized tasks: prefer Inception (free tier)
    if task in ("light", "chat"):
        if INCEPTION_KEY:
            chain.append(("inception", PROVIDER_CONFIG["inception"]["models"]["light"]))
        if GEMINI_KEY:
            chain.append(("gemini", PROVIDER_CONFIG["gemini"]["models"]["light"]))
        if GROQ_KEY:
            chain.append(("groq", PROVIDER_CONFIG["groq"]["models"]["light"]))

    # Quality-critical tasks: balanced approach
    elif task in ("heavy", "email", "cover_letter"):
        # Start with Inception (free, good quality)
        if INCEPTION_KEY:
            chain.append(("inception", PROVIDER_CONFIG["inception"]["models"]["heavy"]))
        # Gemini as backup (good reasoning)
        if GEMINI_KEY:
            chain.append(("gemini", PROVIDER_CONFIG["gemini"]["models"]["heavy"]))
        # Groq as final fallback
        if GROQ_KEY:
            chain.append(("groq", PROVIDER_CONFIG["groq"]["models"]["heavy"]))

    # Default fallback
    else:
        for provider in ["inception", "gemini", "groq"]:
            key = {"inception": INCEPTION_KEY, "gemini": GEMINI_KEY, "groq": GROQ_KEY}[provider]
            if key:
                model = PROVIDER_CONFIG[provider]["models"].get("light") or PROVIDER_CONFIG[provider]["models"].get("heavy")
                if model:
                    chain.append((provider, model))

    return chain


def ask(prompt: str, task: str = "light", json_mode: bool = False, image=None, use_cache: bool = True) -> Tuple[str, str]:
    """
    Smart LLM routing with automatic failover.

    Returns: (response_text, "provider/model")
    """
    # Enforce English output unless the prompt explicitly asks for something else
    # (prevents models like mercury-3 from drifting into other languages).
    if not re.search(r"respond (only |strictly )?in english|english only|reply in english", prompt, re.I):
        prompt = prompt.rstrip() + "\n\nImportant: Respond in English only."

    chain = _smart_chain(task, image)

    if not chain:
        raise RuntimeError("No LLM provider configured. Add INCEPTION_API_KEY, GEMINI_API_KEY, or GROQ_API_KEY.")

    # Cache check
    cache_key = hashlib.sha256(
        f"{task}|{json_mode}|{prompt}|{image[1][:2000] if image else ''}".encode()
    ).hexdigest()

    if use_cache and cache_key in _cache:
        return _cache[cache_key]

    # Filter out providers on cooldown
    now = time.time()
    candidates = [c for c in chain if _cooldown.get(c, 0) <= now] or chain

    errors = []
    for provider, model in candidates:
        optional = True
        for attempt in range(3):
            try:
                if provider == "inception":
                    text = _inception_once(model, task, prompt, json_mode, optional)
                elif provider == "gemini":
                    text = _gemini_once(model, task, prompt, json_mode, image, optional)
                elif provider == "groq":
                    text = _groq_once(model, task, prompt, json_mode, optional)
                else:
                    continue

                if not text.strip():
                    errors.append(f"{provider}/{model}: empty response")
                    break

                result = (text, f"{provider}/{model}")

                # Cache successful result
                if use_cache:
                    _cache[cache_key] = result
                    while len(_cache) > _CACHE_MAX:
                        _cache.popitem(last=False)

                return result

            except LLMError as exc:
                if exc.status == 400 and optional:
                    optional = False
                    continue

                wait = _cooldown_for(exc)
                if wait:
                    _cooldown[(provider, model)] = time.time() + wait

                if exc.status >= 500 and attempt < 2:
                    time.sleep(2)
                    continue

                errors.append(str(exc)[:200])
                break

    raise RuntimeError(f"All LLM providers failed: {' | '.join(errors)}")


def test_providers() -> list:
    """Ping every configured LLM provider with a minimal prompt.

    Returns a list of dicts: {provider, model, status, latency_ms, detail}
    status is "ok" or "error"; latency_ms is None on failure.
    """
    results = []
    tiny = "Reply with the single word: ok"
    for provider in ("inception", "gemini", "groq"):
        key = {"inception": INCEPTION_KEY, "gemini": GEMINI_KEY, "groq": GROQ_KEY}[provider]
        if not key:
            results.append({"provider": provider, "model": None, "status": "skipped",
                            "latency_ms": None, "detail": "No API key set"})
            continue
        model = PROVIDER_CONFIG[provider]["models"].get("light") or PROVIDER_CONFIG[provider]["models"].get("heavy")
        start = time.time()
        try:
            if provider == "inception":
                _inception_once(model, "light", tiny, False, True)
            elif provider == "gemini":
                _gemini_once(model, "light", tiny, False, None, True)
            else:
                _groq_once(model, "light", tiny, False, True)
            latency_ms = int((time.time() - start) * 1000)
            results.append({"provider": provider, "model": model, "status": "ok",
                            "latency_ms": latency_ms, "detail": f"Responded in {latency_ms} ms"})
        except LLMError as exc:
            latency_ms = int((time.time() - start) * 1000)
            results.append({"provider": provider, "model": model, "status": "error",
                            "latency_ms": latency_ms, "detail": str(exc)[:160]})
        except Exception as exc:  # noqa: BLE001
            results.append({"provider": provider, "model": model, "status": "error",
                            "latency_ms": None, "detail": str(exc)[:160]})
    return results


def usage_report() -> str:
    """Generate usage statistics report."""
    now = time.time()
    lines = ["📊 LLM Usage Report (since bot restart)\n"]

    for provider in ["inception", "gemini", "groq"]:
        models = set()
        for task_type in ["light", "heavy", "vision"]:
            model = PROVIDER_CONFIG[provider]["models"].get(task_type)
            if model:
                models.add(model)

        if not models:
            continue

        provider_title = provider.capitalize()
        if provider == "gemini":
            provider_title += " (resets midnight Pacific)"
        elif provider == "groq":
            provider_title += " (resets 00:00 UTC)"
        elif provider == "inception":
            provider_title += " (10B free tokens)"

        rows = []
        for model in models:
            u = _usage.get((provider, model))
            if u and u["day"] != _day_key(provider):
                u = None

            paused = _cooldown.get((provider, model), 0) - now

            if not u and paused <= 0:
                continue

            text = f"• {model}: "
            if u:
                tokens_k = u['tokens'] / 1000
                text += f"{u['calls']} call{'s' if u['calls'] != 1 else ''}, {tokens_k:.1f}k tokens"
            else:
                text += "no calls"

            if paused > 0:
                text += f" · ⏸ paused ~{max(1, int(paused // 60))} min"

            rows.append(text)

        if rows:
            lines.append(f"\n{provider_title}:")
            lines.extend(rows)

    if len(lines) == 1:
        lines.append("\nNo AI calls yet today.")

    return "\n".join(lines)
