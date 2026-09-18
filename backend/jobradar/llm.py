"""LLM access, provider-agnostic.

Gemini is tried first and Groq is the fallback, because their free tiers fail in different
ways: Gemini 2.5 Flash allows far more tokens per minute (~250K vs Groq's ~6-8K), which
matters when the payload is a batch of job descriptions, while Groq's per-day request
allowance is higher. Running out on one provider should not stop the pipeline, so a quota
error on Gemini silently continues on Groq.

Nothing here is on the critical path: if both providers fail, `complete_json` returns None
and enrich.py falls back to heuristics. A run must never fail because of the LLM.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

# Free-tier request-per-minute ceilings. Staying under these is cheaper than handling the
# 429s they produce.
GEMINI_RPM = 10
GROQ_RPM = 28


class Quota(Exception):
    """Provider refused for quota/rate reasons — try the next provider, do not retry here."""


class _Throttle:
    """Simple RPM limiter shared by all callers of one provider."""

    def __init__(self, rpm: int) -> None:
        self.interval = 60.0 / max(rpm, 1)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        # Checked per call rather than at import so tests (and a paid-tier key, which has
        # much higher limits) can switch throttling off without reloading the module.
        if os.getenv("JOBRADAR_NO_THROTTLE"):
            return
        async with self._lock:
            delta = time.monotonic() - self._last
            if delta < self.interval:
                await asyncio.sleep(self.interval - delta)
            self._last = time.monotonic()


_gemini_throttle = _Throttle(GEMINI_RPM)
_groq_throttle = _Throttle(GROQ_RPM)


def _strip_fence(text: str) -> str:
    """Models wrap JSON in ```json fences even when told not to."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def available() -> bool:
    return bool(os.getenv("GEMINI_API_KEY") or os.getenv("GROQ_API_KEY"))


async def _gemini(
    client: httpx.AsyncClient, prompt: str, schema: dict[str, Any] | None
) -> str | None:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None

    config: dict[str, Any] = {"temperature": 0.1, "responseMimeType": "application/json"}
    if schema:
        config["responseSchema"] = schema

    await _gemini_throttle.wait()
    resp = await client.post(
        GEMINI_URL.format(model=GEMINI_MODEL),
        headers={"x-goog-api-key": key},
        json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": config},
        timeout=120.0,
    )
    if resp.status_code in (429, 503):
        raise Quota(f"gemini {resp.status_code}")
    resp.raise_for_status()

    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        return None
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts) or None


async def _groq(client: httpx.AsyncClient, prompt: str, _schema: dict | None) -> str | None:
    key = os.getenv("GROQ_API_KEY")
    if not key:
        return None

    await _groq_throttle.wait()
    resp = await client.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            # Groq has no schema enforcement, only "must be valid JSON".
            "response_format": {"type": "json_object"},
        },
        timeout=120.0,
    )
    if resp.status_code in (429, 503):
        raise Quota(f"groq {resp.status_code}")
    resp.raise_for_status()

    choices = resp.json().get("choices") or []
    return choices[0]["message"]["content"] if choices else None


async def complete_json(
    client: httpx.AsyncClient,
    prompt: str,
    *,
    schema: dict[str, Any] | None = None,
) -> Any | None:
    """Ask for JSON, falling through providers. Returns None if nothing usable came back."""
    for name, fn in (("gemini", _gemini), ("groq", _groq)):
        try:
            raw = await fn(client, prompt, schema)
        except Quota as exc:
            log.info("%s quota exhausted (%s); falling through", name, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - never let a provider fault stop a run
            log.warning("%s call failed: %s", name, exc)
            continue

        if not raw:
            continue
        try:
            return json.loads(_strip_fence(raw))
        except json.JSONDecodeError:
            log.warning("%s returned unparseable JSON", name)
            continue
    return None
